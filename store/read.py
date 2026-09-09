"""Typed read views over the store: one query, one row type, no SQL elsewhere.

The loop, the supervisor sweep, `--report`, the FINDINGS renderer and the
coming `serve` daemon all read the same tables. Until this module each of
them carried its own `SELECT` and its own knowledge of column order, so a
schema change had a dozen silent blast sites in `factory.py`. Here every read
is a named function returning a frozen dataclass (or a list of them) whose
fields are the columns it carries, spelled as the schema spells them, so a
reader can grep `SCHEMA` for any field and a Rust port has its row structs
drawn for it.

Rules the module keeps:

- Explicit SQL strings and explicit tuple-to-dataclass construction. No ORM,
  no query builder, nothing reflected off `cursor.description`.
- Functions take the connection the caller already holds -- the loop's
  writable one or a fresh `open_readonly()` -- and never open their own.
- Reads that fetch the same row with different column subsets are one
  function carrying the union, so the row type is the row.
- Nothing here writes. Writers stay in `store/__init__.py`.

Run the tests: python3 -m unittest discover -s tests -p 'test_store_read*' -v
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import store


def open_readonly(path) -> sqlite3.Connection:
    """Open the store at `path` read-only and return the connection.

    A `mode=ro` URI open: the file is never created, and any write through
    the connection fails with `sqlite3.OperationalError` rather than taking
    the write lock. WAL-safe -- a read-only connection to a WAL store reads
    the last committed snapshot while the loop keeps writing, which is why
    report, sweep, FINDINGS and serve paths open through here instead of
    through the writable opener.

    `row_factory` is left unset on purpose: the functions below build their
    rows themselves, column by column, so the tuple shape is the contract.

    Waits `store.BUSY_TIMEOUT_S` for a lock, the same as the writable
    opener, so a reader is not the one that dies when a checkpoint or a
    long write holds the file.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=store.BUSY_TIMEOUT_S)


# --- tickets -----------------------------------------------------------------


@dataclass(frozen=True)
class Ticket:
    """The `tickets` columns the loop reads back about one ticket."""

    id: int
    linearIssueId: str
    linearIdentifier: str
    status: str
    activeRunId: int | None
    lastRunId: int | None


def ticket_by_id(conn, ticket_id):
    """The ticket row for `ticket_id`, or None when there is no such ticket.

    One read for `store_status`, `warn`, `mirror_push` and `escalate`: each
    wanted a different two or three of these columns off the same row.
    """
    row = conn.execute(
        "SELECT id, linearIssueId, linearIdentifier, status,"
        " activeRunId, lastRunId FROM tickets WHERE id = ?",
        (ticket_id,)).fetchone()
    if row is None:
        return None
    return Ticket(id=row[0], linearIssueId=row[1], linearIdentifier=row[2],
                  status=row[3], activeRunId=row[4], lastRunId=row[5])


@dataclass(frozen=True)
class BlockedTicket:
    """One ticket parked `blocked_on_operator`, with the question it asks.

    `runId` is the run parked for it (`tickets.lastRunId`: `park()` and
    `release()` both move the pointer there) and `askedMs` when the question
    was asked: the newest `redirect` intervention on that run, else the
    run's `lastHeartbeat` for a ticket parked by a module that recorded no
    redirect. Both are None only for a ticket that was parked with no run
    behind it at all.
    """

    id: int
    linearIdentifier: str
    blockedQuestion: str | None
    runId: int | None = None
    askedMs: int | None = None


def blocked_tickets(conn):
    """Every ticket whose status is `blocked_on_operator`, oldest id first.

    The `serve` daemon's `/attention` read: a parked ticket is the one thing
    the operator must answer, and `blockedQuestion` is what it asks. None
    when the ticket was parked without one. The parked run and the moment
    of asking ride along so the band can age the question and name the run
    without deriving either from the poll time.
    """
    rows = conn.execute(
        "SELECT t.id, t.linearIdentifier, t.blockedQuestion, r.id,"
        " (SELECT MAX(i.at) FROM interventions i"
        "  WHERE i.runId = r.id AND i.\"action\" = 'redirect'),"
        " r.lastHeartbeat"
        " FROM tickets t LEFT JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.status = 'blocked_on_operator' ORDER BY t.id").fetchall()
    return [BlockedTicket(id=row[0], linearIdentifier=row[1],
                          blockedQuestion=row[2], runId=row[3],
                          askedMs=row[4] if row[4] is not None else row[5])
            for row in rows]


@dataclass(frozen=True)
class OpenTicket:
    """One ticket in an open (non-terminal) status, with what it waits on.

    `waitsOn` is the ticket's `dependsOn` list resolved to identifiers
    through the same table, holding only the dependencies still open; a
    Linear issue id the store has never mirrored is kept as-is, since the
    store cannot name what it has not seen. `activeRunId` is the live run's
    id, None when the ticket is not being worked.
    """

    id: int
    linearIdentifier: str
    title: str
    status: str
    timeBoxMs: int | None
    activeRunId: int | None
    blockedQuestion: str | None
    waitsOn: tuple[str, ...]
    mirroredAt: int


def open_tickets(conn, project_id=None):
    """Every ticket whose status is not `merged` or `abandoned`, ordered
    by identifier; only `project_id`'s tickets when one is given.

    The `serve` daemon's `/board` read across every project: the store's
    mirror of Linear's columns, grouped by the caller. The loop's startup
    reconcile (KO-329) reads it scoped to its own project, since the
    provider it asks knows only that project's team. `dependsOn` names
    Linear issue ids; each is resolved to an identifier through the open
    rows, dropped when it names a closed ticket (a merged dependency is
    no longer a wait), and kept as-is when the store has never mirrored
    it.
    """
    scope = "" if project_id is None else " AND projectId = ?"
    params = () if project_id is None else (project_id,)
    rows = conn.execute(
        "SELECT id, linearIssueId, linearIdentifier, title, status,"
        " timeBoxMs, activeRunId, blockedQuestion, dependsOn, mirroredAt"
        " FROM tickets WHERE status NOT IN ('merged', 'abandoned')"
        + scope + " ORDER BY linearIdentifier", params).fetchall()
    # The closed ids are read too, so a dependency on a merged ticket is
    # told apart from one the store has never seen.
    mirrored = {row[1]: row[2] for row in rows}
    closed = {row[0] for row in conn.execute(
        "SELECT linearIssueId FROM tickets"
        " WHERE status IN ('merged', 'abandoned')")}
    return [OpenTicket(id=row[0], linearIdentifier=row[2], title=row[3],
                       status=row[4], timeBoxMs=row[5], activeRunId=row[6],
                       blockedQuestion=row[7],
                       waitsOn=tuple(mirrored.get(dep, dep)
                                     for dep in json.loads(row[8])
                                     if dep not in closed),
                       mirroredAt=row[9])
            for row in rows]


# --- runs --------------------------------------------------------------------


@dataclass(frozen=True)
class RunSnapshot:
    """Where one run stands right now: the columns the sweep re-checks."""

    id: int
    ticketId: int
    phase: str
    lastHeartbeat: int
    endedAt: int | None


def run_snapshot(conn, run_id):
    """The run row for `run_id` as the sweep sees it, or None if there is none.

    Read under the caller's lock when the caller holds one: `still_tripped`
    and `act_on_trip` ask this at the moment of acting so the phase an
    outcome names is the one the decision was made on.
    """
    row = conn.execute(
        "SELECT id, ticketId, phase, lastHeartbeat, endedAt"
        " FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return RunSnapshot(id=row[0], ticketId=row[1], phase=row[2],
                       lastHeartbeat=row[3], endedAt=row[4])


@dataclass(frozen=True)
class LiveRun:
    """One unended run in a sweepable phase, with its ticket's label and
    title and the review rounds it has recorded so far."""

    id: int
    linearIdentifier: str
    title: str
    phase: str
    lastHeartbeat: int
    startedAt: int
    timeBoxMs: int | None
    host: str | None
    # Counted off the run's own `reviewRounds` rows, as `store.release()`
    # stamps `runs.reviewRoundCount` at close-out: on a live run the column
    # is still 0, and the rows are the count it will be stamped with.
    reviewRoundCount: int


@dataclass(frozen=True)
class ApprovedCandidate:
    """The prior run an approval released, the sha it was parked on, and
    the pull request `[merge] mode = "pr"` opened for it (None when the park
    opened none)."""

    run_id: int
    sha: str | None
    pr_url: str | None = None
    # Whether the release was an approval -- the human's "merge" -- rather
    # than `--shepherd`'s "look at the pull request again". Only the PR
    # path reads it: a local candidate the operator released is merged.
    approved: bool = True
    # The sha the last independent judgement covered when the run parked:
    # the reviewer's approval or an operator's release. Under `mode = "pr"`
    # a fix round or a rejected fix leaves `sha` past it; None when the
    # park recorded none (a store older than the column).
    approved_sha: str | None = None


def approved_candidate(conn, ticket_id, run_id):
    """The run whose approved candidate `run_id` should take to the gate.

    The newest run of `ticket_id` other than `run_id` itself, if it ended
    with `resumePhase` at the merge gate -- which is what `store.approve()`
    writes on a run parked awaiting merge approval. Returns that run's id
    with the `candidateSha` its park recorded (None on a run parked by a
    module older than the column) and the `prUrl` the park wrote when
    `[merge] mode = "pr"` opened a pull request for it, or None when the
    newest prior run is anything else: the claim then starts the ticket
    over, as it would after a failed run. `approved` is False when the
    newest intervention on that run is `shepherd` rather than `approve`;
    `approved_sha` is the `approvedSha` the park recorded.
    """
    row = conn.execute(
        "SELECT id, resumePhase, candidateSha, prUrl, approvedSha FROM runs"
        " WHERE ticketId = ? AND id <> ?"
        " ORDER BY attempt DESC LIMIT 1", (ticket_id, run_id)).fetchone()
    if row is None or row[1] != "merge_gate":
        return None
    last = conn.execute(
        'SELECT "action" FROM interventions WHERE runId = ?'
        " ORDER BY id DESC LIMIT 1", (row[0],)).fetchone()
    return ApprovedCandidate(run_id=row[0], sha=row[2], pr_url=row[3],
                             approved=last is None or last[0] != "shepherd",
                             approved_sha=row[4])


def live_runs(conn, phases):
    """Every run with no `endedAt` whose phase is in `phases`, oldest id first.

    `phases` is the caller's policy -- the sweep passes its
    `SWEEPABLE_PHASES` -- so this module states no opinion about which live
    runs are worth watching.
    """
    phases = tuple(phases)
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, t.title, r.phase, r.lastHeartbeat,"
        " r.startedAt, r.timeBoxMs, r.host,"
        " (SELECT COUNT(*) FROM reviewRounds rr WHERE rr.runId = r.id)"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NULL"
        f"   AND r.phase IN ({', '.join('?' * len(phases))})"
        " ORDER BY r.id", phases).fetchall()
    return [LiveRun(id=row[0], linearIdentifier=row[1], title=row[2],
                    phase=row[3], lastHeartbeat=row[4], startedAt=row[5],
                    timeBoxMs=row[6], host=row[7], reviewRoundCount=row[8])
            for row in rows]


@dataclass(frozen=True)
class EndedRun:
    """One run that ended, joined to its ticket's label.

    The union of what `--report` and the FINDINGS renderer each read off the
    same rows: the timing columns for the estimate-vs-actual table, the
    outcome columns for the rendered entry.
    """

    id: int
    linearIdentifier: str
    startedAt: int
    endedAt: int
    timeBoxMs: int | None
    reviewRoundCount: int
    outcome: str | None
    outcomeReason: str | None
    branch: str | None
    host: str | None
    # The merge commit on main, full sha; None unless the run merged under a
    # module that wrote the column.
    mergeSha: str | None


def ended_runs(conn):
    """Every run with an `endedAt`, ordered by when it ended, then by id."""
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, r.startedAt, r.endedAt, r.timeBoxMs,"
        " r.reviewRoundCount, r.outcome, r.outcomeReason, r.branch, r.host,"
        " r.mergeSha"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NOT NULL"
        " ORDER BY r.endedAt, r.id").fetchall()
    return [EndedRun(id=row[0], linearIdentifier=row[1], startedAt=row[2],
                     endedAt=row[3], timeBoxMs=row[4], reviewRoundCount=row[5],
                     outcome=row[6], outcomeReason=row[7], branch=row[8],
                     host=row[9], mergeSha=row[10])
            for row in rows]


@dataclass(frozen=True)
class MergedRun:
    """One merged run, joined to its ticket, with its findings counted: what
    `/shipped` draws a row from."""

    id: int
    linearIdentifier: str
    title: str
    startedAt: int
    endedAt: int
    timeBoxMs: int | None
    reviewRoundCount: int
    # The count of findings over the run's review rounds, summed in SQL
    # (`json_array_length(findings)`) so a page never loads the rounds.
    findingCount: int
    host: str | None
    mergeSha: str | None


# The range of a SQLite INTEGER, and so of any run id a cursor can name.
SQLITE_INT64_MIN = -(2 ** 63)
SQLITE_INT64_MAX = 2 ** 63 - 1


def merged_runs(conn, limit, before=None):
    """Up to `limit` runs with outcome `merged`, newest end first (ties by
    id descending), keyset-paged on `(endedAt, id)`.

    `before` is a run id: only runs that ended before that run's end (or
    at the same instant with a smaller id) are answered, so a client pages
    by passing the last id it saw. An id no run has is an empty page, not
    an error: the run may have been the last on a page that is now gone.
    """
    if (before is not None
            and not SQLITE_INT64_MIN <= before <= SQLITE_INT64_MAX):
        # Past what an INTEGER column can hold, so no run has it; binding
        # it would raise OverflowError rather than answer the empty page.
        return []
    where = "r.outcome = 'merged' AND r.endedAt IS NOT NULL"
    params = []
    if before is not None:
        where += (" AND (r.endedAt, r.id) < (SELECT endedAt, id FROM runs"
                  " WHERE id = ? AND endedAt IS NOT NULL)")
        params.append(before)
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, t.title, r.startedAt, r.endedAt,"
        " r.timeBoxMs, r.reviewRoundCount,"
        " (SELECT COALESCE(SUM(json_array_length(rr.findings)), 0)"
        "    FROM reviewRounds rr WHERE rr.runId = r.id),"
        " r.host, r.mergeSha"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        f" WHERE {where}"
        " ORDER BY r.endedAt DESC, r.id DESC LIMIT ?",
        (*params, limit)).fetchall()
    return [MergedRun(id=row[0], linearIdentifier=row[1], title=row[2],
                      startedAt=row[3], endedAt=row[4], timeBoxMs=row[5],
                      reviewRoundCount=row[6], findingCount=row[7],
                      host=row[8], mergeSha=row[9])
            for row in rows]


@dataclass(frozen=True)
class FailedAttempt:
    """One failed run of a ticket, by lifetime attempt number."""

    attempt: int
    outcomeReason: str | None


def latest_human_intervention_at(conn, ticket_id):
    """When a human last intervened on any run of `ticket_id`; 0 if never."""
    (at,) = conn.execute(
        "SELECT COALESCE(MAX(i.at), 0) FROM interventions i"
        " JOIN runs r ON r.id = i.runId"
        " WHERE r.ticketId = ? AND i.source = 'human'",
        (ticket_id,)).fetchone()
    return at


def failed_attempts_since(conn, ticket_id, since):
    """The failed `work` runs of `ticket_id` that ended after `since`.

    Ordered by attempt. A run a human closed out by hand (an
    `interventions` row with `source = 'human'` and `action = 'close_out'`)
    is left out by identity, whatever its `endedAt` -- see
    `failure_history()` in `factory.py` for why.
    """
    rows = conn.execute(
        "SELECT attempt, outcomeReason FROM runs r"
        " WHERE ticketId = ? AND outcome = 'failed' AND endedAt > ?"
        " AND outcomeClass = 'work'"
        " AND NOT EXISTS (SELECT 1 FROM interventions i"
        "                 WHERE i.runId = r.id AND i.source = 'human'"
        "                 AND i.\"action\" = 'close_out')"
        " ORDER BY attempt", (ticket_id, since)).fetchall()
    return [FailedAttempt(attempt=row[0], outcomeReason=row[1]) for row in rows]


@dataclass(frozen=True)
class RecentFailedRun:
    """One run that ended `failed`, with where its ticket stands now.

    `ticketStatus` is the ticket's current `tickets.status`: a reader that
    lists failures the operator has not dealt with keeps the `in_flight`
    ones (a failed run leaves its ticket there with no active run) and
    drops one whose ticket has since been requeued (`ready`) or merged.
    `attempt` is `runs.attempt`, 1-based, so a client can say "strike 2 of
    3" without counting failures it has not seen.
    """

    id: int
    linearIdentifier: str
    outcomeReason: str | None
    endedAt: int
    ticketStatus: str
    attempt: int = 0


def recent_failed_runs(conn, since_ms):
    """Every run that ended `failed` after `since_ms`, oldest end first.

    The window is the caller's policy -- `/attention` passes its own -- so
    this module states no opinion about how long a failure stays news.
    """
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, r.outcomeReason, r.endedAt,"
        " t.status, r.attempt"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.outcome = 'failed' AND r.endedAt > ?"
        " ORDER BY r.endedAt, r.id", (since_ms,)).fetchall()
    return [RecentFailedRun(id=row[0], linearIdentifier=row[1],
                            outcomeReason=row[2], endedAt=row[3],
                            ticketStatus=row[4], attempt=row[5])
            for row in rows]


# --- reviewRounds ------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRound:
    """One review round with its ticket's label, as the FINDINGS entry reads it.

    `verificationResults` and `findings` are the store's JSON documents,
    uncoded: the renderer decides how to treat one that does not decode.
    """

    id: int
    linearIdentifier: str
    round: int
    verdict: str
    reviewerModel: str
    verificationResults: str
    findings: str
    startedAt: int
    endedAt: int | None


def review_rounds(conn):
    """Every review round the store holds, in no particular order.

    The FINDINGS renderer sorts entries itself, defensively, because a stamp
    column can hold something that is not a time; so the order here is
    whatever SQLite returns and the caller must not lean on it.
    """
    rows = conn.execute(
        "SELECT rr.id, t.linearIdentifier, rr.round, rr.verdict,"
        " rr.reviewerModel, rr.verificationResults, rr.findings,"
        " rr.startedAt, rr.endedAt"
        " FROM reviewRounds rr JOIN runs r ON r.id = rr.runId"
        " JOIN tickets t ON t.id = r.ticketId").fetchall()
    return [ReviewRound(id=row[0], linearIdentifier=row[1], round=row[2],
                        verdict=row[3], reviewerModel=row[4],
                        verificationResults=row[5], findings=row[6],
                        startedAt=row[7], endedAt=row[8])
            for row in rows]


@dataclass(frozen=True)
class EndedRound:
    """One finished review round of a run: its number and its findings JSON."""

    round: int
    findings: str


def newest_ended_rounds(conn, run_id):
    """The two newest rounds of `run_id` with an `endedAt`, newest first.

    The pair the stuck-review measure compares; fewer than two come back when
    the run has not been reviewed twice yet.
    """
    rows = conn.execute(
        "SELECT round, findings FROM reviewRounds"
        " WHERE runId = ? AND endedAt IS NOT NULL"
        " ORDER BY round DESC LIMIT 2", (run_id,)).fetchall()
    return [EndedRound(round=row[0], findings=row[1]) for row in rows]


@dataclass(frozen=True)
class RunDetail:
    """One run in full, joined to its ticket: what `/runs/N` answers."""

    id: int
    linearIdentifier: str
    title: str
    phase: str
    attempt: int
    startedAt: int
    endedAt: int | None
    lastHeartbeat: int
    outcome: str | None
    timeBoxMs: int | None
    branch: str | None
    host: str | None
    mergeSha: str | None
    # The review-round cap the loop gave the run; None on a run recorded
    # before the column existed, which `/runs/N` answers with the constant.
    reviewRoundCap: int | None


def run_detail(conn, run_id):
    """The run row for `run_id` with its ticket's label and title, or None."""
    row = conn.execute(
        "SELECT r.id, t.linearIdentifier, t.title, r.phase, r.attempt,"
        " r.startedAt, r.endedAt, r.lastHeartbeat, r.outcome, r.timeBoxMs,"
        " r.branch, r.host, r.mergeSha, r.reviewRoundCap"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return RunDetail(id=row[0], linearIdentifier=row[1], title=row[2],
                     phase=row[3], attempt=row[4], startedAt=row[5],
                     endedAt=row[6], lastHeartbeat=row[7], outcome=row[8],
                     timeBoxMs=row[9], branch=row[10], host=row[11],
                     mergeSha=row[12], reviewRoundCap=row[13])


@dataclass(frozen=True)
class RunRound:
    """One review round of a run, as the run detail lists it: `findings` is
    the store's JSON document, undecoded, as on `ReviewRound`."""

    round: int
    startedAt: int
    endedAt: int | None
    verdict: str
    reviewerModel: str
    findings: str


def rounds_of(conn, run_id):
    """Every review round of `run_id`, oldest first; `[]` for a run with none
    or no such run."""
    rows = conn.execute(
        "SELECT round, startedAt, endedAt, verdict, reviewerModel, findings"
        " FROM reviewRounds WHERE runId = ? ORDER BY round", (run_id,)).fetchall()
    return [RunRound(round=row[0], startedAt=row[1], endedAt=row[2],
                     verdict=row[3], reviewerModel=row[4], findings=row[5])
            for row in rows]


@dataclass(frozen=True)
class NarrativeEvent:
    """One `narrative`-level row of a run's event stream."""

    at: int
    kind: str
    summary: str


def narrative_events(conn, run_id):
    """The `narrative` events of `run_id` in `seq` order, oldest first; the
    `detail` rows and their payloads are left out."""
    rows = conn.execute(
        "SELECT at, kind, summary FROM runEvents"
        " WHERE runId = ? AND level = 'narrative' ORDER BY seq",
        (run_id,)).fetchall()
    return [NarrativeEvent(at=row[0], kind=row[1], summary=row[2])
            for row in rows]


# --- ledger ------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEntry:
    """One entry of a run's narrative, as `store.record_ledger()` wrote it.

    An `intervention` entry also says what the operator's step cleared and
    how long that had waited (KO-308): `cleared` is `"question"` or
    `"failed"` and `waitedMs` the wait in milliseconds, both None when
    nothing was waiting -- `_cleared_by()` is the rule. Other kinds carry
    None for both.
    """

    id: int
    runId: int
    ticketId: int
    at: int
    kind: str
    text: str
    source: str
    cleared: str | None = None
    waitedMs: int | None = None


# The two marks a ledger entry's own run offers for what an operator's step
# cleared: the ask (its newest `redirect` intervention strictly before the
# entry) and the failure (its `endedAt`, when set and strictly before the
# entry). Selected alongside the entry so the rule is one function over two
# values rather than a second read per row.
_LEDGER_MARKS = (
    " (SELECT MAX(i.at) FROM interventions i"
    "  WHERE i.runId = ledger.runId AND i.\"action\" = 'redirect'"
    "  AND i.at < ledger.at),"
    " (SELECT r.endedAt FROM runs r"
    "  WHERE r.id = ledger.runId AND r.endedAt < ledger.at)")


def _cleared_by(kind, at, asked, ended):
    """What an `intervention` entry at `at` cleared and how long it waited.

    The rule KO-308 fixes, in words `docs/reference/http.md` repeats: of
    the run's newest `redirect` strictly before the entry (`asked`) and its
    `endedAt` when strictly before the entry (`ended`), the newer mark wins
    -- `("question", at - asked)` or `("failed", at - ended)`; with neither
    mark, `(None, None)`. A `redirect` entry never pairs with itself, since
    its own row is not strictly before it. Entries of other kinds answer
    `(None, None)` too: only an operator's step clears anything.
    """
    if kind != "intervention":
        return None, None
    if asked is None and ended is None:
        return None, None
    # Compare the timestamps alone: a tuple `max` would break an equal-
    # timestamp tie on the name, and "question" sorts above "failed".
    if ended is None or (asked is not None and asked > ended):
        return "question", at - asked
    return "failed", at - ended


def _ledger_entry(cls, row, **owner):
    """Build a ledger entry of `cls` from a row selected with `_LEDGER_MARKS`
    appended; `owner` is the third column under the name `cls` gives it."""
    cleared, waited = _cleared_by(row[4], row[3], row[7], row[8])
    return cls(id=row[0], runId=row[1], at=row[3], kind=row[4], text=row[5],
               source=row[6], cleared=cleared, waitedMs=waited, **owner)


def ledger(conn, run_id):
    """Every ledger entry of `run_id`, oldest first; `[]` for a run with none
    or no such run. Two entries written in the same millisecond keep the
    order they were written in."""
    rows = conn.execute(
        "SELECT ledger.id, ledger.runId, ledger.ticketId, ledger.at,"
        " ledger.kind, ledger.text, ledger.source," + _LEDGER_MARKS +
        " FROM ledger WHERE ledger.runId = ? ORDER BY ledger.at, ledger.id",
        (run_id,)).fetchall()
    return [_ledger_entry(LedgerEntry, row, ticketId=row[2]) for row in rows]


@dataclass(frozen=True)
class LedgerWindowEntry:
    """One ledger entry across runs, with its ticket's identifier for the
    console: the `/ledger` window (design note 9) and a ticket's thread."""

    id: int
    runId: int
    ticket: str
    at: int
    kind: str
    text: str
    source: str
    cleared: str | None = None
    waitedMs: int | None = None


def ledger_since(conn, since, kind=None, ticket=None, limit=200):
    """Ledger entries at or after `since` (epoch ms) across every run,
    newest first, at most `limit`; narrowed to one `kind` or one ticket
    identifier (`KO-n`) when given. Two entries in the same millisecond
    come back in reverse write order, so the window is a stable page.
    """
    where = ["ledger.at >= ?"]
    args = [since]
    if kind is not None:
        where.append("ledger.kind = ?")
        args.append(kind)
    if ticket is not None:
        where.append("tickets.linearIdentifier = ?")
        args.append(ticket)
    args.append(limit)
    rows = conn.execute(
        "SELECT ledger.id, ledger.runId, tickets.linearIdentifier, ledger.at,"
        " ledger.kind, ledger.text, ledger.source," + _LEDGER_MARKS +
        " FROM ledger JOIN tickets ON tickets.id = ledger.ticketId"
        f" WHERE {' AND '.join(where)} ORDER BY ledger.at DESC, ledger.id DESC"
        " LIMIT ?", args).fetchall()
    return [_ledger_entry(LedgerWindowEntry, row, ticket=row[2])
            for row in rows]


# --- sweepStrikes ------------------------------------------------------------


@dataclass(frozen=True)
class Strike:
    """The sweep's tally on file for one run under suspicion."""

    runId: int
    strikes: int
    lastSeen: int


def strike(conn, run_id):
    """The `sweepStrikes` row for `run_id`, or None when it is not suspected."""
    row = conn.execute(
        "SELECT runId, strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
        (run_id,)).fetchone()
    if row is None:
        return None
    return Strike(runId=row[0], strikes=row[1], lastSeen=row[2])


# --- supervisorHeartbeats ----------------------------------------------------


@dataclass(frozen=True)
class SupervisorBeat:
    """The newest supervisor heartbeat: the one watcher that could be alive."""

    pid: int
    startedAt: int
    lastBeat: int
    passes: int
    host: str | None


def supervisor_beat(conn):
    """The heartbeat row with the most recent `lastBeat`, or None if none.

    The SELECT `store.latest_supervisor_heartbeat()` makes, as a row type
    rather than a tuple: the `serve` daemon reads the supervisor's state
    through here so it imports nothing from `store` itself. `host` is None
    for a beat written before the column existed.
    """
    row = conn.execute(
        "SELECT pid, startedAt, lastBeat, passes, host"
        " FROM supervisorHeartbeats"
        " ORDER BY lastBeat DESC, startedAt DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return SupervisorBeat(pid=row[0], startedAt=row[1], lastBeat=row[2],
                          passes=row[3], host=row[4])

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

import sqlite3
from dataclasses import dataclass
from pathlib import Path


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
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


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
    """One ticket parked `blocked_on_operator`, with the question it asks."""

    id: int
    linearIdentifier: str
    blockedQuestion: str | None


def blocked_tickets(conn):
    """Every ticket whose status is `blocked_on_operator`, oldest id first.

    The `serve` daemon's `/attention` read: a parked ticket is the one thing
    the operator must answer, and `blockedQuestion` is what it asks. None
    when the ticket was parked without one.
    """
    rows = conn.execute(
        "SELECT id, linearIdentifier, blockedQuestion FROM tickets"
        " WHERE status = 'blocked_on_operator' ORDER BY id").fetchall()
    return [BlockedTicket(id=row[0], linearIdentifier=row[1],
                          blockedQuestion=row[2])
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
    """One unended run in a sweepable phase, with its ticket's label."""

    id: int
    linearIdentifier: str
    phase: str
    lastHeartbeat: int
    startedAt: int
    timeBoxMs: int | None
    host: str | None


def live_runs(conn, phases):
    """Every run with no `endedAt` whose phase is in `phases`, oldest id first.

    `phases` is the caller's policy -- the sweep passes its
    `SWEEPABLE_PHASES` -- so this module states no opinion about which live
    runs are worth watching.
    """
    phases = tuple(phases)
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, r.phase, r.lastHeartbeat,"
        " r.startedAt, r.timeBoxMs, r.host"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NULL"
        f"   AND r.phase IN ({', '.join('?' * len(phases))})"
        " ORDER BY r.id", phases).fetchall()
    return [LiveRun(id=row[0], linearIdentifier=row[1], phase=row[2],
                    lastHeartbeat=row[3], startedAt=row[4], timeBoxMs=row[5],
                    host=row[6])
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


def ended_runs(conn):
    """Every run with an `endedAt`, ordered by when it ended, then by id."""
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, r.startedAt, r.endedAt, r.timeBoxMs,"
        " r.reviewRoundCount, r.outcome, r.outcomeReason, r.branch, r.host"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NOT NULL"
        " ORDER BY r.endedAt, r.id").fetchall()
    return [EndedRun(id=row[0], linearIdentifier=row[1], startedAt=row[2],
                     endedAt=row[3], timeBoxMs=row[4], reviewRoundCount=row[5],
                     outcome=row[6], outcomeReason=row[7], branch=row[8],
                     host=row[9])
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
    """

    id: int
    linearIdentifier: str
    outcomeReason: str | None
    endedAt: int
    ticketStatus: str


def recent_failed_runs(conn, since_ms):
    """Every run that ended `failed` after `since_ms`, oldest end first.

    The window is the caller's policy -- `/attention` passes its own -- so
    this module states no opinion about how long a failure stays news.
    """
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, r.outcomeReason, r.endedAt,"
        " t.status"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.outcome = 'failed' AND r.endedAt > ?"
        " ORDER BY r.endedAt, r.id", (since_ms,)).fetchall()
    return [RecentFailedRun(id=row[0], linearIdentifier=row[1],
                            outcomeReason=row[2], endedAt=row[3],
                            ticketStatus=row[4])
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

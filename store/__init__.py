"""store: the v2 durable state store, one WAL-mode SQLite file.

Local-first by resolved decision (2026-08-22): all loop state lives behind
this one module so a hosted backend later is a driver swap, not a loop
rewrite. Stdlib ``sqlite3`` only.

The API so far is ``open()`` and ``init()`` for the schema,
``ensure_project()`` for the repo's projects row, ``claim()``/``release()``
for the per-ticket lease, ``mirror_ticket()``/``transition()`` for ticket
status, ``pickable()`` for the pickability predicate,
``resume()`` for the resume guidance invariant,
``findings_fingerprint()``/``findings_overlap()`` for stuck-review
detection, ``record_review_round()`` for the rows they read, and
``contract_snapshot()``/``run_contract()``/``contract_drift()`` for the
claim-time freeze of a ticket's contract and the drift check against it.

Conventions, fixed here for every later ticket to follow:

* **camelCase** table and column names, matching the field names in the
  state model verbatim. The table names are camelCase in the contract
  (``reviewRounds``, ``runEvents``, ``linearDeliveries``), so the columns
  follow rather than splitting the file across two casings.
* **List-typed fields are JSON text.** SQLite has no array type, so the
  contract's ``string[]`` and object-array fields are stored as a JSON
  document defaulting to ``'[]'``. ``mirror_ticket()`` encodes the ticket's
  three lists on the way in; decoding belongs to the readers, in the later
  tickets.
* **Optional (``?``) fields are nullable; everything else is NOT NULL.**
* **Union types become CHECK constraints**, so an unknown status, phase or
  verdict is rejected by the database rather than by a caller who
  remembered to look.
* **Rows are keyed by a synthetic ``id INTEGER PRIMARY KEY``**, standing in
  for the contract's Convex-shaped ``Id<table>`` references.

Contract source: docs/v2/state-model.md §1-§3, plus the lease column from
§7, which KO-341 moved from the project to the ticket. That document is
deliberately gitignored, so the sections are cited here instead of vendored.
"""
from __future__ import annotations

import hashlib
import json
import socket
import time

from .schema import SCHEMA_VERSION, _transaction, init, open, transaction  # noqa: F401


class ClaimConflict(Exception):
    """A claim was refused and nothing was written.

    The typed failure of `claim()`: either the project already has an active
    run — v0's single-threading rule (state-model §7), so this caller does not
    get to start another one — or the ticket does not belong to the project
    whose lease was asked for. Both leave every table exactly as it was.
    """


# --- the claim-time contract snapshot -----------------------------------------
# A run is worked to the ticket as it stood when the lease was taken: that
# body is what the implementer was briefed with and what the reviewer judged
# against. Linear keeps letting a human edit it, though, so the claim freezes
# the contract here and the merge gate compares the live ticket against the
# freeze before the branch lands.
#
# The fields are the ones a run is actually held to: the title it was briefed
# with, and the two lists §2's pickability predicate reads. The estimate is
# deliberately not among them — `runs.timeBoxMs` already snapshots it, and a
# re-pointed estimate changes what the run was budgeted, not what it was
# asked to do.
CONTRACT_FIELDS = ("title", "acceptanceCriteria", "verificationCommands")


def contract_snapshot(title, acceptance_criteria, verification_commands):
    """Freeze a ticket's contract as one canonical JSON document.

    Canonical so the same contract is the same bytes on both sides of a
    comparison: keys sorted, no encoder-variable whitespace, and the lists
    left in the order the ticket gives them — a reordered acceptance list is
    an edited ticket, not a formatting accident. Both sides build the document
    through this function rather than assembling their own, which is what
    keeps a drift check from reporting the callers' formatting as drift.
    """
    return json.dumps(
        {
            "title": title,
            "acceptanceCriteria": list(acceptance_criteria),
            "verificationCommands": list(verification_commands),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def run_contract(conn, run_id):
    """The snapshot run `run_id` was claimed under, or None if it has none.

    None is a real answer and not an error: a run claimed before this column
    existed has nothing frozen, and `contract_drift()` reads that as nothing
    to compare rather than as no drift. An unknown `run_id` is a caller bug
    and raises, the way `run_phase()` answers the same mistake.
    """
    row = conn.execute(
        "SELECT ticketSnapshot FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    return row[0]


def contract_drift(before, after):
    """The `CONTRACT_FIELDS` that differ between two snapshots, in field order.

    Empty when the two agree and empty when either is None — an unreadable or
    unrecorded side is a comparison that did not happen, and reporting it as
    drift would block a merge on a Linear outage. The answer is field names
    rather than a bare bool so the caller can say *what* moved: "the ticket
    changed" sends a human to a diff they have to find themselves.
    """
    if before is None or after is None:
        return ()
    was, is_now = json.loads(before), json.loads(after)
    return tuple(f for f in CONTRACT_FIELDS if was.get(f) != is_now.get(f))


def claim(conn, project_id, ticket_id, now=None):
    """Take the ticket's lease for a new run on `ticket_id`; return its id.

    One `BEGIN IMMEDIATE` transaction, per state-model §7 as KO-341 narrows
    it: assert `tickets.activeRunId IS NULL` for the chosen ticket, insert
    the `runs` row in phase `claimed`, then point `tickets.activeRunId` at
    it. The lease is per ticket, not per project: two loops on one target
    each claim a ticket of their own, and only a claim naming a ticket
    another live run holds is refused. `projects.activeRunId` is neither
    asserted nor written any more; a store from before this ticket keeps
    the column and it simply stays null from now on.

    IMMEDIATE matters. The lease is a read (is it free?) followed by a write
    (take it), and a deferred transaction takes no write lock until the write,
    which leaves room for two claimers to both read "free". IMMEDIATE takes the
    write lock up front, so concurrent claimers serialize in SQLite and the
    second one reads a lease that is already held. Losing is therefore a
    deterministic `ClaimConflict`, not a race.

    Every failure path rolls back, so a lost claim leaves no orphan `runs` row.
    `attempt` is 1 + the ticket's prior runs, making it 1-based. `now` is epoch
    milliseconds for `startedAt`/`lastHeartbeat`, defaulting to the clock.

    The run also takes its own copy of the ticket's `timeBoxMs`, because the
    claim is the moment the estimate applied to this attempt: a later mirror
    of the same Linear issue may carry a re-pointed estimate, and a run row
    that read it back through the ticket would silently restate what it was
    budgeted. A ticket with no estimate snapshots NULL, the same "unknown".

    `ticketSnapshot` is frozen for the same reason and one more: the contract
    the run is worked to is the body as it stood at the claim, so a mirror
    that later re-points the title or either list must not be able to change
    what this run was asked for after the fact. The merge gate reads the
    freeze back through `run_contract()` and compares it with the live ticket.
    """
    if now is None:
        now = int(time.time() * 1000)
    conn.execute("BEGIN IMMEDIATE")
    try:
        # A ticket row that does not exist matches nothing here and is
        # refused a moment later by the ownership check below: an unknown
        # ticket is a malformed claim, not a lease conflict, and reads
        # better as one. The refusal names the ticket by its identifier,
        # which is what the loop's line and the operator's `--sweep` use.
        held = conn.execute(
            "SELECT activeRunId, linearIdentifier FROM tickets"
            " WHERE id = ? AND activeRunId IS NOT NULL",
            (ticket_id,),
        ).fetchone()
        if held is not None:
            raise ClaimConflict(
                f"ticket {held[1]}: lease already held by run {held[0]}"
            )
        (prior,) = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE ticketId = ?", (ticket_id,)
        ).fetchone()
        # A ticket that does not exist reads as no estimate and no contract
        # here and is refused a moment later by the ownership check below, so
        # this read decides nothing about whether the claim is legal. One
        # SELECT for both snapshots, so the estimate and the contract a run
        # records are the same ticket at the same instant.
        ticket = conn.execute(
            "SELECT timeBoxMs, title, acceptanceCriteria, verificationCommands"
            " FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        estimate = ticket[0] if ticket else None
        snapshot = None if ticket is None else contract_snapshot(
            ticket[1], json.loads(ticket[2]), json.loads(ticket[3]))
        run_id = conn.execute(
            "INSERT INTO runs"
            " (ticketId, projectId, attempt, phase, startedAt, lastHeartbeat,"
            "  timeBoxMs, ticketSnapshot, host)"
            " VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?, ?)",
            (ticket_id, project_id, prior + 1, now, now, estimate, snapshot,
             socket.gethostname()),
        ).lastrowid
        # Scoped by projectId as well as id: claiming another project's ticket
        # would otherwise open a run of this project on work it does not
        # own. Zero rows updated means exactly that, and is refused.
        updated = conn.execute(
            "UPDATE tickets SET activeRunId = ? WHERE id = ? AND projectId = ?",
            (run_id, ticket_id, project_id),
        ).rowcount
        if updated != 1:
            raise ClaimConflict(
                f"ticket {ticket_id} is not a ticket of project {project_id}"
            )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return run_id


# §4's phase union, transcribed from the diagram's phase list. It duplicates
# the `runs.phase` CHECK constraint deliberately: the constraint is the
# enforcement, this is what a caller's typo is caught against *before* a
# transaction opens, so a misspelled phase reads as a named ValueError rather
# than as a bare IntegrityError from a table the caller never mentions.
PHASES = (
    "claimed", "working", "verifying", "reviewing", "addressing", "merge_gate",
    "awaiting_merge_approval", "merging", "squashing", "done",
    "blocked_on_operator", "failed", "killed",
)


class RunEnded(ValueError):
    """`set_phase()` was asked to move a run whose `endedAt` is stamped.

    A `ValueError` like every other refused write here, and a named one
    because the loop has to tell it apart from a caller bug: the run was
    ended underneath a live loop -- by the supervisor sweep, by an operator
    `--sweep --act` -- while the loop was blocked in an agent call, and the
    loop's next phase change is the moment it finds out. `run_id`, `outcome`
    and `reason` are the ended row's, so the catcher can say what ended it.
    """

    def __init__(self, run_id, outcome, reason):
        super().__init__(f"run {run_id} has ended ({outcome}: {reason})")
        self.run_id, self.outcome, self.reason = run_id, outcome, reason


def set_phase(conn, run_id, phase, note=None, now=None):
    """Move run `run_id` to `phase`; return the phase it was in.

    §6 gives run phase exactly one writer, and this is it for a running loop:
    a stage boundary is three facts — the new phase, a heartbeat proving the
    loop was alive at that boundary, and a narrative `runEvents` row saying so
    — and any of the three landing without the others is a lie about the run.
    A supervisor reading a phase whose heartbeat still sits at the previous
    boundary sees a run that has been stale for a stage; an event stream
    missing the transition its own run row shows is a stream nothing can be
    reconstructed from. So all three go in one `_transaction()`, and a reader
    sees either all of them or none.

    The event is `narrative` level with kind `phase_change`, which is §2's
    dual-level log read literally: transitions are low-volume and drive the
    live view, so they are written individually rather than batched, and
    `payload` stays NULL because §2 gives payloads to `detail` rows only.
    The event's `summary` always opens with `"<previous> -> <phase>"`, so the
    sequence a run walked can be read back off its own stream — a
    `phase_change` row that does not name its phases is a transition nothing
    can reconstruct the run from. An optional `note` is appended after a
    colon for what the phase names alone do not say: which review round, which
    branch.

    `seq` is `MAX(seq) + 1` for the run, read inside the transaction, so the
    `UNIQUE (runId, seq)` index stands behind the per-run monotonicity rather
    than a caller's counter. `now` is epoch milliseconds for `lastHeartbeat`
    and the event's `at`, defaulting to the clock.

    Re-entering the phase a run is already in is allowed and logged: the loop
    verifies once per review round, and collapsing those into one event would
    erase the round boundary the log exists to show. `resume()` is the one
    other phase writer and deliberately does not come through here — it moves
    a parked run without stamping a heartbeat it has no evidence for.

    A run with `endedAt` stamped is refused with `RunEnded`, and nothing is
    written: run 39 (KO-213) was failed by the supervisor sweep while its
    loop waited on an implementer, and the loop's next phase change walked
    the ended run `failed -> verifying -> reviewing` towards a merge under a
    row that said the work had failed. The ending is the last word on a run;
    the check sits inside the transaction so a release landing between the
    read and the UPDATE cannot slip through. `endedAt` alone is the test,
    whatever the phase says: `resume()` clears the stamp when it puts a run
    back to work, so a resumed run moves, and `release()` moves the terminal
    phase through here *before* it stamps `endedAt`, so an ending lands.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT phase, endedAt, outcome, outcomeReason FROM runs"
            " WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        previous, ended_at, outcome, reason = row
        if ended_at is not None:
            raise RunEnded(run_id, outcome, reason)
        conn.execute(
            "UPDATE runs SET phase = ?, lastHeartbeat = ? WHERE id = ?",
            (phase, now, run_id),
        )
        _append_event(
            conn, run_id, "narrative", "phase_change",
            f"{previous} -> {phase}" + (f": {note}" if note else ""), now)
    return previous


def set_branch(conn, run_id, branch):
    """Record `branch` as the task branch of the live run `run_id`.

    Written by the loop the moment it names the branch it is about to cut,
    before the first phase change to `working`: the console's files panel
    reads `runs.branch` to find the run's worktree, and a run whose branch
    was only known at its end answered "no branch" for the whole phase the
    panel exists to show (KO-304, run 127). An ended run is refused with
    `RunEnded`, as `set_phase()` refuses it: the branch of a finished run is
    history, and the close-out that ended it is the last word.
    """
    with _transaction(conn):
        row = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ended_at, outcome, reason = row
        if ended_at is not None:
            raise RunEnded(run_id, outcome, reason)
        conn.execute("UPDATE runs SET branch = ? WHERE id = ?",
                     (branch, run_id))


def set_review_round_cap(conn, run_id, cap):
    """Record `cap`, the review-round cap the loop gave the live run `run_id`.

    Written where the loop computes the cap from the candidate's size and
    the target's `[loop]` review keys, once, before round 1: `/runs/N`
    answers it as `max_rounds`, and a console that sized the round timeline
    by the module constant drew a fourth round past the end of a two-round
    bar (KO-321). An ended run is refused with `RunEnded`, as `set_phase()`
    refuses it: the cap of a finished run is history.
    """
    with _transaction(conn):
        row = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ended_at, outcome, reason = row
        if ended_at is not None:
            raise RunEnded(run_id, outcome, reason)
        conn.execute("UPDATE runs SET reviewRoundCap = ? WHERE id = ?",
                     (cap, run_id))


def heartbeat(conn, run_id, now=None):
    """Stamp `lastHeartbeat` on the live run `run_id`; return True if it did.

    The one `lastHeartbeat` writer that is not a stage boundary. `set_phase()`
    moves the heartbeat only when the phase moves, so a loop waiting on a
    30-minute implementer was silent for the whole wait and the supervisor
    read that silence as a dead worker (KO-212, run 39). This is the beat the
    loop sends on a timer while it waits: one UPDATE of one column, no phase
    change and no `runEvents` row, because a heartbeat is not narrative --
    a stream with a row every few minutes saying "still here" is a stream
    nobody can read the run off any more.

    A run with `endedAt` stamped is left exactly as it is, columns and all:
    the beat thread outlives its stage by a scheduling tick at most, and a
    beat that landed on a released run would make an ended run look live to
    a `--report` or a sweep that reads the heartbeat. `now` is epoch
    milliseconds, defaulting to the clock. Returns whether a row moved.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        cur = conn.execute(
            "UPDATE runs SET lastHeartbeat = ? WHERE id = ? AND endedAt IS NULL",
            (now, run_id))
    return cur.rowcount == 1


def run_phase(conn, run_id):
    """Return the phase run `run_id` is in.

    The read half of `set_phase()`, here for the same reason every other
    statement in this module is: a caller that needs the phase a run stopped
    in — to name it in an outcome reason, say — must not open its own SQL
    against `runs` to get it. An unknown `run_id` is a caller bug and raises
    `ValueError`, as it does everywhere else here.
    """
    row = conn.execute("SELECT phase FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    return row[0]


# §2's two log levels. `narrative` is the run's story and drives the live
# view; `detail` is the volume underneath it, and is the only level §2 gives a
# payload to.
EVENT_LEVELS = ("narrative", "detail")


def _append_event(conn, run_id, level, kind, summary, at, payload=None):
    """Append one row to run `run_id`'s event stream; return its `seq`.

    No transaction of its own, deliberately: an event describes a thing that
    happened, so it belongs to the transaction of the write it describes —
    `set_phase()` lands the phase, the heartbeat and this row together or not
    at all. `seq` is `MAX(seq) + 1` read inside that transaction, so the
    `UNIQUE (runId, seq)` index stands behind the per-run monotonicity rather
    than a caller's counter.
    """
    (seq,) = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM runEvents WHERE runId = ?",
        (run_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO runEvents (runId, seq, level, kind, summary, payload, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, seq, level, kind, summary, payload, at),
    )
    return seq


def record_event(conn, run_id, kind, summary, level="narrative", now=None,
                 payload=None):
    """Append one event of `kind` to run `run_id`'s stream; return its `seq`.

    `set_phase()` writes the stream's `phase_change` rows and is the only
    writer of a run's phase; this is how the loop writes the rows that are not
    transitions — a best-effort projection that failed, say. `kind` is free
    text because §2's column is a label rather than an enum, `level` is one of
    §2's two, and `payload` is the `detail`-row field: the text behind the
    summary (a crash's traceback, say), refused on a `narrative` row so the
    stream's two levels keep meaning what §2 says they mean.

    An unknown `run_id` is a caller bug and raises `ValueError`, the way
    `set_phase()` and `run_phase()` answer the same mistake — the foreign key
    would refuse the row anyway, but as an `IntegrityError` naming a
    constraint rather than the run that does not exist. `now` is epoch
    milliseconds for `at`, defaulting to the clock.
    """
    if level not in EVENT_LEVELS:
        raise ValueError(f"unknown event level {level!r}")
    if payload is not None and level != "detail":
        raise ValueError("payload is a detail-level field")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute("SELECT 1 FROM runs WHERE id = ?",
                        (run_id,)).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        return _append_event(conn, run_id, level, kind, summary, now,
                             payload=payload)


def park(conn, run_id, phase, note=None, candidate_sha=None, pr_url=None,
         now=None, approved_sha=None, pr_seen=None):
    """Park the live run `run_id` in `phase` and give its lease back.

    `[merge] approve = "human"`: the reviewer approved and the pre-merge
    verify passed, and a person now has to say "merge". The run is not over
    -- nothing failed and nothing merged, and the candidate it holds is the
    one the answer is about -- so unlike `release()` this stamps no
    `endedAt` and no outcome: `runs.phase` reads `awaiting_merge_approval`
    for as long as the run waits, which is what the acceptance criterion and
    `/attention` read. What it shares with `release()` is the lease half,
    written the same way and in the same transaction as the phase move:
    the ticket's pointer moves from `activeRunId` to `lastRunId`, so the
    ticket is free to be claimed again and the parked run stays reachable
    from it exactly as an ended one is.

    `candidate_sha` is the full sha of the candidate the park is about --
    the one the reviewer approved and the pre-merge verify passed -- stored
    as `runs.candidateSha` so the resume that follows an approval can hold
    the worktree to it rather than merge whatever it finds there.

    `pr_url` is the pull request `[merge] mode = "pr"` opened for the
    candidate before parking it, stored as `runs.prUrl` in the same
    transaction as the phase move: the URL is what the park is waiting on,
    so a reader never sees a run parked for a PR without knowing which.

    `approved_sha` is the sha the last independent judgement covered -- the
    reviewer's approval or the operator's release -- stored as
    `runs.approvedSha`. Under `mode = "pr"` it and `candidate_sha` part
    ways once a fix round moves the candidate: the resumed shepherd merges
    the candidate only at this sha, and reviews it again at any other.

    `pr_seen` is `(updated_at, threads, checks, review)` as the pull
    request read after the pass's own writes -- GitHub's `updatedAt`
    string, its review thread count, the head's checks rollup and the
    review decision -- written by `record_pr_seen()` in the same
    transaction (KO-362, KO-368), so the loop's per-tick reconcile knows
    what activity the pass has already answered. None records nothing.

    `phase` must be one of `PARKED_PHASES`; the sweep leaves those alone, so
    a run parked here is not reported dead for having no heartbeat. Parking
    a run that has already ended raises `RunEnded`, and an unknown `run_id`
    raises `ValueError`, both before any write.
    """
    if phase not in PARKED_PHASES:
        raise ValueError(f"{phase!r} is not a phase a run is parked in")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT ticketId FROM runs WHERE id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        (ticket_id,) = row
        # `set_phase()` is what refuses an ended run, with `RunEnded`.
        set_phase(conn, run_id, phase, note=note, now=now)
        if candidate_sha is not None:
            conn.execute("UPDATE runs SET candidateSha = ? WHERE id = ?",
                         (candidate_sha, run_id))
        if pr_url is not None:
            conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?",
                         (pr_url, run_id))
        if approved_sha is not None:
            conn.execute("UPDATE runs SET approvedSha = ? WHERE id = ?",
                         (approved_sha, run_id))
        if pr_seen is not None:
            record_pr_seen(conn, run_id, pr_seen)
        conn.execute(
            "UPDATE tickets SET activeRunId = NULL, lastRunId = ?"
            " WHERE id = ? AND activeRunId = ?",
            (run_id, ticket_id, run_id),
        )


def record_pr_seen(conn, run_id, seen, parked_only=False, facts_only=False):
    """Record what one read of the pull request run `run_id` is parked on
    saw: `seen` is `(updated_at, threads, checks, review)` -- GitHub's
    `updatedAt` string, the review-thread count, the head's checks rollup
    ("success", "pending", "failure") and the review decision
    ("approved", "changes_requested", "review_required"), each None when
    GitHub did not say -- written as `runs.prSeenAt`, `prSeenThreads`,
    `prSeenChecks` and `prSeenReview` in one statement. The loop's
    reconcile holds the first two against the next read to tell new
    review activity from its own (KO-362); `/attention`'s `pr_open` item
    carries the last three (KO-368). `parked_only` writes nothing to a
    run no longer in `awaiting_merge_approval`, for a caller that read
    the run outside the transaction it writes in. `facts_only` writes the
    checks rollup and review decision alone, leaving the activity mark
    (`prSeenAt`, `prSeenThreads`) as the last pass recorded it: the
    reconcile's read of an unchanged pull request refreshes the facts
    without moving what it holds the next read against. Joins the
    caller's transaction when one is open.
    """
    updated_at, threads, checks, review = seen
    guard = " AND phase = 'awaiting_merge_approval'" if parked_only else ""
    with _transaction(conn):
        if facts_only:
            conn.execute("UPDATE runs SET prSeenChecks = ?, prSeenReview = ?"
                         f" WHERE id = ?{guard}", (checks, review, run_id))
            return
        conn.execute("UPDATE runs SET prSeenAt = ?, prSeenThreads = ?,"
                     f" prSeenChecks = ?, prSeenReview = ? WHERE id = ?{guard}",
                     (updated_at, threads, checks, review, run_id))


def _json_list(field, values):
    """Encode a contract `string[]` field as the JSON text the schema stores.

    A bare `str` is rejected rather than encoded: it is the easy mistake here,
    it is iterable, and a JSON string is truthy where the empty list is not —
    so passing one would route an under-specced ticket to `ready`.
    """
    if isinstance(values, str):
        raise ValueError(f"{field} must be a list of strings, got {values!r}")
    items = list(values)
    bad = [v for v in items if not isinstance(v, str)]
    if bad:
        raise ValueError(f"{field} must contain only strings, got {bad[0]!r}")
    return json.dumps(items)


# The `ledger` table's two enums, transcribed from its CHECKs so a typo is a
# ValueError naming it here rather than an IntegrityError from SQLite.
LEDGER_KINDS = ("merge", "failure", "round", "adjudication", "intervention",
                "note")
LEDGER_SOURCES = ("loop", "operator")


def record_ledger(conn, run_id, kind, text, source="loop", now=None):
    """Append one entry of `kind` to run `run_id`'s ledger; return its id.

    The store's half of `board.ledger()`: the row lands here first, and the
    board comment the loop then posts is a projection of it, so the run's
    narrative survives a board that is down, cancelled or third-party. The
    ticket is the run's own (`runs.ticketId`), read here rather than passed,
    so a row can never name a ticket other than the one its run was claimed
    for. Joins a caller's `transaction()` when one is open -- an
    intervention's row and its ledger entry land together -- and otherwise
    is one of its own.
    """
    if kind not in LEDGER_KINDS:
        raise ValueError(f"unknown ledger kind {kind!r}")
    if source not in LEDGER_SOURCES:
        raise ValueError(f"unknown ledger source {source!r}")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"text must be non-empty, got {text!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute("SELECT ticketId FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            "INSERT INTO ledger (runId, ticketId, at, kind, text, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, row[0], now, kind, text, source))
    return cursor.lastrowid


# §2's severity union, transcribed. Checked here because `reviewRounds.findings`
# is a JSON document rather than rows, so no CHECK constraint stands behind it:
# this helper is the only place a severity typo can be caught before it changes
# a fingerprint and, through it, whether the review looks converged.
SEVERITIES = ("p0", "p1", "p2", "nit")

# The canonical record's separators. §2 writes the key as `path:line:severity`,
# but a colon is a legal character in a path, so joining on one would let
# `{"path": "a:1", "line": 2}` and `{"path": "a", "line": ...}` collide into
# the same record. The ASCII separators are not characters a reviewer cites a
# path by, which is what makes them unambiguous — but "not expected" is not
# "cannot happen", and `findings` is a decoded JSON document, so a path may
# carry any character at all. `_finding_keys()` therefore *rejects* them rather
# than trusting their absence: a path holding a separator could otherwise forge
# a record boundary and hash a round to another round's fingerprint. Rejecting
# keeps the encoding escape-free, so digests already stored stay valid.
_FIELD_SEP = "\x1f"   # ASCII unit separator
_RECORD_SEP = "\x1e"  # ASCII record separator

# A finding with no line is keyed at -1: absent has to be *some* value, and a
# sentinel outside the range of real line numbers keeps "the whole file" and
# "line 1" distinct instead of folding one into the other. It is only outside
# that range because `_finding_keys()` rejects non-positive lines, so no
# reviewer can hand us a -1 that collides with "no line" and makes two
# different findings fingerprint alike.
_NO_LINE = -1

# The fingerprint of a round that found nothing: sha256 of the empty canonical
# form, which is what the general path already computes for an empty set. Named
# because callers compare against it ("did this round find anything?") and
# pinned as a literal because it is written into a NOT NULL column and compared
# across rounds recorded by different releases — this value cannot drift.
EMPTY_FINGERPRINT = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def _finding_keys(findings):
    """Normalize `findings` to the set of `(path, line, severity)` keys.

    The key is §2's, and what it leaves out is the point: `message` and
    `criterion` are prose a reviewer rewrites every round, so including them
    would make an unchanged complaint look like a new one and hide exactly the
    non-convergence the fingerprint exists to catch.

    A **set**, so two findings sharing a key collapse into one. Under this key
    they are the same complaint about the same place, and duplicates would
    otherwise make the fingerprint depend on how many times the reviewer said
    it. It also keeps `findings_fingerprint()` and `findings_overlap()` reading
    the same canonical input, which is what makes them comparable at all.

    Raises `ValueError` for a finding missing `path` or `severity`, carrying a
    severity outside §2's union, citing a line below 1, or naming a path that
    contains one of the canonical form's ASCII separators — a fingerprint over
    malformed findings would be a number that compares fine and means nothing.
    Lines are 1-based, so a 0 or a negative is junk either way; rejecting it
    also keeps `_NO_LINE` unreachable as an explicit value. A separator inside
    a path is rejected for the sharper reason that the canonical form does not
    escape them: `[("a", 1, "p0"), ("b", 2, "p1")]` and the single finding
    `("a\x1f1\x1fp0\x1eb", 2, "p1")` would otherwise serialize to the same
    bytes and hash alike, which is §6 reading two rounds that share nothing as
    the same round twice.
    """
    keys = set()
    for finding in findings:
        try:
            path = finding["path"]
            severity = finding["severity"]
        except (TypeError, KeyError) as exc:
            raise ValueError(
                f"finding must carry a path and a severity, got {finding!r}"
            ) from exc
        if not isinstance(path, str) or not path:
            raise ValueError(f"finding path must be a non-empty string, got {path!r}")
        if _FIELD_SEP in path or _RECORD_SEP in path:
            raise ValueError(
                "finding path must not contain the canonical form's ASCII"
                f" separators, got {path!r}"
            )
        if severity not in SEVERITIES:
            raise ValueError(
                f"finding severity must be one of {SEVERITIES}, got {severity!r}"
            )
        line = finding.get("line")
        if line is None:
            line = _NO_LINE
        elif isinstance(line, bool) or not isinstance(line, int):
            raise ValueError(
                f"finding line must be an integer or absent, got {line!r}"
            )
        elif line < 1:
            raise ValueError(
                f"finding line must be a positive integer or absent, got {line!r}"
            )
        keys.add((path, line, severity))
    return keys


def _message_digest(finding):
    """A finding's message normalised for comparison: lower-cased, whitespace
    collapsed, digits kept -- the form `unparsed_path()` already digests, so
    a reworded complaint reads as new and a rewrapped one does not."""
    return " ".join(str(finding.get("message", "")).split()).lower()


def findings_fingerprint(findings):
    """Hash a review round's `findings` into its stable fingerprint.

    State-model §2: "hash of sorted (path:line:severity) tuples". `findings` is
    the decoded `reviewRounds.findings` list — mappings with `path`, an
    optional `line`, and a `severity`; any other keys are ignored.

    Sorted before hashing, so the order the reviewer happened to emit its
    findings in cannot change the answer: two rounds that raised the same
    complaints fingerprint identically, which is the whole mechanism §6's
    `review_stuck` trip condition reads. A round that found nothing hashes to
    `EMPTY_FINGERPRINT` rather than raising — zero findings is a `pass`, an
    ordinary outcome, not an error.

    Pure. The result is a 64-character sha256 hex digest, sized for
    `reviewRounds.findingsFingerprint`.
    """
    canonical = _RECORD_SEP.join(
        _FIELD_SEP.join((path, str(line), severity))
        for path, line, severity in sorted(_finding_keys(findings))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def findings_overlap(earlier, later):
    """How much two rounds' findings share, as a fraction in [0.0, 1.0].

    The Jaccard index over the same `(path, line, severity)` keys
    `findings_fingerprint()` hashes: shared keys divided by the keys either
    round raised. Two rounds sharing two of three findings each score
    `2/4 = 0.5` — the shared fraction of everything on the table, not of
    either round alone, so neither a reviewer that drops findings nor one that
    piles new ones on can inflate the number.

    §6 reads a fingerprint match as "the same round twice" and this as the
    softer signal next to it: a review that keeps re-raising most of its
    findings is not converging even when the fingerprints differ. The
    threshold is the supervisor's policy, not this function's.

    Identical inputs always score 1.0, including two rounds that both found
    nothing: the sets are equal, and a `pass` after a `pass` is not the caller's
    stuck-review question. Argument order does not matter — the measure is
    symmetric; the names only say how it is usually read.

    One exception to the key-only reading: when the two rounds together raise
    a *single* key, they overlap only if their messages agree once normalised
    (`_message_digest()`), else 0.0. A one-finding round is the common case
    for a small ticket, and one coarse key -- a file cited without a line --
    is all it takes for two different complaints to score 1.00 and end a
    converging run. With two or more keys the reworded-complaint reasoning
    above still holds and the plain Jaccard measure is kept; the fingerprint
    is untouched either way.
    """
    earlier_keys = _finding_keys(earlier)
    later_keys = _finding_keys(later)
    union = earlier_keys | later_keys
    if not union:
        return 1.0
    if len(union) == 1 and earlier and later:
        earlier_messages = {_message_digest(f) for f in earlier}
        later_messages = {_message_digest(f) for f in later}
        return 1.0 if earlier_messages == later_messages else 0.0
    return len(earlier_keys & later_keys) / len(union)


# §2's `reviewRounds.verdict` union, transcribed from the column's CHECK so a
# caller can map onto it without reading the DDL. The reviewer's own
# vocabulary is a different one (`APPROVE`/`REQUEST_CHANGES`, `PASS`/`FAIL`);
# translating it is the loop's job, not this module's.
ROUND_VERDICTS = ("pass", "changes_requested", "error")


def _document_argument(label, value):
    """`value` as the list a `reviewRounds` document column is written from.

    The contract's arguments are object *arrays*, and `list()` alone does not
    say so: it accepts any iterable, so `findings="prose"` becomes a document
    of five one-character findings and a mapping becomes a document of its
    keys. Both were written past the refusal they were supposed to hit, and
    the renderer can only show them as a row nothing should have stored. So
    the shape is checked before the coercion, and a string, a mapping, or
    anything that is not a sequence is a `ValueError` named for `label`.
    """
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError(
        f"{label} must be a list of objects, got {type(value).__name__}")


def _json_document(label, value):
    """`value` as the JSON text a `reviewRounds` document column stores.

    Strict on both sides of the encoder: a Python value it cannot serialize
    raises `TypeError`, and `allow_nan=False` turns the non-JSON floats it
    would otherwise emit into a `ValueError`. Both become the `ValueError`
    the caller's other refusals are, named for `label`, so a round that
    cannot be written as valid JSON is refused whole rather than stored as
    text the renderer cannot read back.
    """
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a JSON document: {exc}") from exc


def record_review_round(conn, run_id, round_number, verdict, reviewer_model,
                        findings=(), verification_results=(),
                        started_at=None, ended_at=None):
    """Persist one review round of `run_id` as a row; return its id.

    The write half of the fingerprint helpers above: `findings` is hashed
    into `findingsFingerprint` here rather than by the caller, because §6's
    stuck-review check compares digests across rounds recorded at different
    times, and a caller that computed its own could hash a different
    normalization and make two identical rounds look unlike. Hashing first
    also validates: `findings_fingerprint()` rejects a malformed finding
    before this opens a transaction, so a round is stored whole or not at all.

    `findings` and `verification_results` are the contract's object arrays,
    stored as the JSON documents the schema declares. Both are encoded here,
    before the transaction, for the same reason the fingerprint is: a value
    `json` cannot write (bytes, a set) or writes as something no reader can
    decode back (`NaN`, `Infinity` — legal to the encoder, not to JSON) is a
    `ValueError`, and nothing is stored. So is a value that is not a list at
    all: `"prose"` and `{...}` are iterable, and coercing them would store a
    document of characters or of keys instead of refusing the caller's
    mistake. The renderer reads these columns
    back with `json.loads()`; refusing here is what lets it trust them. A
    round that found nothing is an ordinary `pass` and stores `[]` against
    `EMPTY_FINGERPRINT`.

    `ended_at` defaults to NULL, which is the column's "still running"; a
    caller recording a finished round passes both stamps. `started_at`
    defaults to the clock because the column is NOT NULL and a round being
    recorded has certainly started.

    The run is checked inside the transaction for the module's usual reason: an
    unknown `run_id` is a caller bug and raises `ValueError` rather than a
    foreign-key `IntegrityError` from the driver. `UNIQUE (runId, round)` is
    left to the database — recording the same round twice is a bug this must
    not paper over by overwriting the first record of it.
    """
    if verdict not in ROUND_VERDICTS:
        raise ValueError(
            f"round verdict must be one of {ROUND_VERDICTS}, got {verdict!r}"
        )
    if (isinstance(round_number, bool) or not isinstance(round_number, int)
            or round_number < 1):
        raise ValueError(
            f"round must be a positive integer, got {round_number!r}"
        )
    findings = _document_argument("findings", findings)
    fingerprint = findings_fingerprint(findings)
    findings_json = _json_document("findings", findings)
    results_json = _json_document(
        "verification results",
        _document_argument("verification results", verification_results))
    if started_at is None:
        started_at = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute(
            "SELECT 1 FROM runs WHERE id = ?", (run_id,)
        ).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            "INSERT INTO reviewRounds (runId, round, verificationResults,"
            " verdict, findings, findingsFingerprint, reviewerModel,"
            " startedAt, endedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, round_number, results_json, verdict, findings_json,
             fingerprint, reviewer_model, started_at, ended_at),
        )
    return cursor.lastrowid


# --- the supervisor sweep's strike tally --------------------------------------
# A run whose loop has crashed stops heartbeating, but so does one whose host
# is briefly wedged, so liveness is not a single sample: the sweep records what
# it saw and only the second consecutive silent sighting is evidence. The
# counting lives here rather than in the sweep because it is a read-then-write
# over a store table, and two sweeps racing on one target must serialize on it
# the way every other writer in this module does.


def record_strike(conn, run_id, stale, heartbeat, now=None):
    """Record one sweep's liveness sighting of run `run_id`; return its strikes.

    `stale` is the sweep's verdict on this run's heartbeat, not a threshold
    this decides: the sweep owns how old is too old (and 5/5 will make that
    configurable), and this owns only how many sightings in a row say so.

    A silent run's tally goes up by one and the row remembers the sweep that
    last touched it. A run seen alive drops its row and answers 0 -- the
    strikes a sweep counts are consecutive, so one heartbeat clears the count
    rather than leaving a run one old sighting away from tripping forever.

    Which is why `heartbeat` -- the run's `lastHeartbeat`, the timestamp the
    caller's verdict was reached on -- is compared against the `lastSeen` of
    the strike already on file. A sighting is only the *next* consecutive one
    if the run has been silent throughout; a run that answered after the last
    strike was recorded and then went quiet again has proved itself alive in
    between, and starts over at one however few sweeps saw it do so. Counting
    on sightings alone makes the tally consecutive in sweeps rather than in
    silence, and a run heartbeating just slower than the sweep interval trips
    while alive -- exactly the false positive two strikes exist to prevent.

    An unknown `run_id` is a caller bug and raises `ValueError`, as everywhere
    else here. `now` is epoch milliseconds for `lastSeen`, defaulting to the
    clock; a sweep passes its own so every run in one pass is stamped with the
    one time it was taken.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute(
            "SELECT 1 FROM runs WHERE id = ?", (run_id,)
        ).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        if not stale:
            conn.execute("DELETE FROM sweepStrikes WHERE runId = ?", (run_id,))
            strikes = 0
        else:
            row = conn.execute(
                "SELECT strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
                (run_id,)
            ).fetchone()
            if row is None or heartbeat > row[1]:
                # Nothing on file, or the run answered after what is: either
                # way this is the first sighting of the silence it is in now.
                strikes = 1
            else:
                strikes = row[0] + 1
            conn.execute(
                "INSERT INTO sweepStrikes (runId, strikes, lastSeen)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT (runId) DO UPDATE SET strikes = excluded.strikes,"
                " lastSeen = excluded.lastSeen",
                (run_id, strikes, now),
            )
    return strikes


def record_supervisor_heartbeat(conn, pid, started_at, now=None):
    """Record one completed pass of the supervisor `pid`; return its passes.

    The supervisor is identified by `(pid, started_at)` rather than pid alone
    because pids are reused: a supervisor started tomorrow with yesterday's
    pid is a different watcher, and folding its passes into the old row would
    make the old one look like it never died. The first call inserts the row
    with one pass; every later call bumps `lastBeat` and the count. `now` is
    epoch milliseconds, defaulting to the clock; the loop passes the instant
    its sweep ran so the beat and the sweep it vouches for agree. Every beat
    stamps `host` with this machine's hostname, so a store read elsewhere can
    say which machine the watcher is on.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        conn.execute(
            "INSERT INTO supervisorHeartbeats"
            " (pid, startedAt, lastBeat, passes, host)"
            " VALUES (?, ?, ?, 1, ?)"
            " ON CONFLICT (pid, startedAt) DO UPDATE SET"
            "   lastBeat = excluded.lastBeat, passes = passes + 1,"
            "   host = excluded.host",
            (pid, started_at, now, socket.gethostname()),
        )
        return conn.execute(
            "SELECT passes FROM supervisorHeartbeats"
            " WHERE pid = ? AND startedAt = ?", (pid, started_at)).fetchone()[0]


def latest_supervisor_heartbeat(conn):
    """The newest supervisor heartbeat, or None when no supervisor has beaten.

    `(pid, started_at, last_beat, passes, host)` for the row whose `lastBeat`
    is most recent: the one supervisor that could still be alive, since any
    other process's row stopped moving before it. `host` is None for a beat
    written before the column existed. Read-only, so `--report` can ask it
    of a store a live supervisor is writing to.
    """
    row = conn.execute(
        "SELECT pid, startedAt, lastBeat, passes, host"
        " FROM supervisorHeartbeats"
        " ORDER BY lastBeat DESC, startedAt DESC LIMIT 1").fetchone()
    return tuple(row) if row is not None else None


def record_loop_restart(conn, project_id, sha, now=None):
    """Note that the loop is about to re-exec itself from `sha`; return the id.

    Written by the loop just before `os.execv()` replaces it, so a restart that
    never comes back has left something a reader can see: the exec itself
    prints nothing once it has failed, and every gate before it had passed.
    `now` is epoch milliseconds for `at`, defaulting to the clock. The row is
    the question "did the loop return?"; `record_loop_return()` and a claim
    are the two ways of answering yes, `unreturned_loop_restarts()` is how the
    sweep asks.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        cursor = conn.execute(
            "INSERT INTO loopRestarts (projectId, sha, at) VALUES (?, ?, ?)",
            (project_id, sha, now))
        return cursor.lastrowid


def record_loop_return(conn, project_id, now=None):
    """The loop's exit note: every open restart of `project_id` came back.

    Called where the loop prints "no ready tickets" and exits clean -- the one
    way a loop that restarted successfully can end without ever claiming, and
    so without a heartbeat to vouch for it. Stamps `returnedAt` on every
    restart row of the project not already returned, and returns how many it
    stamped: zero for a loop that was not restarted, which is the common case
    and not an error. `now` is epoch milliseconds, defaulting to the clock.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        return conn.execute(
            "UPDATE loopRestarts SET returnedAt = ?"
            " WHERE projectId = ? AND returnedAt IS NULL",
            (now, project_id)).rowcount


def unreturned_loop_restarts(conn, grace_ms, now=None):
    """Restarts older than `grace_ms` no loop activity has followed, once each.

    A restart row counts as unreturned when it has no `returnedAt`, no run of
    its project has a heartbeat newer than it -- `claim()` stamps a fresh
    run's heartbeat at its claim time, so a claim is a heartbeat here -- and
    it is at least `grace_ms` old at `now`, the time the exec is allowed to
    take before its silence means something. Each row is returned as
    `(id, project_id, sha, age_ms)` exactly once: this stamps `reportedAt` on
    what it returns, in the caller's transaction when there is one, so the
    sweep that prints the line is the sweep that records it and the next pass
    is quiet about the same restart. A restart younger than the grace is not
    returned and not stamped; it is asked about again on the next pass.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        rows = conn.execute(
            "SELECT id, projectId, sha, at FROM loopRestarts"
            " WHERE returnedAt IS NULL AND reportedAt IS NULL"
            "   AND at <= ?"
            "   AND NOT EXISTS (SELECT 1 FROM runs"
            "                   WHERE runs.projectId = loopRestarts.projectId"
            "                     AND runs.lastHeartbeat > loopRestarts.at)"
            " ORDER BY at, id", (now - grace_ms,)).fetchall()
        conn.executemany(
            "UPDATE loopRestarts SET reportedAt = ? WHERE id = ?",
            [(now, row[0]) for row in rows])
        return [(row[0], row[1], row[2], now - row[3]) for row in rows]


from .operate import (  # noqa: E402,F401 - re-export after the run API it calls
    APPROVED_RESUME_PHASE,
    ENDED_PHASES,
    FULL_SHA,
    GATE_CONFLICT_REASON,
    INTERVENTION_ACTIONS,
    INTERVENTION_SOURCES,
    INTERVENTION_TRIGGERS,
    OUTCOME_CLASSES,
    PARKED_PHASES,
    RESUMABLE_PHASES,
    RESUMABLE_WORK_PHASES,
    RUN_PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    ApproveRefused,
    GuidanceNotAccepted,
    RepointRefused,
    RequeueRefused,
    ResumeRefused,
    _release_parked,
    approve,
    babysit,
    is_gate_conflict,
    record_intervention,
    release,
    repoint,
    requeue,
    resume,
)
from .tickets import (  # noqa: E402,F401 - re-export after `_json_list`
    STATE_GRAPHS,
    TICKET_STATUSES,
    TICKET_TRANSITIONS,
    IllegalTransition,
    Pickability,
    ensure_project,
    mirror_ticket,
    pickable,
    pickable_tickets,
    render_state_graph,
    render_state_graph_section,
    transition,
    walk_ticket,
)

if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--state-graph"]:
        sys.exit("usage: python3 store.py --state-graph")
    print("\n".join(render_state_graph_section(name, globals()[table])
                    for name, table in STATE_GRAPHS), end="")

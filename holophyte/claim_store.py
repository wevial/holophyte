"""holophyte.claim_store: a store-mode project claims from the store
(Phase 3 stage 3).

In `[board] mode = "store"` the store owns the queue: `claim_from_store()`
takes the first `store.read.claimable()` row, admits it at its revision R
with `_admit_ticket()` unchanged, reads that one issue back from the board
(`_confirm_on_board()`), mirrors what it heard, and claims only while the
ticket is still at R -- `store.claim(expected_revision=R)` asserts it
under the claim's own transaction. A revision that moved since admission
admits the ticket again, at most `READMIT_LIMIT` times an ask. A board
that cannot be asked claims nothing (KO-351). `store_mode()` is the one
predicate every store-mode branch of the claim, the scheduler and the
sweep is gated on; mirror mode never reaches this module.

The board is read once a pass, not once an ask: `sync_board()` mirrors
the ready listing into the store, throttled on the project's
`boardAskedAt`, and the scheduler counts `claimable()` rather than the
listing (`pool_handoff.listing()`).
"""
from time import time

import store
import store.read
from holophyte import freshness
from holophyte.board import (
    body_problem,
    foreign_lease_holders,
    lease_host,
    mirror_task,
    on_pull_request,
)
from holophyte.config_tables import board_mode
from holophyte.freshness import skip_labelled_stale
from holophyte.redact import safe_print as print

# How often one ask admits a candidate again after its revision moved
# before skipping it: a board edited faster than admission runs must not
# hold the loop on one ticket.
READMIT_LIMIT = 2

# `_candidate()`'s answers besides a claim: skip the candidate, admit it
# again at its new revision, or end the ask with nothing claimed.
SKIP, READMIT, STOP = "skip", "readmit", "stop"


class _BoardDown:
    """`claim_from_store()`'s task when the board could not be read back:
    falsy, as an empty queue is, and its own object, so the loop can say
    the board failed rather than that the queue drained."""

    def __bool__(self):
        return False


BOARD_DOWN = _BoardDown()

# What `sync_board()` answers: the board was not asked (throttled, held or
# the Linear budget low), its listing was mirrored, or it could not be.
NOT_ASKED, SYNCED, FAILED = "not asked", "synced", "failed"


def store_mode(target):
    """Whether `target`'s board is in store mode: `[board] mode = "store"`."""
    # `provider.store_mode` (queued pushes, notes, the listing) must match
    # this: `board_for()` sets it from the same `board_mode(target)`.
    return board_mode(target).mode == "store"


def announce(target):
    """The loop's one startup line in store mode; nothing in mirror mode."""
    if store_mode(target):
        print("[holo2] board mode store: claims come from the store's queue")


def sync_board(target, conn, project, provider, now=None,
               min_interval_ms=None):
    """One store-mode board sync: the ready listing mirrored into the store
    (`dispatch._mirror_queue()`, blockers included), unless the project's
    board was asked within `min_interval_ms` -- the loop's `tick_sec`, the
    sweep's `board_ask_sec` -- on the shared `boardAskedAt` stamp, which is
    written before the ask as `board_ready()` writes it. A held project or
    a low Linear budget is not asked and not stamped. Answers `NOT_ASKED`,
    `SYNCED`, or `FAILED` when the listing could not be mirrored.
    `states()` stays the host sweep's (`observe_board()`); the claim reads
    its candidate back on its own.
    """
    from holophyte.admission import held_line
    from holophyte.dispatch import _mirror_queue
    from holophyte.supervisor import linear_budget_low
    now = int(time() * 1000) if now is None else now
    if min_interval_ms is not None:
        (asked_at,) = conn.execute(
            "SELECT boardAskedAt FROM projects WHERE id = ?",
            (project,)).fetchone()
        if asked_at is not None and now - asked_at < min_interval_ms:
            return NOT_ASKED
    if held_line(conn, project) or linear_budget_low():
        return NOT_ASKED
    with store.transaction(conn):
        store.stamp_board_ask(conn, project, now)
    if _mirror_queue(target, conn, project, provider) is None:
        return FAILED
    return SYNCED


def superseded(conn, ticket_id, task):
    """Whether `task`, built from the store at `store_revision`, was judged
    on a revision the ticket has since left (Phase 3 stage 3): a refusal of
    it writes nothing to the board, and the claim admits the ticket again.
    False for any other task."""
    at = task.get("store_revision")
    if at is None:
        return False
    row = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                       (ticket_id,)).fetchone()
    return row is not None and row[0] != at


def task_of(row):
    """The task a `ClaimableTicket` is admitted as: the stored body parsed
    as a board parses it, with the stored board fields over it and
    `store_revision` naming the revision it was read at.

    The contract lists are the row's own, the ones the claim freezes. A
    stored body that is empty is a board that sent none (a real board's
    empty description is never `ready`), so it is not judged, as
    `body_problems()` does not judge a task without a body.
    """
    from provider import parse_body
    task = parse_body(row.linearIdentifier, row.body)
    commands = row.verificationCommands
    task.update(
        issue_id=row.linearIssueId, title=row.title,
        body=row.body or None,
        criteria=list(row.acceptanceCriteria),
        verify=commands[0] if commands else None,
        priority=row.priority, labels=list(row.labels), url=row.url,
        board_state=row.boardState, filed_at=row.filedAt,
        store_revision=row.revision)
    if row.timeBoxMs is not None:
        task["budget_min"] = row.timeBoxMs // 60000
    return task


def claim_from_store(target, conn, project_id, provider, order, skip, seen):
    """`_claim_next()` in store mode: the same `(task, ticket_id, run_id)`
    answer, the task being the board's own read of the claimed issue.

    Candidates come from the store's queue in `order`, never from a board
    listing, so an empty queue parks nothing (`_park_unlisted()` is not
    reached). A refused candidate is added to `skip`. A board that could
    not be asked answers `BOARD_DOWN` as the task.
    """
    from holophyte.admission import held_line
    readmitted = {}
    while True:
        line = held_line(conn, project_id)
        if line:
            print(line)
            return None, None, None
        row = next((r for r in store.read.claimable(conn, project_id, order)
                    if r.linearIdentifier not in skip), None)
        if row is None:
            return None, None, None
        answer = _candidate(target, conn, project_id, provider, row, seen)
        if answer == STOP:
            return BOARD_DOWN, None, None
        if answer == SKIP:
            skip.add(row.linearIdentifier)
        elif answer == READMIT:
            _readmit(conn, row, readmitted, skip)
        else:
            return answer


def _candidate(target, conn, project_id, provider, row, seen):
    """Admit `row` at its revision, read it back from the board and claim
    it there; the claim's answer, or `SKIP`, `READMIT` or `STOP`."""
    from holophyte.claim import HELD, _admit_ticket, _claim_run, claimed_run
    ticket_id = _admit_ticket(target, conn, project_id, provider, task_of(row),
                              seen)
    if ticket_id is None:
        # A refusal judged on a revision the board has since replaced is
        # not the ticket's (its board writes were dropped): judge it again.
        return READMIT if _revision(conn, row.id) != row.revision else SKIP
    live, verdict = _confirm_on_board(target, conn, project_id, provider, row)
    if live is None:
        return verdict
    if _revision(conn, ticket_id) != row.revision:
        return READMIT
    try:
        run_id = _claim_run(target, conn, project_id, provider, live,
                            ticket_id, seen, expected_revision=row.revision)
    except store.RevisionMoved:
        return READMIT
    if run_id is HELD:
        return SKIP
    if run_id is not None:
        live = dict(live, _run=claimed_run(target, live, conn, run_id,
                                           provider))
        freshness.carry_warning(conn, run_id, live)
    return live, ticket_id, run_id


def _confirm_on_board(target, conn, project_id, provider, row):
    """Read the admitted candidate back from the board, one issue, and
    mirror what it holds now; answer `(live, None)` to go on to the claim,
    or `(None, SKIP)` or `(None, STOP)`.

    A raise is no evidence and ends the ask: nothing is claimed while the
    board is unreachable. Gone and completed are skipped, the sweep's and
    `_reconcile_mirror()`'s to settle; so is an issue labelled `stale` or
    leased by another writer, which only the live labels show (the stored
    labels are the board-owned ones). Any other answer is mirrored with its
    column (the stale skip's mirror too), which writes the next revision
    when a board-owned field moved.
    """
    identifier = row.linearIdentifier
    try:
        live = provider.fetch_task(row.linearIssueId)
    except Exception as e:  # noqa: BLE001 - no evidence, no claim
        print(f"[holo2] {identifier} could not be read back from the board"
              f" ({e}); nothing is claimed while the board is unreachable")
        return None, STOP
    column = "ready" if live is None else live.get("column", "ready")
    if live is None or column is None:
        print(f"[holo2] {identifier} is"
              f" {'gone from' if live is None else 'completed on'} the board;"
              " skipping it")
        return None, SKIP
    if skip_labelled_stale(conn, project_id, live):
        return None, SKIP
    others = foreign_lease_holders(live.get("labels"), lease_host(target))
    if others:
        print(f"[holo2] {identifier} is leased by {others[0]} on the board;"
              " skipping it")
        return None, SKIP
    problem = body_problem(live, target.path,
                           on_pull_request=on_pull_request(conn, project_id,
                                                           live))
    mirror_task(conn, project_id, live, specced=problem is None)
    if problem:
        print(f"[holo2] {identifier} skipped: {problem}")
        return None, SKIP
    return live, None


def _revision(conn, ticket_id):
    (revision,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                               (ticket_id,)).fetchone()
    return revision


def _readmit(conn, row, readmitted, skip):
    """Count one readmission of `row`, and skip it past `READMIT_LIMIT`;
    one line either way, naming both revisions."""
    identifier = row.linearIdentifier
    readmitted[identifier] = readmitted.get(identifier, 0) + 1
    now = _revision(conn, row.id)
    moved = f"{identifier}: revision moved from {row.revision} to {now}"
    if readmitted[identifier] > READMIT_LIMIT:
        skip.add(identifier)
        print(f"[holo2] {moved} since it was admitted, {READMIT_LIMIT} times"
              " this ask; skipping it")
        return
    print(f"[holo2] {moved} since it was admitted; asking the queue again")

from time import time

import store
import store.board
import store.read
from holophyte.board.projection import (
    body_problem,
    foreign_lease_holders,
    lease_host,
    mirror_task,
    on_pull_request,
)
from holophyte.config.config_tables import board_mode, story_config
from holophyte.redact import safe_print as print
from holophyte.review import freshness
from holophyte.review.freshness import skip_labelled_stale
from holophyte.story import story_claim

# A board edited faster than admission runs must not hold the loop on one ticket.
READMIT_LIMIT = 2

SKIP, READMIT, STOP = "skip", "readmit", "stop"


class _BoardDown:
    def __bool__(self):
        return False


BOARD_DOWN = _BoardDown()

NOT_ASKED, SYNCED, FAILED = "not asked", "synced", "failed"


def store_mode(target):
    # `board_for()` sets `provider.store_mode` from the same `board_mode()`.
    return board_mode(target).mode == "store"


def announce(target):
    if store_mode(target):
        print("[holo2] board mode store: claims come from the store's queue")


def sync_board(target, conn, project, provider, now=None,
               min_interval_ms=None):
    from holophyte.admission import held_line
    from holophyte.host.supervisor import linear_budget_low
    from holophyte.loop.dispatch import _mirror_queue
    if getattr(provider, "native", False) is True:
        with store.transaction(conn):
            store.board.resolve_dependencies(conn, project)
        return SYNCED
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
    at = task.get("store_revision")
    if at is None:
        return False
    row = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                       (ticket_id,)).fetchone()
    return row is not None and row[0] != at


def task_of(row):
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
    from holophyte.admission import held_line
    readmitted = {}
    if not held_line(conn, project_id) and _read_back_waiting(
            target, conn, project_id, provider) == STOP:
        return BOARD_DOWN, None, None
    while True:
        line = held_line(conn, project_id)
        if line:
            print(line)
            return None, None, None
        row = next((r for r in store.read.claimable(conn, project_id, order)
                    if r.linearIdentifier not in skip), None)
        if row is None:
            return None, None, None
        refused = story_claim.refusal(target, conn, row.id)
        if refused:
            print(f"[holo2] {row.linearIdentifier} skipped: {refused}")
            skip.add(row.linearIdentifier)
            continue
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
    from holophyte.loop.claim import HELD, _admit_ticket, _claim_run, claimed_run
    ticket_id = _admit_ticket(target, conn, project_id, provider, task_of(row),
                              seen)
    if ticket_id is None:
        # A refusal on a revision the board has since replaced is not the ticket's.
        return READMIT if _revision(conn, row.id) != row.revision else SKIP
    live, verdict = _confirm_on_board(target, conn, project_id, provider, row)
    if live is None:
        return verdict
    if _revision(conn, ticket_id) != row.revision:
        return READMIT
    try:
        run_id = _claim_run(target, conn, project_id, provider, live,
                            ticket_id, seen, expected_revision=row.revision,
                            max_parallel=story_config(target).max_parallel)
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
    identifier = row.linearIdentifier
    try:
        live = provider.fetch_task(row.linearIssueId)
    except Exception as e:
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
    blocked_by = live.get("blocked_by")
    ticket_id = mirror_task(conn, project_id, live, specced=problem is None,
                            depends_on=blocked_by)
    if problem:
        print(f"[holo2] {identifier} skipped: {problem}")
        return None, SKIP
    if blocked_by:
        from holophyte.loop.dispatch import _wait_on_blockers
        _wait_on_blockers(conn, ticket_id)
    refused = story_claim.refusal(target, conn, ticket_id)
    if refused:
        print(f"[holo2] {identifier} skipped at the read-back: {refused}")
        return None, SKIP
    if blocked_by:
        print(f"[holo2] {identifier} gained a blocker on the board since the"
              " last sync; waiting on it")
        return None, SKIP
    return live, None


def _read_back_waiting(target, conn, project_id, provider):
    for row in story_claim.waiting_children(conn, project_id):
        if _confirm_on_board(target, conn, project_id, provider,
                             row)[1] == STOP:
            return STOP
    return None


def _revision(conn, ticket_id):
    (revision,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                               (ticket_id,)).fetchone()
    return revision


def _readmit(conn, row, readmitted, skip):
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

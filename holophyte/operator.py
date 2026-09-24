"""Operator commands and startup: probe before claim, record route failures."""
import getpass
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

import store
import store.read
import store.tickets
from holophyte.agent_routes import reset, routes
from holophyte.agents import (
    probe_diagnostic,
    probe_implementer,
    startup_routes,
)
from holophyte.board import release_lease_label
from holophyte.claim import _claim_next
from holophyte.config_tables import loop_config, report_config
from holophyte.findings import commit_findings
from holophyte.gates import sh
from holophyte.pool import scheduler
from holophyte.pool_handoff import _fetch_main, _ff_main, _prepare_reexec  # noqa: F401
from holophyte.reconcile import (
    CloseRefused,
    _reconcile_at_startup,
    _reconcile_pull_requests,
    close_out_landed,
)
from holophyte.reexec import reexec_self
from holophyte.report import migration_header, report_lines
from holophyte.runs import open_store
from holophyte.startup import banner
from holophyte.supervisor import linear_budget_low, supervisor_liveness_line
from store import operator_notes

BABYSIT_DEFAULT_NOTE = "sent back to the babysitter"

EXEC = os.execv  # Replace after a self-merge; tests observe through this seam.


def self_hosted(target):
    """Whether the target is the factory checkout, requiring exec after merge."""
    return Path(__file__).resolve().parent.parent == target.path.resolve()


def main(target, provider):
    """Probe before claiming, then run serially or schedule worker children."""
    banner(target)
    reset(target)
    try:
        knobs = loop_config(target)
        if not startup_routes(target, provider, probe_implementer,
                              activate=knobs.workers == 1):
            return 1
        run = _serial if knobs.workers == 1 else scheduler
        return run(target, provider, knobs)
    except store.SchemaNewer as moved:
        reexec_self(_schema_reason(moved), EXEC)
    finally:
        reset(target)


def _record_startup_probe(target, provider, probe):
    """Record failure or clear an outage when startup succeeds."""
    from time import time

    from store import launch_backoff

    if probe.ok and not target.store_path.exists():
        return
    conn = open_store(target)
    try:
        project = store.ensure_project(conn, provider.team, target.path)
        if probe.ok:
            launch_backoff.clear(conn, project)
        else:
            launch_backoff.failure(conn, project, probe_diagnostic(target, probe),
                                   int(time() * 1000), pending=True)
    finally:
        conn.close()


def _serial(target, provider, knobs):
    """Claim and dispatch serially; `worker()` runs the same phases once."""
    from holophyte.dispatch import (
        PARKED,
        SWEPT,
        _dispatch,
        _mirror_queue,
        _startup_sweep,
    )

    restart_after_merge = self_hosted(target)
    stop_on_failure = knobs.stop_on_failure
    order = knobs.order
    # Preserve a nonzero exit when continuing past failures.
    failed = False
    conn = open_store(target)
    try:
        # The team name keys the project until the provider resolves its id.
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        seen = _startup_sweep(target, conn)
        _reconcile_at_startup(target, conn, project, provider)
        # Refused tickets remain on the board's ready list. Remember them so
        # a blocked head-of-queue ticket cannot starve the tickets behind it.
        skip = set()
        first_pass = True
        while True:
            # A move `open()` reads needs no restart here: only the pool
            # re-executes for one, to hand its workers to the new build.
            moved = _schema_move(target)
            if moved and not moved.readable:
                _reexec(target, conn, project, moved.reason)
                return
            # Before the claim: a pull request a person merged since the
            # last pass ships its parked run here (KO-359). The first pass
            # asked at startup, before the mirror was repaired.
            if not first_pass:
                # A ticket sent back to the babysitter for new review
                # activity (KO-362) may be one this pass parked and put in
                # `skip`; it is ready again, and this pass claims it.
                skip -= _reconcile_pull_requests(target, conn, project,
                                                 provider)
            first_pass = False
            _mirror_queue(target, conn, project, provider)
            # The claim spends the mirror's listing. Check its budget after
            # mirroring: that request (including a caught 429) may have pushed
            # the budget below its tenth. Stop before a refused claim; the
            # supervisor waits for the reset before restarting (KO-434).
            if linear_budget_low():
                store.record_loop_return(conn, project)
                print("[holo2] the ready listing waits for the budget's"
                      " reset; done.")
                return 1
            task, ticket_id, run_id = _claim_next(target, conn, project,
                                                  provider, order, skip, seen)
            if not task:
                # The exit note, in the store before it is on the terminal:
                # a loop that was re-exec'd and found nothing to claim ends
                # here without ever heartbeating, and this is what tells the
                # sweep the restart came back.
                store.record_loop_return(conn, project)
                print("[holo2] Linear has no ready tickets. done.")
                return 1 if failed else None
            if run_id is None:
                return
            merged = _dispatch(target, conn, run_id, provider, task, ticket_id)
            if merged is PARKED:
                # An approved candidate waiting for a person: not a failure,
                # so neither the stop nor the exit status is spent on it.
                # Its ticket is `blocked_on_operator`, which the claim path
                # refuses, so skipping it is only cheaper than refusing it.
                skip.add(task["id"])
                print(f"[holo2] {task['id']} parked awaiting merge approval;"
                      " continuing to the next ready ticket")
                continue
            if merged is SWEPT:
                # The sweep closed the run out and the loop honoured it by
                # stopping the turn; the ticket's mirror says what the sweep
                # left it saying, so it is not offered again this pass, and
                # a failure the sweep already counted is not counted twice.
                skip.add(task["id"])
                outcome = conn.execute("SELECT outcome FROM runs WHERE id = ?",
                                       (run_id,)).fetchone()[0]
                print(f"[holo2] {task['id']} ended ({outcome}); continuing"
                      " to the next ready ticket")
                continue
            if not merged:
                # The regenerated window stays uncommitted, like the preserved
                # branch it describes: a human closes both out. Nonzero so the
                # shell — and anything supervising it — sees the failure.
                if stop_on_failure or routes(target).failed:
                    return 1  # stop on first failure; ticket stays In Progress
                # `[loop] stop_on_failure = false`: the run is closed out
                # exactly as above, and the loop goes on to the next ready
                # ticket. The failed one is skipped for the rest of this
                # pass -- its mirror is `in_flight`, so the claim path would
                # refuse it anyway, but not offering it again is cheaper than
                # refusing it and the print is one line about the failure
                # rather than two.
                failed = True
                skip.add(task["id"])
                print(f"[holo2] {task['id']} failed; continuing to the next"
                      " ready ticket (stop_on_failure = false)")
                continue
            commit_findings(target,
                            f"Complete task {task['id']}: {task['title']}")
            if restart_after_merge:
                # Terminal run, released lease: restart with the merged code.
                _reexec(target, conn, project)
                return  # only a test's EXEC returns
    finally:
        conn.close()


def _schema_reason(moved):
    return (f"store schema moved to {moved.version} under this process;"
            " re-executing")


class SchemaMove(NamedTuple):
    reason: str
    readable: bool  # `open()` accepts the moved store on this build


def _schema_move(target):
    """Probe at the pass boundary before claiming or spawning more work.

    A `SchemaMove` for a store above this build's version, whether `open()`
    refuses it or reads it from its migrate note's floor; None otherwise."""
    try:
        conn = store.open(target.store_path)
    except store.SchemaNewer as moved:
        return SchemaMove(_schema_reason(moved), False)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    if version <= store.SCHEMA_VERSION:
        return None
    return SchemaMove(f"store schema moved to {version} under this process"
                      " and is readable by this build; re-executing", True)


def _reexec(target, conn, project, reason=None, *, prepared_sha=None,
            can_ff=None, worker_pids=()):
    """Update and exec; a schema move with workers asks the caller to drain."""
    from holophyte.pool_handoff import factory_checkout

    sha = prepared_sha or sh(["git", "rev-parse", "--short", "HEAD"],
                             factory_checkout())
    if can_ff is None:
        can_ff, schema_moves = _prepare_reexec(target, worker_pids)
        if schema_moves and worker_pids:
            return False
    if can_ff:
        _ff_main(target)
    arriving = sh(["git", "rev-parse", "--short", "HEAD"], factory_checkout())
    store.record_loop_restart(conn, project, json.dumps({
        "leaving": sha, "arriving": arriving}))
    conn.close()
    reason = reason or "merged a change to the factory itself"
    reexec_self(f"{reason}; re-executing at {arriving} (leaving {sha})", EXEC)


def report(target, conn=None, out=None, now=None):
    """Print the target store's estimate-vs-actual table. Returns nothing.

    `--report`'s whole body: it reads rows and prints them, so no ticket is
    claimed, no worktree is cut and no provider is imported -- which is what
    makes it safe to run against the store of a loop that is still working.

    An older store is refused until the loop or serve daemon migrates it.
    A target with no store at all is not created for the sake of an empty
    table; it is reported.

    Below the table, one line naming the `[report] findings` mode, so an
    operator can see whether this target has opted into rendering
    FINDINGS.md at close-out (`repo`) or not (`none`), then one on the target's
    supervisor -- see `supervisor_liveness_line()`, always last. `now` is
    the clock the heartbeat's age is taken against, injectable so a test
    can place a beat in time.
    """
    out = out or sys.stdout
    if conn is None and not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return
    owned = conn is None
    conn = conn if conn is not None else store.open(target.store_path, migrate=False)
    try:
        for line in migration_header(conn):
            print(line, file=out)
        print("\n".join(report_lines(conn, target)), file=out)
        print(f"findings: {report_config(target).findings}", file=out)
        print(supervisor_liveness_line(target, conn, now), file=out)
    finally:
        if owned:
            conn.close()


def requeue(target, identifier, note, out=None, provider=None):
    """Put the failed ticket `identifier` back in the queue. Returns nothing.

    `--requeue`'s whole body, and off every other mode's write path: it opens
    the store, does `store.requeue()`'s one transaction, prints the requeued
    line and exits. The identifier is the Linear one (`KO-n`), resolved in
    this target's store; an identifier the store has not mirrored, or one it
    holds more than once, is a `SystemExit` naming it, as is every refusal
    `store.requeue()` makes -- and in all of those nothing is written. A
    target with no store has nothing to requeue and says so the same way.

    With a `provider`, the board lease label comes off too (KO-351): this
    writer's `holo:HOST`, taken off before the store's transaction makes
    the ticket claimable, and only while the store names no other live run
    on the ticket, so a claim that follows finds its own label untouched
    whatever the order of the two. Best-effort: the
    failed run's close-out should already have removed it, and a board that
    was down then gets one more chance here; one still down leaves a label
    this writer's next claim treats as stale.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        failed_run = _requeue_candidate(conn, ticket_id)
        if failed_run is not None:
            run_id, pr_url = failed_run
            release_lease_label(target, conn, ticket_id, provider, run_id)
            if pr_url:
                # The console's row keeps its link while the ticket waits:
                # the branch is still open as a pull request, and the next
                # run adopts it rather than opening a second (KO-407).
                note = (f"{note}\n\nThe failed run's branch is still open"
                        f" as {pr_url}")
        try:
            run_id = store.requeue(conn, ticket_id, note)
        except (store.RequeueRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} requeued after run {run_id}", file=out)
    finally:
        conn.close()


def _requeue_candidate(conn, ticket_id):
    """The failed run `store.requeue()` would requeue `ticket_id` after, or
    None when it would refuse: a read of the same rows, made first so the
    run's lease label can come off while the ticket is still `in_flight`.
    `store.requeue()` re-reaches the verdict inside its own transaction.
    Returns `(run_id, pr_url)` -- the pull request the failed run left open
    (`runs.prUrl`, None when it opened none) rides along so the requeue
    note can name it."""
    ticket = store.read.ticket_by_id(conn, ticket_id)
    if ticket is None or ticket.activeRunId is not None \
            or ticket.lastRunId is None \
            or ticket.boardState in ("Backlog", "Canceled", "Done"):
        return None
    row = conn.execute("SELECT outcome, phase, prUrl FROM runs"
                       " WHERE id = ?", (ticket.lastRunId,)).fetchone()
    if not row or row[0] != "failed":
        return None
    if ticket.status == "in_flight" or (
            ticket.status == "blocked_on_operator"
            and row[1] != "awaiting_merge_approval"):
        return ticket.lastRunId, row[2]
    return None

def approve(target, identifier, note, out=None):
    """Release the ticket `identifier` parked for merge approval. Returns
    nothing.

    `--approve`'s whole body, `--requeue`'s twin: it opens the store, does
    `store.approve()`'s one transaction -- the `approve` intervention row
    carrying `note`, the parked run ended with its resume point at the merge
    gate, the ticket walked to `ready` -- prints what it did and exits. The
    loop's next claim of the ticket takes the preserved candidate straight to
    the merge gate. Every refusal `store.approve()` makes is a `SystemExit`
    naming the ticket's state, and nothing is written then; an identifier the
    store has not mirrored, or a target with no store, is refused the same
    way.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            run_id = store.approve(conn, ticket_id, note)
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} approved: run {run_id} released from"
              " awaiting_merge_approval and the ticket is ready; the loop's"
              " next claim resumes its candidate at the merge gate",
              file=out)
    finally:
        conn.close()


def babysit_ticket(target, identifier, note, out=None):
    """Release parked PRs with instructions or rechecks; refuse as SystemExit.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            if note != BABYSIT_DEFAULT_NOTE:
                run_id = store.read.ticket_by_id(conn, ticket_id).lastRunId
                event_id = operator_notes.send_back(
                    conn, run_id, note, getpass.getuser())
                purpose = ("as a maintainer instruction "
                           f"(operator_note event {event_id})")
            else:
                run_id = store.babysit(conn, ticket_id, note)
                purpose = "for another look"
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} sent back to the babysitter: run {run_id}"
              f" released and the ticket is ready {purpose}", file=out)
    finally:
        conn.close()


def repoint(target, identifier, sha, note, out=None):
    """Move the ticket `identifier`'s parked candidate to `sha`. Returns
    nothing.

    `--repoint`'s whole body, `--approve`'s sibling for the rebuilt-branch
    case: it opens the store, does `store.repoint()`'s one transaction --
    the `repoint` intervention row carrying `note`, the narrative event
    naming both shas, `candidateSha` moved -- prints the old and new shas
    and exits. The branch itself is the operator's git work, done before
    this; the merge gate the next approval resumes into holds the branch to
    the new sha exactly as it held it to the old. Every refusal
    `store.repoint()` makes is a `SystemExit` naming the ticket and the
    reason, and nothing is written then; an identifier the store has not
    mirrored, or a target with no store, is refused the same way.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            run_id, old_sha = store.repoint(conn, ticket_id, sha, note)
        except (store.RepointRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} re-pointed: run {run_id}'s candidate"
              f" moved from {old_sha} to {sha}; the merge gate now holds the"
              " branch to the new sha", file=out)
    finally:
        conn.close()


def close_ticket(target, identifier, landed, note=None, out=None, provider=None):
    """Record an external landing after a terminal, unsuccessful factory run.
    Preserve the outcome; validate and walk atomically, then project to the board.
    """
    out = sys.stdout if out is None else out
    conn = _operator_store(target)
    message = f"Closed: change landed at {landed}; no factory merge occurred."
    if note:
        message += f"\n\n{note}"
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            close_out_landed(target, conn, ticket_id, message, provider)
        except CloseRefused as refused:
            raise SystemExit(f"[holo2] {identifier}: {refused}") from None
        print(f"[holo2] {identifier} closed: {landed}; no factory merge",
              file=out)
    finally:
        conn.close()


def _operator_store(target):
    """Open the operator's store, refusing a missing store without creating it."""
    if not target.store_path.exists():
        raise SystemExit(f"[holo2] no store at {target.store_path}")
    return open_store(target)


def _ticket_by_identifier(target, conn, identifier):
    """The store's ticket id for the Linear identifier `KO-n`, or the exit
    for one the store has not mirrored or holds more than once."""
    rows = conn.execute(
        "SELECT id FROM tickets WHERE linearIdentifier = ?",
        (identifier,)).fetchall()
    if not rows:
        raise SystemExit(
            f"[holo2] {identifier}: no such ticket in {target.store_path}")
    if len(rows) > 1:
        raise SystemExit(
            f"[holo2] {identifier} names {len(rows)} tickets in"
            f" {target.store_path}; refusing to pick one")
    return rows[0][0]

"""The operator commands and the loop's entry point (KO-390).

`main()` drives one pass of the factory -- claim, mirror, lease,
`run_task()`, close out, repeat -- under `[loop] workers = 1`, or hands
the queue to `holophyte.pool`'s `scheduler()` above it; a loop that
merged a change to the factory itself re-executes through `_reexec()`
and the `EXEC` seam once `self_hosted()` says the target is this
repository. `report()` is `--report`'s whole body. `requeue()`,
`approve()`, `babysit_ticket()` and `repoint()` are the four operator
verbs behind `--requeue`, `--approve`, `--babysit` and `--repoint`:
each opens the store through `_operator_store()`, resolves the ticket
through `_ticket_by_identifier()`, does its one `store` transaction and
exits.

Moved verbatim out of `holophyte/loop.py`; the run stages, the
dispatcher and the startup sweep and queue mirror the serial pass
shares with the pool's scheduler stay there, and `PARKED`, `SWEPT`,
`_dispatch`, `_mirror_queue` and `_startup_sweep` are imported back
inside `_serial()`, the house pattern for a back-import
(`holophyte/pool.py`, `holophyte/claim.py`), so a `holophyte.loop`
attribute patch still lands.
"""
import os
import sys
from pathlib import Path

import store
import store.read
import store.tickets
from holophyte.agents import probe_implementer
from holophyte.board import release_lease_label
from holophyte.claim import _claim_next
from holophyte.config_tables import loop_config, report_config
from holophyte.findings import commit_findings
from holophyte.gates import sh
from holophyte.pool import scheduler
from holophyte.reconcile import (
    _reconcile_at_startup,
    _reconcile_pull_requests,
)
from holophyte.reexec import reexec_self
from holophyte.report import report_lines
from holophyte.runs import open_store
from holophyte.supervisor import linear_budget_low, supervisor_liveness_line

# How the loop restarts itself after merging a change to its own code: the
# process image is replaced, never a module reloaded. A seam so tests can
# see the decision without exec-ing the test runner.
EXEC = os.execv


def self_hosted(target):
    """Whether `target` is the repository this very module was imported from.

    Decided once at startup by `main()`: a loop working on the factory's own
    checkout keeps running the pre-merge code after every merge, so each
    dogfooded fix is invisible to the loop that merged it until someone
    restarts it (the writer host, 2026-09-02: run 17 cut a worktree without the
    ticket id run 16 had just merged support for).
    """
    # This module lives in `holophyte/`, one level below the repository; the
    # comparison is against the repository, as it was when it lived in
    # `factory.py`.
    return Path(__file__).resolve().parent.parent == target.path.resolve()


def main(target, provider):
    """The loop: one process working the queue a ticket at a time under
    `[loop] workers = 1`, the default; a scheduler over a pool of
    `--worker` children above it (KO-343). Returns the exit status.

    The first thing the pass does is prove a configured `[agents]
    implementer` answers (KO-357): `check_agent_commands()` settled that the
    program resolves, and this settles that it runs and replies, by asking it
    for one word under a short cap. A route that does not answer ends the
    pass here, nonzero, with the command and what it said on the terminal --
    before a ticket is claimed, where being wrong costs a message rather
    than a lease held through a failed implement turn. The default route is
    not probed, and a `--worker` child does not repeat this: it enters
    through `worker()`, not here."""
    probe = probe_implementer(target)
    if probe is not None:
        print(probe.describe())
        if not probe.ok:
            return 1
    knobs = loop_config(target)
    if knobs.workers == 1:
        return _serial(target, provider, knobs)
    return scheduler(target, provider, knobs)


def _serial(target, provider, knobs):
    """One pass of the factory in this process: claim, mirror, lease,
    `run_task()`, close out, repeat. The phases are the plain functions
    below, called in the order they ran when this was one function
    (KO-211). The loop as it was before the pool: `[loop] workers = 1`
    runs exactly this, and a `--worker` child runs the same phases once
    in `worker()`."""
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
    # Whether any run this pass failed, for the exit code when the loop was
    # told to go on past failures: the shell still sees a nonzero status for
    # a night that was not clean.
    failed = False
    conn = open_store(target)
    try:
        # The provider knows its team by name rather than by id; the column's
        # contract is one row per Linear team, which the name keys just as
        # well until the provider resolves the id.
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        seen = _startup_sweep(target, conn)
        _reconcile_at_startup(target, conn, project, provider)
        # The tickets this pass has refused to claim. A blocked ticket keeps
        # its place in the board's ready set — `blocked_on_operator` projects
        # to Todo, the column a human picks work out of — so it is offered
        # again the moment it is skipped. Remembering the refusal is what
        # turns "not this one" into "the one after it" instead of the same
        # ticket forever.
        skip = set()
        first_pass = True
        while True:
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
            # The claim's `claim_next()` spends the same ready listing the
            # mirror does, so a complexity budget under its tenth holds the
            # whole pass, not just the mirror: the pass ends on the reset
            # line `linear_budget_low()` prints once rather than asking to
            # be refused (KO-434). Asked after the mirror, not before it:
            # the mirror's own answer can be what pushed the budget under
            # its tenth -- a caught 429's remembered headers included --
            # and the mirror guards its own ask, so this one check holds
            # both (KO-434 review). The supervisor's fallback waits out
            # the same reset and starts the loop again once the meter
            # refills.
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
                print(f"[holo2] {task['id']} was swept mid-turn; continuing"
                      " to the next ready ticket")
                continue
            if not merged:
                # The regenerated window stays uncommitted, like the preserved
                # branch it describes: a human closes both out. Nonzero so the
                # shell — and anything supervising it — sees the failure.
                if stop_on_failure:
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
                # Store and Linear are terminal for this run, the lease is
                # released and no worktree is open: the re-exec starts
                # exactly where the next pass would, from the merged code.
                # Only after a merge -- a failure returned above, which is
                # the intended stop.
                _reexec(target, conn, project)
                return  # only a test's EXEC returns
    finally:
        conn.close()


def _reexec(target, conn, project):
    """Replace the process image with a fresh `factory.py` from the merged
    code, through the `EXEC` seam. Returns only when a test's EXEC does."""
    sha = sh(["git", "rev-parse", "--short", "HEAD"], target.path)
    # The note the supervisor watches for, written before the exec because
    # nothing can be written after a failed one: the sweep reports this
    # restart if no claim, heartbeat or exit note follows it within the
    # grace window.
    store.record_loop_restart(conn, project, sha)
    conn.close()
    reexec_self("merged a change to the factory itself;"
                f" re-executing from {sha}", EXEC)


def report(target, conn=None, out=None, now=None):
    """Print the target store's estimate-vs-actual table. Returns nothing.

    `--report`'s whole body: it reads rows and prints them, so no ticket is
    claimed, no worktree is cut and no provider is imported -- which is what
    makes it safe to run against the store of a loop that is still working.

    The one write it can make is `open_store()`'s migration: a store older
    than the run row's estimate column is brought up to the schema this
    queries instead of failing on the missing column, and the round counts an
    older module never stamped are recomputed from the rounds themselves
    rather than reported as zero. A target with no store at all is not created
    for the sake of an empty table; it is reported.

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
    conn = conn if conn is not None else open_store(target)
    try:
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
            or ticket.lastRunId is None:
        return None
    row = conn.execute("SELECT outcome, outcomeReason, prUrl FROM runs"
                       " WHERE id = ?", (ticket.lastRunId,)).fetchone()
    if not row or row[0] != "failed":
        return None
    if ticket.status == "in_flight" or (
            ticket.status == "blocked_on_operator"
            and store.is_gate_conflict(row[1])):
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
    """Send the ticket `identifier`, parked on its pull request, back to the
    babysitter. Returns nothing.

    `--babysit`'s whole body and `approve()`'s twin: `store.babysit()`'s
    one transaction -- its intervention row carrying `note`, the
    parked run ended with its resume point at the merge gate, the ticket
    walked to `ready` -- printed and done. The loop's next claim of the
    ticket resumes the candidate on its PR and makes another round of
    passes: new threads verdicted and answered, checks awaited; a PR that
    comes up ready under `[merge] approve = "human"` parks again for the
    human's `--approve`. The refusals are `--approve`'s, as `SystemExit`.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            run_id = store.babysit(conn, ticket_id, note)
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} sent back to the babysitter: run {run_id}"
              " released from awaiting_merge_approval and the ticket is"
              " ready; the loop's next claim resumes its candidate on the"
              " pull request", file=out)
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


def _operator_store(target):
    """The store an operator command writes to, or the exit for a target
    that has none: nothing to requeue, approve or re-point, and no file
    made for the sake of saying so."""
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

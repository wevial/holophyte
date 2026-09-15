"""The worker pool and its scheduler (KO-343).

Under `[loop] workers > 1` `main()` is `scheduler()`: a pool of
`factory.py --worker` children sized to the claimable queue, each one
`worker()` -- the serial loop's phases once, for one ticket, exiting with
a `WORKER_*` status the scheduler reads back. `_wait_any()` reaps the
children through the `WAIT` seam, `_PoolState` is what the scheduler has
learnt from their exits, `_claimable()` is the store's count of what a
worker could claim, `_spawn_worker()` starts one through `SPAWN`, and
`_PrefixedOut` folds each child's `[holo2]` tag into `[holo2 wN]` in the
shared log.

Moved verbatim out of `holophyte/loop.py` (KO-388); the claim, dispatch
and queue-mirror phases the pool shares with the serial loop stay there,
imported back inside the functions that call them.
"""
import os
import subprocess
import sys
from time import monotonic, sleep

import store
import store.tickets
from holophyte.config_tables import loop_config
from holophyte.findings import commit_findings, refresh_findings
from holophyte.gates import MergeLockHeld, merge_lock
from holophyte.reconcile import _reconcile_at_startup, _reconcile_pull_requests
from holophyte.reexec import reexec_command
from holophyte.runs import open_store
from holophyte.supervisor import Sweep

# --- the pool (KO-343) -------------------------------------------------------
#
# A worker's exit status is its one word back to the scheduler. `0` is a
# merge, as a clean process exit should be; `1` a failed run, the status the
# serial loop exits with on one and the one an uncaught exception exits a
# Python process with, so a worker that crashed outside `_dispatch()` reads
# as the failure it is. The other three are the scheduler's alone.
WORKER_MERGED = 0
WORKER_FAILED = 1
WORKER_PARKED = 2   # parked awaiting merge approval: not a failure
WORKER_IDLE = 3     # nothing left to claim
WORKER_STOP = 4     # the claim said stop for a human (`_claim_run()`)
# The environment variable a worker reads its slot number from, for the
# `[holo2 wN]` prefix on its lines: the children share the scheduler's
# stdout, and the prefix is what tells their lines apart in one log.
WORKER_SLOT_ENV = "HOLOPHYTE_WORKER"
# The seams the scheduler spawns and reaps through, so a test patches these
# and never `subprocess.Popen` or `os.wait` for the whole process.
SPAWN = subprocess.Popen


# How often the timed wait looks for an exited child, in seconds.
WAIT_POLL_S = 0.5


def _wait_any(children, timeout):
    """Block until any child exits, or `timeout` seconds pass; return
    `(pid, exit_code)`, or `(None, None)` when the deadline passed with no
    exit (KO-353). `timeout` is `None` for no deadline.

    `children` is the pool's live `Popen` objects by pid. The scheduler
    holds them for as long as the workers live -- a `Popen` dropped while
    its child runs is put on the module's housekeeping list, and the next
    `Popen()` reaps whatever on that list has exited, out from under this
    `os.wait()`: the worker becomes a phantom the pool waits on forever and
    its exit status is lost (the review of KO-343 reproduced it with two
    real children). Held, they are reaped here alone, and the one reaped is
    told its status so it is not put on that list when the pool drops it.
    Under a deadline the wait is `os.waitpid(-1, WNOHANG)` every
    `WAIT_POLL_S` until a child is reported or the deadline passes: there
    is no `os.wait()` with a timeout, and a signal-driven one would race
    a child that exited before the alarm was set.
    """
    if timeout is None:
        pid, status = os.wait()
    else:
        deadline = monotonic() + timeout
        while True:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid:
                break
            if monotonic() >= deadline:
                return None, None
            sleep(min(WAIT_POLL_S, max(deadline - monotonic(), 0)))
    code = os.waitstatus_to_exitcode(status)
    if pid in children:
        children[pid].returncode = code
    return pid, code


WAIT = _wait_any
# A worker runs no sweep of its own -- the scheduler swept once, and a
# second sweep would count one silence twice -- so its held-ticket lines
# have no sweep to point at.
NOTHING_SEEN = Sweep(0, (), False, (), ())


def worker(target, provider):
    """One `--worker` child: claim one ticket, run it, close it out, exit.

    The serial loop's phases once, less what the scheduler has already
    done -- the startup sweep, the reconcile, the queue mirror -- and less
    what belongs to the scheduler alone: no re-exec after a self-merge
    (the scheduler restarts once the pool has drained, so this worker
    finishes on the code it started with) and no exit note. Returns one
    of the `WORKER_*` statuses; the scheduler reads it from the exit code.
    """
    from holophyte.claim import _claim_next
    from holophyte.loop import PARKED, _dispatch

    slot = os.environ.get(WORKER_SLOT_ENV)
    if slot:
        # Both streams: a traceback, or a verify line's stderr, lands in the
        # same shared log as the progress lines, and is only attributable to
        # this worker by the prefix (the review of KO-343, second round).
        sys.stdout = _PrefixedOut(sys.stdout, f"[holo2 w{slot}]")
        sys.stderr = _PrefixedOut(sys.stderr, f"[holo2 w{slot}]")
    knobs = loop_config(target)
    conn = open_store(target)
    try:
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        task, ticket_id, run_id = _claim_next(target, conn, project, provider,
                                              knobs.order, set(), NOTHING_SEEN)
        if not task:
            print("[holo2] nothing left to claim; worker done.")
            return WORKER_IDLE
        if run_id is None:
            return WORKER_STOP
        merged = _dispatch(target, conn, run_id, provider, task, ticket_id,
                           refresh=False)
        if merged is PARKED:
            print(f"[holo2] {task['id']} parked awaiting merge approval")
            return WORKER_PARKED
        if not merged:
            _render_findings_locked(target, conn, run_id, task)
            return WORKER_FAILED
        _render_findings_locked(target, conn, run_id, task,
                                commit=f"Complete task {task['id']}: {task['title']}")
        return WORKER_MERGED
    finally:
        conn.close()


def _render_findings_locked(target, conn, run_id, task, commit=None):
    """A worker's rendering of FINDINGS.md, under the merge lock: the
    regeneration, and for a merged run its commit with `commit`'s message.

    The serial loop writes the window (and commits it, for a merged run)
    after its gate has let the lock go, which costs nothing when it is the
    only process in the checkout. A worker is not: a sibling can be merging
    in the same checkout at that moment, and a write to FINDINGS.md beside
    its merge dirties the checkout it is merging in or lands in its index,
    while a `git add`/`git commit` beside it is an index-lock failure for
    one of them (the review of KO-343, both rounds). So the write, and the
    commit when there is one, are one held span, the same lock the gate
    takes; a failed run's close-out passes `refresh=False` to
    `close_out_failure()` and renders here instead. A lock that cannot be
    had within the gate's wait leaves the window unrendered and says so:
    the run's outcome is in the store, and the next close-out in this
    checkout renders these rows with its own.
    """
    try:
        with merge_lock(target, run_id):
            refresh_findings(target, conn)
            if commit is not None:
                commit_findings(target, commit)
    except MergeLockHeld as e:
        what = "uncommitted" if commit is not None else "unrendered"
        print(f"[holo2] FINDINGS.md left {what} for {task['id']}: {e}")


class _PrefixedOut:
    """A text stream that starts every line with `prefix`, folding the
    factory's own `[holo2]` tag into it: `[holo2] run failed` from worker
    2 reads `[holo2 w2] run failed`, and any other line is prefixed whole.
    Writes are passed through as they come, so the line buffering the
    package set on the real stream still lands each line when it is said.
    """

    def __init__(self, stream, prefix):
        self.stream = stream
        self.prefix = prefix
        self.at_line_start = True
        # Whitespace written at a line start with no newline yet, such
        # as the indentation `traceback` writes before a source line: held
        # until the line shows what it is, so the prefix lands before it.
        self.held = ""

    def write(self, text):
        out = []
        for piece in text.splitlines(keepends=True):
            if self.at_line_start:
                piece = self.held + piece
                self.held = ""
                if not piece.strip():
                    if not piece.endswith(("\n", "\r")):
                        self.held = piece
                        continue
                elif piece.startswith("[holo2]"):
                    piece = self.prefix + piece[len("[holo2]"):]
                else:
                    piece = f"{self.prefix} {piece}"
            self.at_line_start = piece.endswith(("\n", "\r"))
            out.append(piece)
        return self.stream.write("".join(out))

    def __getattr__(self, name):
        return getattr(self.stream, name)


def scheduler(target, provider, knobs):
    """`[loop] workers > 1`: keep up to `knobs.workers` `--worker` children
    running, one per claimable ticket, until the queue is empty.

    The startup checks and the sweep once, then a tick per child exit:
    mirror the board's ready listing, count the tickets a worker could
    claim (`_claimable()`), spawn until `min(claimable, workers)` are
    alive, block until any child exits, read its status. While the pool
    is below the ceiling the block carries `knobs.tick_sec` as a deadline,
    and a deadline that reaps nobody is a tick like any other: the
    listing and the count run again for a ticket filed since (KO-353); a
    full pool waits on exits alone. A failed worker
    under `stop_on_failure` stops the spawning and the running workers are
    waited for, as the serial loop stops on its first failure; a merge into
    the factory itself does the same and re-execs once the pool has
    drained, so no worker ever runs code newer than the scheduler's. A
    worker that found nothing to claim is not a stop: the listing can run
    ahead of a claim a sibling is about to make, so the tick spawns nothing
    and the next exit recounts. Exits 0 with the queue empty and the pool
    drained, nonzero when any worker failed or stopped for a human.
    """
    from holophyte.loop import _mirror_queue, _startup_sweep
    from holophyte.operator import _reexec, self_hosted

    conn = open_store(target)
    pool = {}  # pid -> (slot number, Popen), the live workers
    slots = iter(range(1, sys.maxsize))
    state = _PoolState(self_hosted(target), knobs.stop_on_failure)
    try:
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        _startup_sweep(target, conn)
        _reconcile_at_startup(target, conn, project, provider)
        first_tick = True
        while True:
            # Every tick, timer or exit: a pull request merged on GitHub
            # since the last one ships its parked run (KO-359). The first
            # tick asked at startup, before the mirror was repaired.
            if not first_tick:
                _reconcile_pull_requests(target, conn, project, provider)
            first_tick = False
            listing = None
            if state.spawning:
                listing = _mirror_queue(target, conn, project, provider)
                if listing is None:
                    # The board could not be asked: an empty listing would
                    # end the loop reporting a queue it never saw. Nothing
                    # is spawned on it; a live pool recounts at its next
                    # exit, an empty one ends the loop nonzero, as the
                    # serial loop's claim ends it when the board is down.
                    state.unlisted()
                else:
                    # The claimable count leaves out the tickets the live
                    # workers hold -- a claim is a lease -- so the pool the
                    # queue can fill is the workers running plus what is
                    # still free to take, capped at the ceiling. Counting
                    # only the free tickets against the live pool never
                    # refilled a pool after its first exit.
                    want = min(len(pool) + _claimable(conn, project, listing),
                               knobs.workers)
                    while len(pool) < want:
                        slot = next(slots)
                        child = _spawn_worker(target, slot)
                        pool[child.pid] = (slot, child)
            if not pool:
                if listing is None and state.spawning:
                    print("[holo2] the board's ready listing failed and no"
                          " worker is running; stopping. relaunch once the"
                          " board answers")
                    return 1
                if state.restart and not state.stopped:
                    # Not after a stop: a restarted scheduler would know
                    # nothing of the failure, spawn again and exit clean
                    # under `stop_on_failure = true`. The operator relaunches
                    # on the merged code, as after a serial failure.
                    _reexec(target, conn, project)
                    return  # only a test's EXEC returns
                store.record_loop_return(conn, project)
                print("[holo2] Linear has no ready tickets. done.")
                return 1 if state.failed else None
            timeout = None if len(pool) >= knobs.workers else knobs.tick_sec
            pid, code = WAIT({pid: child for pid, (_, child) in pool.items()},
                             timeout)
            if pid in pool:  # else the supervisor, another child, or a tick
                state.exited(pool.pop(pid)[0], code)
    finally:
        conn.close()


class _PoolState:
    """What the scheduler has learnt from its workers' exits: whether any
    failed (the exit status), whether it may still spawn, and whether it
    restarts once the pool has drained. A drain is for good -- a failure
    under `stop_on_failure`, a stop for a human, a self-merge -- while an
    idle worker only holds the next tick's spawning, since the listing
    can run ahead of a claim a sibling is about to make. The first two
    are a `stopped` drain: the loop ends nonzero when the pool is in, and
    a self-merge seen alongside does not restart it."""

    def __init__(self, restart_after_merge, stop_on_failure):
        self.restart_after_merge = restart_after_merge
        self.stop_on_failure = stop_on_failure
        self.failed = False
        self.stopped = False
        self.paused = False
        self.restart = False

    @property
    def draining(self):
        return self.stopped or self.restart

    @property
    def spawning(self):
        return not (self.draining or self.paused)

    def unlisted(self):
        """This tick's listing failed: no verdict on the queue, no spawn,
        and the loop's exit is nonzero whatever the pool goes on to do."""
        self.failed = True

    def exited(self, slot, code):
        """Read worker `slot`'s exit `code`; one printed line each."""
        self.paused = False
        if code == WORKER_MERGED:
            print(f"[holo2] worker {slot} merged its ticket")
            if self.restart_after_merge:
                # Workers mid-run finish on the code they started with; none
                # is started on it, and the scheduler restarts from the
                # merged code once the last one is in.
                self.restart = True
        elif code == WORKER_PARKED:
            print(f"[holo2] worker {slot} parked its ticket awaiting"
                  " merge approval")
        elif code == WORKER_IDLE:
            print(f"[holo2] worker {slot} found nothing to claim")
            self.paused = True
        elif code == WORKER_STOP:
            print(f"[holo2] worker {slot} stopped for a human")
            self.failed = self.stopped = True
        else:
            print(f"[holo2] worker {slot} failed (exit {code})")
            self.failed = True
            if self.stop_on_failure:
                self.stopped = True


def _claimable(conn, project, listing):
    """How many of the board's ready `listing` a worker could claim now:
    the store's own pickability -- mirrored `ready`, under no live run's
    lease, specced, and every dependency merged -- asked of the rows
    `_mirror_queue()` just refreshed. The store's word, not the board's:
    a ticket a failed run left `in_flight`, one parked on the operator, one
    whose body the validator refused or one waiting on a sibling all sit in
    the board's ready column, and a worker spawned for one of them would
    only refuse it (the review of KO-343 found the dependency clause
    missing here: a worker spawned for a ticket `pickable()` then refused).
    One store read for the tick, as the ticket asks: `pickable_tickets()`
    fetches the project's rows once and answers §2 for all of them in
    memory (the review's second round counted seven selects for five
    tickets when this asked `pickable()` one ticket at a time)."""
    verdicts = store.tickets.pickable_tickets(conn, project)
    return sum(1 for task in listing if verdicts.get(task["id"]))


def _spawn_worker(target, slot):
    """Start `factory.py TARGET --worker` as slot `slot`, sharing this
    process's stdout and stderr so one `tee` captures the whole pool;
    return the `Popen`, which the caller holds until `WAIT()` reports it
    (see `_wait_any()`). The command line is the scheduler's own,
    `--worker` appended, so the interpreter flags the operator launched
    with (`-u` above all) reach the child too."""
    program, argv = reexec_command()
    env = dict(os.environ, **{WORKER_SLOT_ENV: str(slot)})
    child = SPAWN([program, *argv[1:], "--worker"], env=env,
                  stdin=subprocess.DEVNULL)
    print(f"[holo2] started worker {slot} as pid {child.pid}")
    return child

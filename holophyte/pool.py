"""Worker pool: the scheduler mirrors and reconciles; children claim one task.

The pool drains for schema moves its workers cannot read; ordinary
self-merges and additive moves preserve children.
SPAWN and WAIT are the process seams; worker exit codes report outcomes.
"""
import os
import subprocess
import sys
from time import monotonic, sleep

import store
import store.tickets
from holophyte import admission, pool_handoff
from holophyte.config_tables import loop_config
from holophyte.findings import commit_findings, findings_off, refresh_findings
from holophyte.gates import MergeLockHeld, merge_lock
from holophyte.reconcile import _reconcile_at_startup
from holophyte.redact import safe_print as print
from holophyte.reexec import reexec_command
from holophyte.runs import open_store
from holophyte.startup import banner
from holophyte.supervisor import Sweep, linear_budget_low

# --- the pool (KO-343) -------------------------------------------------------
#
# A worker's exit status is its one word back to the scheduler. `0` is a
# merge, as a clean process exit should be; `1` a failed run, the status the
# serial loop exits with on one and the one an uncaught exception exits a
# Python process with. The other three are the scheduler's alone.
WORKER_MERGED = 0
WORKER_FAILED = 1
WORKER_PARKED = 2   # parked awaiting merge approval: not a failure
WORKER_IDLE = 3     # nothing left to claim
WORKER_STOP = 4     # the claim said stop for a human (`_claim_run()`)
# The variable a worker reads its slot number from, for the `[holo2 wN]`
# prefix: the children share the scheduler's stdout.
WORKER_SLOT_ENV = "HOLOPHYTE_WORKER"
# Set to 1 when the scheduler's startup critic probe failed: a worker does
# not probe the critic again, and keeps the critic off for its life.
CRITIC_DOWN_ENV = "HOLOPHYTE_CRITIC_DOWN"
# The seams the scheduler spawns and reaps through, so a test patches these
# and never `subprocess.Popen` or `os.wait` for the whole process.
SPAWN = subprocess.Popen


# How often the timed wait looks for an exited child, in seconds.
WAIT_POLL_S = 0.5


def _wait_any(children, timeout):
    """Wait for a child exit, returning (pid, code), or (None, None) on timeout.
    Retain each Popen until reaped and set its returncode so Popen's cleanup
    cannot reap a child behind the scheduler's back. A deadline uses WNOHANG
    polling; None uses blocking os.wait()."""
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
# second sweep would count one silence twice.
NOTHING_SEEN = Sweep(0, (), False, (), ())


def worker(target, provider):
    """Worker processes own and probe their fallback routes independently."""
    from holophyte.agent_routes import reset, routes
    from holophyte.agents import startup_routes
    from holophyte.config_tables import AGENT_FALLBACK_KEYS

    slot = os.environ.get(WORKER_SLOT_ENV)
    if slot:
        # Both streams: a traceback or a verify line's stderr lands in the
        # same shared log, attributable to this worker only by the prefix.
        sys.stdout = _PrefixedOut(sys.stdout, f"[holo2 w{slot}]")
        sys.stderr = _PrefixedOut(sys.stderr, f"[holo2 w{slot}]")
    banner()
    configured = target.config().get("agents") or {}
    reset(target)
    routes(target).critic_failed = os.environ.get(CRITIC_DOWN_ENV) == "1"
    try:
        if "writer" in configured or any(k in configured for k in AGENT_FALLBACK_KEYS):
            if not startup_routes(target, provider, critic=False):
                return WORKER_STOP
        result = _worker(target, provider)
        return WORKER_STOP if routes(target).failed else result
    finally:
        reset(target)


def _worker(target, provider):
    """Claim and dispatch one ticket, then return its worker exit status.
    The scheduler mirrors, reconciles and re-execs; this child owns one run."""
    from holophyte.claim import _claim_next
    from holophyte.claim_store import BOARD_DOWN
    from holophyte.dispatch import PARKED, _dispatch

    knobs = loop_config(target)
    conn = open_store(target)
    try:
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        if linear_budget_low():
            return WORKER_IDLE
        task, ticket_id, run_id = _claim_next(target, conn, project, provider,
                                              knobs.order, set(), NOTHING_SEEN)
        if task is BOARD_DOWN:
            # Not an empty queue: the board could not be read back at the
            # claim, and a worker whose claim raised exits 1 in mirror mode.
            return WORKER_FAILED
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

    The serial loop writes the window (and commits a merged run's) after
    its gate has let the lock go, which costs nothing when it is the only
    process in the checkout. A worker is not: a sibling can be merging in
    the same checkout at that moment, and a write to FINDINGS.md beside
    its merge dirties the checkout it is merging in, while a `git add`/
    `git commit` beside it is an index-lock failure for one of them (the
    review of KO-343, both rounds). So the write, and the commit when
    there is one, are one held span, the same lock the gate takes; a
    failed run's close-out passes `refresh=False` and renders here
    instead. A lock that cannot be had within the gate's wait leaves the
    window unrendered and says so: the next close-out in this checkout
    renders these rows with its own. A target with the file off has no
    write and no commit to serialise, so it does not wait on the lock.
    """
    if findings_off(target):
        return
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
    2 reads `[holo2 w2] run failed`; any other line is prefixed whole.
    Writes pass through as they come, so the stream's line buffering
    still lands each line when it is said.
    """

    def __init__(self, stream, prefix):
        self.stream = stream
        self.prefix = prefix
        self.at_line_start = True
        # Whitespace written at a line start with no newline yet, like
        # `traceback`'s indent: held until the line shows what it is, so
        # the prefix lands before it.
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

    Mirror and refill on exits or partial-pool deadlines (`tick_sec`, KO-353).
    A full pool waits on exits. Failure under `stop_on_failure`
    drains and stops; only a schema move that is not additive drains
    before re-exec.
    An idle worker pauses spawning until the next exit recounts: a sibling
    may have claimed ahead of it. Return zero for an empty, drained queue,
    nonzero for a broken worker process, a human stop, or an unavailable
    board with no live workers. Ticket run failures do not make it nonzero."""
    from holophyte.claim import _park_unlisted
    from holophyte.claim_store import announce, store_mode
    from holophyte.dispatch import _startup_sweep
    from holophyte.operator import _reexec, self_hosted

    conn = open_store(target)
    pool = pool_handoff.restore(target)
    previous = set(pool)
    slots = iter(range(pool_handoff.next_slot(pool), sys.maxsize))
    state = _PoolState(self_hosted(target), knobs.stop_on_failure)
    try:
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        _startup_sweep(target, conn)
        announce(target)
        _reconcile_at_startup(target, conn, project, provider)
        first_tick = True
        while True:
            state.check_schema(target)
            if (pool_handoff.prepare_restart(state, target, pool)
                    and state.may_reexec(target)):
                pool_handoff.save(target, pool)
                _reexec(target, conn, project, state.reason,
                        prepared_sha=state.prepared_sha, can_ff=state.can_ff)
                return  # only a test's EXEC returns
            # Every tick, timer or exit: a pull request merged on GitHub
            # since the last one ships its parked run (KO-359). The first
            # tick asked at startup, before the mirror was repaired.
            admission.reconcile_tick(target, conn, project, provider, first_tick)
            first_tick = False
            held = admission.held_line(conn, project)
            if admission.held_idle(held, pool, conn, project):
                return 1 if state.broken else 0
            listing = None
            if state.spawning and not held:
                listing = pool_handoff.listing(target, conn, project, provider)
                if listing is not None:
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
                    # A stop takes priority: restarting would spawn again.
                    # The operator decides when to relaunch.
                    _reexec(target, conn, project, state.reason,
                            prepared_sha=state.prepared_sha, can_ff=state.can_ff)
                    return  # only a test's EXEC returns
                store.record_loop_return(conn, project)
                if listing is not None and not store_mode(target):
                    # The claim's empty-pass reconcile (KO-425) on this
                    # tick's own listing -- no second board ask. A
                    # store-mode queue is the store's own: nothing parks.
                    _park_unlisted(conn, project,
                                   [task["id"] for task in listing])
                print("[holo2] Linear has no ready tickets. done.")
                return 1 if state.broken else 0
            timeout = None if len(pool) >= knobs.workers else knobs.tick_sec
            pid, code = WAIT({pid: child for pid, (_, child) in pool.items()},
                             timeout)
            if pid in pool:  # else the supervisor, another child, or a tick
                state.exited(pool.pop(pid)[0], code)
                previous.discard(pid)
                pool_handoff.save(target, pool, previous)
    finally:
        conn.close()


class _PoolState:
    """Exit outcomes: failures may stop, idle pauses, self-merges restart.
    A stop takes priority over any pending restart. `broken` means a worker
    stopped for a human or exited by signal/unknown code, not a failed run."""

    def __init__(self, restart_after_merge, stop_on_failure):
        self.restart_after_merge = restart_after_merge
        self.stop_on_failure = stop_on_failure
        self.broken = False
        self.stopped = False
        self.paused = False
        self.restart = False
        self.restart_reason = None   # a store move this build cannot open: drain
        self.readable_reason = None  # one it can: hand the pool off
        self.unfollowable = False
        self.prepared_sha = None
        self.can_ff = None

    def check_schema(self, target):
        """A migration stops spawning. One `open()` refuses drains; one this
        build reads takes the self-merge hand-off path, unless this checkout
        already failed to fast-forward onto it."""
        from holophyte.operator import _schema_move

        if not self.spawning:
            return
        moved = _schema_move(target)
        if not moved or (moved.readable and self.unfollowable):
            return
        self.restart = True
        if moved.readable:
            self.readable_reason = moved.reason
        else:
            self.restart_reason = moved.reason

    @property
    def reason(self):
        return self.restart_reason or self.readable_reason

    def may_reexec(self, target):
        """Whether a prepared restart may exec now. For a readable store move,
        only once the checkout has fast-forwarded: otherwise -- a failed
        fetch, or a diverged main -- the code on disk is this build, which
        would find the same moved store and restart again. That restart is
        dropped and spawning resumes on this build."""
        if not self.readable_reason or (
                self.can_ff and pool_handoff._ff_main(target)):
            return True
        self.restart = False
        self.readable_reason = None
        self.unfollowable = True
        self.prepared_sha = self.can_ff = None
        return False

    @property
    def draining(self):
        return self.stopped or self.restart

    @property
    def spawning(self):
        return not (self.draining or self.paused)

    def exited(self, slot, code):
        """Read worker `slot`'s exit `code`; one printed line each."""
        self.paused = False
        if code == WORKER_MERGED:
            print(f"[holo2] worker {slot} merged its ticket")
            if self.restart_after_merge:
                self.restart = True
        elif code == WORKER_PARKED:
            print(f"[holo2] worker {slot} parked its ticket awaiting"
                  " merge approval")
        elif code == WORKER_IDLE:
            print(f"[holo2] worker {slot} found nothing to claim")
            self.paused = True
        elif code == WORKER_STOP:
            print(f"[holo2] worker {slot} stopped for a human")
            self.broken = self.stopped = True
        else:
            print(f"[holo2] worker {slot} failed (exit {code})")
            if code != WORKER_FAILED:
                self.broken = True
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
    from holophyte.agent_routes import routes

    program, argv = reexec_command()
    env = dict(os.environ, **{WORKER_SLOT_ENV: str(slot)})
    env.pop(CRITIC_DOWN_ENV, None)
    if routes(target).critic_failed:
        env[CRITIC_DOWN_ENV] = "1"
    child = SPAWN([program, *argv[1:], "--worker"], env=env,
                  stdin=subprocess.DEVNULL)
    print(f"[holo2] started worker {slot} as pid {child.pid}")
    return child

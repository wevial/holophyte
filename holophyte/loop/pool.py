import os
import subprocess
import sys
from time import monotonic, sleep

import store
import store.tickets
from holophyte import admission
from holophyte.config.config_tables import loop_config
from holophyte.host.reconcile import _reconcile_at_startup
from holophyte.host.startup import banner
from holophyte.host.supervisor import Sweep, linear_budget_low
from holophyte.loop import pool_handoff
from holophyte.loop.gates import MergeLockHeld, merge_lock
from holophyte.loop.reexec import reexec_command
from holophyte.loop.runs import open_store
from holophyte.redact import safe_print as print
from holophyte.review.findings import commit_findings, findings_off, refresh_findings

WORKER_MERGED = 0
WORKER_FAILED = 1
WORKER_PARKED = 2
WORKER_IDLE = 3
WORKER_STOP = 4
WORKER_BOARD_DOWN = 5
WORKER_SLOT_ENV = "HOLOPHYTE_WORKER"
CRITIC_DOWN_ENV = "HOLOPHYTE_CRITIC_DOWN"
SPAWN = subprocess.Popen


WAIT_POLL_S = 0.5


def _wait_any(children, timeout):
    """Each Popen is kept and given its returncode, so its cleanup never reaps."""
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
# A second sweep in a worker would count one silence twice.
NOTHING_SEEN = Sweep(0, (), False, (), ())


def worker(target, provider):
    from holophyte.agents.agent_routes import reset, routes
    from holophyte.agents.fallback import startup_routes
    from holophyte.config.config_tables import AGENT_FALLBACK_KEYS
    from holophyte.config.reader import REVIEW_FALLBACK_KEYS

    slot = os.environ.get(WORKER_SLOT_ENV)
    if slot:
        sys.stdout = _PrefixedOut(sys.stdout, f"[holo2 w{slot}]")
        sys.stderr = _PrefixedOut(sys.stderr, f"[holo2 w{slot}]")
    banner()
    configured = target.config().get("agents") or {}
    reset(target)
    routes(target).critic_failed = os.environ.get(CRITIC_DOWN_ENV) == "1"
    try:
        if any(k in configured for k in ("writer", "trimmer", *AGENT_FALLBACK_KEYS,
                                         *REVIEW_FALLBACK_KEYS)):
            if not startup_routes(target, provider, critic=False):
                return WORKER_STOP
        result = _worker(target, provider)
        return WORKER_STOP if routes(target).failed else result
    finally:
        reset(target)


def _worker(target, provider):
    from holophyte.loop.claim import _claim_next
    from holophyte.loop.claim_store import BOARD_DOWN
    from holophyte.loop.dispatch import PARKED, _dispatch

    knobs = loop_config(target)
    conn = open_store(target)
    try:
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        if linear_budget_low():
            return WORKER_IDLE
        task, ticket_id, run_id = _claim_next(target, conn, project, provider,
                                              knobs.order, set(), NOTHING_SEEN)
        if task is BOARD_DOWN:
            return WORKER_BOARD_DOWN
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
    """Under the merge lock: a sibling may be merging in the same checkout."""
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
    def __init__(self, stream, prefix):
        self.stream = stream
        self.prefix = prefix
        self.at_line_start = True
        # Line-start whitespace waits for the line, so the prefix lands before it.
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
    from holophyte.cli.operator import _reexec, self_hosted
    from holophyte.loop.claim import _park_unlisted
    from holophyte.loop.claim_store import announce, store_mode
    from holophyte.loop.dispatch import _startup_sweep
    from holophyte.story.witness import witness_step

    conn = open_store(target)
    pool = pool_handoff.restore(target)
    previous = set(pool)
    slots = iter(range(pool_handoff.next_slot(pool), sys.maxsize))
    state = _PoolState(self_hosted(target), knobs.stop_on_failure,
                       knobs.tick_sec)
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
                return
            admission.reconcile_tick(target, conn, project, provider, first_tick)
            first_tick = False
            held = admission.held_line(conn, project)
            if admission.held_idle(held, pool, conn, project):
                return 1 if state.broken else 0
            witness_step(target, conn, project)
            listing = None
            if state.spawning and not held:
                listing = pool_handoff.listing(target, conn, project, provider)
                if listing is not None:
                    # Live workers hold leases the claimable count leaves out.
                    want = state.ceiling(
                        len(pool), _claimable(conn, project, listing),
                        knobs.workers)
                    while len(pool) < want:
                        slot = next(slots)
                        child = _spawn_worker(target, slot)
                        pool[child.pid] = (slot, child)
            if not pool:
                if state.board_down or (listing is None and state.spawning):
                    what = (" could not be read back at the claim"
                            if state.board_down else "'s ready listing failed")
                    print(f"[holo2] the board{what} and no worker is running;"
                          " stopping. relaunch once the board answers")
                    return 1
                if state.restart and not state.stopped:
                    _reexec(target, conn, project, state.reason,
                            prepared_sha=state.prepared_sha, can_ff=state.can_ff)
                    return
                store.record_loop_return(conn, project)
                if listing is not None and not store_mode(target):
                    _park_unlisted(conn, project,
                                   [task["id"] for task in listing])
                print("[holo2] Linear has no ready tickets. done.")
                return 1 if state.broken else 0
            timeout = None if len(pool) >= knobs.workers else knobs.tick_sec
            pid, code = WAIT({pid: child for pid, (_, child) in pool.items()},
                             timeout)
            if pid in pool:
                state.exited(pool.pop(pid)[0], code)
                previous.discard(pid)
                pool_handoff.save(target, pool, previous)
    finally:
        conn.close()


class _PoolState:
    def __init__(self, restart_after_merge, stop_on_failure, tick_sec):
        self.restart_after_merge = restart_after_merge
        self.stop_on_failure = stop_on_failure
        self.tick_sec = tick_sec
        self.broken = False
        self.stopped = False
        self.board_down = False
        self.paused = False
        self.paused_at = None
        self.restart = False
        self.restart_reason = None
        self.readable_reason = None
        self.unfollowable = False
        self.prepared_sha = None
        self.can_ff = None

    def check_schema(self, target):
        from holophyte.cli.operator import _schema_move

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
        """Unless main fast-forwarded, the disk build would find the move again."""
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
        return not (self.draining or self.paused
                    and monotonic() - self.paused_at < self.tick_sec)

    def ceiling(self, live, claimable, workers):
        want = min(live + claimable, workers)
        return min(want, live + 1) if self.paused else want

    def exited(self, slot, code):
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
            self.paused_at = monotonic()
        elif code == WORKER_STOP:
            print(f"[holo2] worker {slot} stopped for a human")
            self.broken = self.stopped = True
        elif code == WORKER_BOARD_DOWN:
            print(f"[holo2] worker {slot} could not read its ticket back"
                  " from the board; spawning stops")
            self.board_down = self.stopped = True
        else:
            print(f"[holo2] worker {slot} failed (exit {code})")
            if code != WORKER_FAILED:
                self.broken = True
            if self.stop_on_failure:
                self.stopped = True


def _claimable(conn, project, listing):
    verdicts = store.tickets.pickable_tickets(conn, project)
    return sum(1 for task in listing if verdicts.get(task["id"]))


def _spawn_worker(target, slot):
    from holophyte.agents.agent_routes import routes

    program, argv = reexec_command()
    env = dict(os.environ, **{WORKER_SLOT_ENV: str(slot)})
    env.pop(CRITIC_DOWN_ENV, None)
    if routes(target).critic_failed:
        env[CRITIC_DOWN_ENV] = "1"
    child = SPAWN([program, *argv[1:], "--worker"], env=env,
                  stdin=subprocess.DEVNULL)
    print(f"[holo2] started worker {slot} as pid {child.pid}")
    return child

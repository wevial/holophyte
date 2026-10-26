import contextlib
import os
import signal
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from time import monotonic, sleep

import store
import store.tickets
from holophyte import admission
from holophyte.config.config_tables import loop_config
from holophyte.host.reconcile import _reconcile_at_startup
from holophyte.host.startup import banner
from holophyte.host.supervisor import Sweep, linear_budget_low
from holophyte.loop import gate_build, pool_handoff
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
WORKER_HANDOFF = 6
WORKER_SLOT_ENV = "HOLOPHYTE_WORKER"
CRITIC_DOWN_ENV = "HOLOPHYTE_CRITIC_DOWN"
SPAWN = subprocess.Popen


@dataclass(frozen=True)
class Reexec:
    run_id: int


WAIT_POLL_S = 0.5
MAX_RESTARTS = 2
STORE_SKIPS = 3
STORE_LOCKED = ("database is locked", "locking protocol")


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
                _hand_off_gate_run(target, provider)
                return WORKER_STOP
        result = _worker(target, provider)
        if not isinstance(result, Reexec):
            return WORKER_STOP if routes(target).failed else result
    finally:
        reset(target)
    return gate_build.replace_into_gate(result.run_id)


def _hand_off_gate_run(target, provider):
    gate_run = os.environ.pop(gate_build.GATE_RUN_ENV, None)
    if gate_run:
        gate_build.hand_off_gate_run(
            target, provider, int(gate_run), "a route probe failed after the re-exec"
            " into the merge gate")


def witness_child(target, provider):
    from holophyte.story.witness import COMMAND, witness_step

    def cancel(signum, frame):
        COMMAND.cancel(signum)

    previous = {signum: signal.signal(signum, cancel)
                for signum in (signal.SIGTERM, signal.SIGHUP)}
    conn = open_store(target)
    try:
        witness_step(target, conn, store.tickets.ensure_project(
            conn, provider.team, target.path), step=True)
    finally:
        conn.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0


def _worker(target, provider):
    from holophyte.loop.claim import _claim_next
    from holophyte.loop.claim_store import BOARD_DOWN
    from holophyte.loop.dispatch import HANDED_OFF, PARKED, REEXEC, _dispatch

    gate_run = os.environ.pop(gate_build.GATE_RUN_ENV, None)
    knobs = loop_config(target)
    conn = open_store(target)
    try:
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        if gate_run:
            resumed = gate_build.gate_task(target, conn, provider, int(gate_run))
            if resumed is None:
                print(f"[holo2] run {gate_run} is not waiting at the merge gate;"
                      " nothing to resume")
                return WORKER_IDLE
            (task, ticket_id), run_id = resumed, int(gate_run)
            if not task:
                _render_findings_locked(target, conn, run_id,
                                        {"id": f"run {run_id}"})
                return WORKER_BOARD_DOWN if task is BOARD_DOWN else WORKER_FAILED
        elif linear_budget_low():
            return WORKER_IDLE
        else:
            task, ticket_id, run_id = _claim_next(
                target, conn, project, provider, knobs.order, set(),
                NOTHING_SEEN)
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
        if merged is HANDED_OFF:
            return WORKER_HANDOFF
        if merged is REEXEC:
            return Reexec(run_id)
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

    conn = open_store(target)
    pool = pool_handoff.restore(target)
    witness = _WitnessChild(knobs.tick_sec)
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
        skips = _StoreSkips(conn, knobs.tick_sec)
        while True:
            with skips:
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
                if admission.held_idle(held, pool or witness.child, conn, project):
                    return 1 if state.broken else 0
                listing = None
                if state.spawning and not held and len(pool) < knobs.workers:
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
                witness.tick(target, conn, project, state)
                if not pool and witness.child is None and not witness.waiting:
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
            _reap(target, pool, previous, state, witness, knobs.tick_sec)
            if skips.count >= STORE_SKIPS:
                print("[holo2] scheduler stopping: store unavailable for"
                      f" {STORE_SKIPS} consecutive ticks ({skips.error})")
                return 1
    finally:
        conn.close()


class _StoreSkips:
    def __init__(self, conn, tick_sec):
        self.conn = conn
        self.tick_sec = tick_sec
        self.count = 0
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if kind is None:
            self.count = 0
            return False
        if not (isinstance(exc, sqlite3.OperationalError)
                and str(exc) in STORE_LOCKED):
            return False
        if self.conn.in_transaction:
            self.conn.rollback()
        self.count += 1
        self.error = exc
        print(f"[holo2] scheduler tick skipped: store unavailable ({exc});"
              f" next tick in {self.tick_sec}s")
        return True


def _reap(target, pool, previous, state, witness, tick_sec):
    children = {**{pid: child for pid, (_, child) in pool.items()},
                **witness.children()}
    if not children:
        sleep(tick_sec)
        return
    pid, code = WAIT(children, tick_sec)
    if not witness.reaped(pid, code) and pid in pool:
        state.exited(pool.pop(pid)[0], code)
        previous.discard(pid)
        pool_handoff.save(target, pool, previous)


class _WitnessChild:
    def __init__(self, tick_sec):
        self.tick_sec = tick_sec
        self.child = None
        self.spawned_at = None
        self.exited_at = None
        self.waiting = False
        self.cancelled = set()
        self.inherited = set()
        self.let_run = set()

    def tick(self, target, conn, project, state):
        if state.spawning:
            self.cancel_if_triggered(target, conn)
            self.spawn_if_due(target, conn, project)
        elif state.draining:
            self.waiting = False

    def spawn_if_due(self, target, conn, project):
        from holophyte.story.witness import pass_due
        from holophyte.story.witness_trigger import quiet

        self.waiting = False
        if self.child is not None:
            return
        tip = pass_due(target, conn, project)
        if tip is None:
            self.waiting = any(_still_running(pid) for pid in self.inherited)
            return
        if tip == self.spawned_at and monotonic() - self.exited_at < self.tick_sec:
            return
        if not quiet(target, tip):
            self.waiting = True
            return
        self.spawned_at = tip
        program, argv = reexec_command()
        env = {k: v for k, v in os.environ.items() if k != WORKER_SLOT_ENV}
        self.child = SPAWN([program, *argv[1:], "--witness-step"], env=env,
                           stdin=subprocess.DEVNULL)
        print(f"[holo2] started a witness pass as pid {self.child.pid}")

    def cancel_if_triggered(self, target, conn):
        from holophyte.story.witness_running import (
            add_restart,
            restarts,
            running_step,
        )

        running = running_step(target)
        if running is not None and (self.child is None
                                    or running[0] != self.child.pid):
            self.inherited.add(running[0])
        if running is None or running[0] in self.cancelled:
            return
        pid, identifier, ticket, sha = running
        tip = _triggering_tip(target, conn, ticket, sha)
        if tip is None or (pid, tip) in self.let_run:
            return
        moved = f"{identifier} at {sha[:12]}, main now at {tip[:12]}"
        if restarts(target, ticket) >= MAX_RESTARTS:
            self.let_run.add((pid, tip))
            print(f"[holo2] letting the witness pass of {moved} run: restarted"
                  f" {MAX_RESTARTS} times since its last completed pass")
            return
        add_restart(target, ticket)
        self.cancelled.add(pid)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
        print(f"[holo2] cancelled the witness pass of {moved}; it restarts"
              " once main is quiet")

    def children(self):
        return {} if self.child is None else {self.child.pid: self.child}

    def reaped(self, pid, code):
        if self.child is None or pid != self.child.pid:
            return False
        if code:
            print(f"[holo2] the witness pass exited {code}")
        self.child = None
        self.exited_at = monotonic()
        return True


def _still_running(pid):
    from holophyte.host.supervisor_lock import pid_alive

    try:
        exited, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return pid_alive(pid)
    return not exited


def _triggering_tip(target, conn, ticket, sha):
    from holophyte.story.witness import TIP_FAILURES, main_tip
    from holophyte.story.witness_trigger import _GitFailed, triggered

    try:
        tip = main_tip(target)
        if tip != sha and triggered(target, conn, ticket, sha, tip):
            return tip
    except (*TIP_FAILURES, _GitFailed):
        pass
    return None


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
        elif code == WORKER_HANDOFF:
            print(f"[holo2] worker {slot} handed its run to the next build")
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

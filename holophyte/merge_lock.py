"""Bounded, heartbeat-aware waiting for a healthy merge gate holder (KO-496)."""
import contextlib
import json
from time import monotonic, time

import store
import store.read
from holophyte.config_tables import merge_config, sweep_config
from holophyte.gates import MergeLockHeld, merge_lock, read_merge_lock
from holophyte.pr import CHECK_POLL_S
from holophyte.runs import heartbeat_while, set_phase


@contextlib.contextmanager
def live_merge_lock(target, conn, run_id, beat_s, operation="gate"):
    """Keep the acquisition alive, then close its wait event before the work."""
    with contextlib.ExitStack() as stack:
        with heartbeat_while(conn, run_id, beat_s):
            with _live_lock_wait(target, conn, run_id) as extend:
                stack.enter_context(merge_lock(target, run_id, extend_wait=extend,
                                               operation=operation))
        yield


@contextlib.contextmanager
def _live_lock_wait(target, conn, run_id):
    """One paired event for an extended acquisition, closed before the work."""
    started, since = monotonic(), time()
    waiting = {}
    ceiling = merge_config(target).check_wait_sec + 300
    stale_ms = sweep_config(target).heartbeat_stale_ms

    def extend(holder, elapsed):
        if conn is None or run_id is None or not holder or holder[0] is None:
            return 0
        snapshot = store.read.run_snapshot(conn, holder[0])
        if (snapshot is None or snapshot.endedAt is not None
                or time() * 1000 - snapshot.lastHeartbeat > stale_ms
                or elapsed >= ceiling):
            return 0
        if not waiting:
            waiting.update(holder=holder[0], since=since)
            set_phase(conn, run_id, "merge_gate",
                      f"waiting for merge lock (run {holder[0]})")
            store.record_event(conn, run_id, "merge_lock_wait", json.dumps(
                dict(waiting, state="begin", waited=elapsed)))
        return min(CHECK_POLL_S, ceiling - elapsed)

    try:
        yield extend
    finally:
        if waiting:
            store.record_event(conn, run_id, "merge_lock_wait", json.dumps(
                dict(waiting, state="end", waited=monotonic() - started)))


def lock_nap(path, elapsed, wait, poll, extend_wait, operation="gate"):
    if elapsed < wait:
        return min(poll, wait - elapsed)
    holder = read_merge_lock(path)
    if extend_wait is not None:
        nap = extend_wait(holder, elapsed)
        if nap > 0:
            return nap
    who = (f"run {holder[0]}" if holder and holder[0] is not None
           else "a run it does not name")
    extra = f"; waited {elapsed:.0f}s" if elapsed > wait else ""
    raise MergeLockHeld(
        f"merge lock {path} held by {who} for longer than the"
        f" {wait:.0f}s wait; the {operation} did not run{extra}. A holder whose"
        " run has ended is cleared by --sweep --act")

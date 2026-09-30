import json
import os
import signal
import sqlite3
import sys
import threading
import time

import store
from holophyte import deadline
from holophyte.admission import project_of
from holophyte.admission import state as admission_state
from holophyte.cli.status import HOME_LOCK, SWEEP_STATE, load_sweep_state
from holophyte.host.reconcile import GITHUB_BUDGET, _failed_pull_requests
from holophyte.host.registry import HostError, settings
from holophyte.host.supervisor import (
    HOST_SWEEP_PID,
    NEWER_SCHEMA,
    STOP_SIGNALS,
    ReconcileMemory,
    act_on_trip,
    factory_revision,
    reconcile_parked_pull_requests,
    sweep,
)
from holophyte.host.supervisor_lock import (
    SupervisorHeld,
    acquire_supervisor_lock,
    pid_alive,
    read_supervisor_lock,
    reclaim_turn,
    release_supervisor_lock,
    supervisor_lock_path,
)
from holophyte.host.sweep_report import merge_lock_lines, restart_lines, sweep_lines
from holophyte.loop.runs import open_store
from store import launch_backoff

UNAVAILABLE_AFTER = 3
# The rest is slack under `TimeoutStartSec`: no timeout cuts a Linear read in flight.
RECONCILE_SHARE = 0.5
# Far below the store's own busy timeout, so a locked store holds no other back.
SWEEP_BUSY_MS = 5000


def _now_ms():
    return int(time.time() * 1000)


def entry_key(entry):
    return entry.name or str(entry.path)


def load_state(home, out):
    path = home / SWEEP_STATE
    try:
        doc = load_sweep_state(home)
    except (OSError, ValueError) as bad:
        print(f"[holo2] {path} could not be read ({bad}); this run starts"
              " from an empty sweep state", file=out)
        return {}
    if doc is None:
        print(f"[holo2] no {path} yet; this run starts from an empty sweep"
              " state", file=out)
        return {}
    if not isinstance(doc, dict):
        print(f"[holo2] {path} is not a JSON object; this run starts from an"
              " empty sweep state", file=out)
        return {}
    return doc


def save_state(home, state):
    home.mkdir(parents=True, exist_ok=True)
    path = home / SWEEP_STATE
    temporary = path.with_name(f"{SWEEP_STATE}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, sort_keys=True, indent=1))
        handle.flush()
        os.fsync(handle.fileno())
    # A rename, so a reader sees the old document or the new one.
    os.replace(temporary, path)


def memory_for(state, name):
    def table(key):
        saved = (state.get(key) or {}).get(name) or {}
        return {int(ident): at for ident, at in saved.items()}
    return ReconcileMemory(table("mirror_asked_at"), table("failed_asked"),
                           table("states_asked"))


def keep_memory(state, name, memory, conn):
    still = {row[3] for (project,) in conn.execute("SELECT id FROM projects")
             for row in _failed_pull_requests(conn, project)}
    state.setdefault("mirror_asked_at", {})[name] = {
        str(project): at for project, at in memory.mirror_asked.items()}
    state.setdefault("states_asked", {})[name] = {
        str(project): at for project, at in memory.states_asked.items()}
    state.setdefault("failed_asked", {})[name] = {
        str(run): at for run, at in memory.failed_asked.items()
        if run in still}


def load_budget(state):
    """Kept across runs: a fresh process would read every parked pull request."""
    saved = state.get("github_budget") or {}
    GITHUB_BUDGET.remaining = saved.get("remaining")
    GITHUB_BUDGET.reset_at = saved.get("reset_at")


def keep_budget(state):
    state["github_budget"] = {"remaining": GITHUB_BUDGET.remaining,
                              "reset_at": GITHUB_BUDGET.reset_at}


def judge_target_lock(target, name, out):
    path = supervisor_lock_path(target)
    if not path.exists():
        return None
    with reclaim_turn(path):
        holder = read_supervisor_lock(path)
        if holder is None:
            if not path.exists():
                return None
            return (f"per-project supervisor lock {path} names no process;"
                    " remove it if no supervisor runs for this project")
        pid = holder[0]
        if pid > 0 and not pid_alive(pid):
            path.unlink(missing_ok=True)
            print(f"[holo2] {name}: removed per-project supervisor lock"
                  f" {path}: pid {pid} is gone", file=out)
            return None
    if pid <= 0:
        return f"per-project supervisor lock {path} names pid {pid}"
    return (f"a per-project supervisor, pid {pid}, holds {path}; not"
            " sweeping beside it")


def beat(conn, state, now):
    row = conn.execute(
        "SELECT startedAt FROM supervisorHeartbeats WHERE pid = ?"
        " ORDER BY lastBeat DESC LIMIT 1", (HOST_SWEEP_PID,)).fetchone()
    since = row[0] if row is not None else state.setdefault("since", now)
    store.record_supervisor_heartbeat(conn, HOST_SWEEP_PID, since, now)


def sweep_store(entry, state, now, out):
    if entry.error is not None:
        return f"error: {entry.error}", None
    target = entry.target
    held = judge_target_lock(target, entry_key(entry), out)
    if held is not None:
        return f"error: {held}", None
    if not target.store_path.exists():
        return f"skipped: no store at {target.store_path}", None
    conn = open_store(target)
    try:
        conn.execute(f"PRAGMA busy_timeout = {SWEEP_BUSY_MS}")
        project = project_of(conn, target)
        if project is None:
            return (f"skipped: no project row for {target.path};"
                    f" `factory.py project add {target.path}` writes it"), None
        admission, note = admission_state(conn, target)
        if admission == "disabled":
            return f"skipped: disabled: {note}", None
        seen = sweep(target, conn, now)
        # Printed now: a cut or failed reconcile would lose these for good.
        for line in restart_lines(seen):
            print(f"[{entry_key(entry)}] {line}", file=out)
        beat(conn, state, now)
        outcome = backoff_outcome(conn, project, now) or "ok"
    finally:
        conn.close()
    return outcome, seen


def backoff_outcome(conn, project, now):
    backoff = launch_backoff.current(conn, project)
    if backoff is None or backoff["until"] is None or backoff["until"] <= now:
        return None
    until = time.strftime("%H:%M", time.gmtime(backoff["until"] / 1000))
    reason = next(iter(str(backoff.get("reason") or "").splitlines()), "")
    return f"backoff: until {until} UTC ({reason})"


def sweep_project(entry, state, now, out):
    name = entry_key(entry)
    skipped = state.setdefault("skipped", {})
    try:
        outcome, seen = sweep_store(entry, state, now, out)
    except sqlite3.DatabaseError as bad:
        count = skipped[name] = skipped.get(name, 0) + 1
        if count >= UNAVAILABLE_AFTER:
            outcome = (f"error: unavailable, store not opened for {count}"
                       f" runs in a row ({bad})")
        else:
            outcome = (f"error: store unavailable ({bad}), run {count} of"
                       f" {UNAVAILABLE_AFTER} before it is unavailable")
        return outcome, None
    except SystemExit as refused:
        text = str(refused)
        outcome = ("error: schema newer than build: " if NEWER_SCHEMA in text
                   else "error: ") + text
        seen = None
    except Exception as bad:  # noqa: BLE001 - the project boundary
        outcome, seen = f"error: {type(bad).__name__}: {bad}", None
    skipped.pop(name, None)
    return outcome, seen


def _provider(target):
    from provider import board_for
    return board_for(target)


def reconcile_store(entry, seen, state, now, out):
    target, name = entry.target, entry_key(entry)
    provider = _provider(target)
    memory = memory_for(state, name)
    conn = open_store(target)
    try:
        outcomes = []
        for trip in seen.trips:
            deadline.check(f"acting on run {trip.run_id}")
            outcomes.append(act_on_trip(target, conn, trip, provider))
        seen = seen._replace(acted=True, outcomes=tuple(outcomes),
                             locks=tuple(merge_lock_lines(target, conn, True)),
                             restarts=())
        if seen.trips or seen.watched:
            print("\n".join(f"[{name}] {line}"
                            for line in sweep_lines(seen, target)), file=out)
        reconcile_parked_pull_requests(target, conn, now, provider, out,
                                       memory=memory)
    finally:
        try:
            keep_memory(state, name, memory, conn)
        finally:
            conn.close()


def reconcile_one(entry, seen, state, now, out, end, stop):
    try:
        with deadline.bounded(end, stop) as bound:
            reconcile_store(entry, seen, state, now, out)
    except (deadline.DeadlineReached, deadline.CallRefused) as reached:
        return str(reached), None
    except (Exception, SystemExit) as bad:  # noqa: BLE001 - the boundary
        return None, bad
    if bound.refused:
        more = len(bound.refused) - 1
        return bound.refused[0] + (f" and {more} more requests refused"
                                   if more else " refused"), None
    return None, None


def reconcile_all(swept, state, home, stop, sweep_sec, now, out):
    if not swept:
        return {}
    names = [entry_key(entry) for entry, _seen in swept]
    cursor = state.get("reconcile_cursor")
    start = names.index(cursor) if cursor in names else 0
    order = swept[start:] + swept[:start]
    end = time.monotonic() + sweep_sec * RECONCILE_SHARE
    errors = {}
    for index, (entry, seen) in enumerate(order):
        name = entry_key(entry)
        state["reconcile_cursor"] = name
        if stop.is_set() or time.monotonic() >= end:
            return errors
        save_state(home, state)
        share = (end - time.monotonic()) / (len(order) - index)
        reached, bad = reconcile_one(entry, seen, state, now, out,
                                     time.monotonic() + share, stop)
        if reached is not None:
            print(f"[holo2] {name}: reconcile cut, {reached}; the next run"
                  " starts after it", file=out)
        if bad is not None:
            errors[name] = f"error: {type(bad).__name__}: {bad}"
            print(f"[holo2] {name}: reconcile failed: {bad}", file=out)
        keep_budget(state)
        # Past a cut project too, so one slow read cannot starve the rest.
        state["reconcile_cursor"] = names[(start + index + 1) % len(names)]
        save_state(home, state)
    state["reconcile_cursor"] = names[(start + 1) % len(names)]
    return errors


def _begin(home, state, out, now, revision):
    started, ended = state.get("started"), state.get("ended")
    if isinstance(started, int) and (not isinstance(ended, int)
                                     or ended < started):
        print(f"[holo2] the sweep run started at {started} as pid"
              f" {state.get('pid')} did not end; the reconcile resumes at"
              f" {state.get('reconcile_cursor') or 'the first project'}",
              file=out)
        state["interrupted"] = {"started": started, "pid": state.get("pid")}
    else:
        state.pop("interrupted", None)
    state.update(started=now, ended=None, exit=None, pid=os.getpid(),
                 revision=revision, projects={})
    save_state(home, state)


def run_pass(host, stop=None, out=None, clock=None, revision=None):
    out = out or sys.stdout
    stop = threading.Event() if stop is None else stop
    clock = clock or _now_ms
    home = host.home
    state = load_state(home, out)
    now = clock()
    _begin(home, state, out, now, revision or factory_revision())
    exit_code = 0
    try:
        if not host.path.exists():
            raise HostError(f"[holo2] no host registry at {host.path};"
                            " `factory.py project add PATH` registers one")
        sweep_sec = settings(host).sweep_sec
        entries = host.projects()
    except HostError as bad:
        print(str(bad), file=out)
        state["error"] = str(bad)
        return _finish(home, state, 1, clock)
    state.pop("error", None)
    load_budget(state)
    # A store a recent run could not open goes last: the healthy ones beat first.
    unopened = state.get("skipped") or {}
    entries = sorted(entries, key=lambda entry: entry_key(entry) in unopened)
    swept = []
    for entry in entries:
        if stop.is_set():
            break
        name = entry_key(entry)
        outcome, seen = sweep_project(entry, state, clock(), out)
        state["projects"][name] = outcome
        if not outcome.startswith("ok"):
            print(f"[holo2] {name}: {outcome}", file=out)
        if seen is not None:
            swept.append((entry, seen))
        save_state(home, state)
    errors = reconcile_all(swept, state, home, stop, sweep_sec, now, out)
    state["projects"].update(errors)
    if stop.is_set():
        print("[holo2] host sweep stopped on signal before its run was"
              " whole", file=out)
        exit_code = 1
    if any(outcome.startswith("error")
           for outcome in state["projects"].values()):
        exit_code = 1
    return _finish(home, state, exit_code, clock)


def _finish(home, state, exit_code, clock):
    keep_budget(state)
    state.update(ended=clock(), exit=exit_code)
    save_state(home, state)
    return exit_code


def supervise_host(host, once=False, out=None, wait=None, clock=None):
    out = out or sys.stdout
    revision = factory_revision()
    try:
        interval = settings(host).sweep_sec
    except HostError as bad:
        print(str(bad), file=out)
        return 1
    pid = os.getpid()
    lock = host.home / HOME_LOCK
    try:
        acquire_supervisor_lock(lock, host.home, pid)
    except SupervisorHeld as held:
        print(f"{held}; this host sweep run exits", file=out)
        return 1
    stop = threading.Event()
    wait = stop.wait if wait is None else wait
    previous = {signum: signal.signal(signum, lambda *_: stop.set())
                for signum in STOP_SIGNALS}
    try:
        if once:
            return run_pass(host, stop, out, clock, revision)
        print(f"[holo2] host sweep as pid {pid}: every {interval}s over"
              f" {host.path}, lock at {lock}", file=out)
        # Unread at start stays unknown: a later `HEAD` is not this code.
        revision = revision or "unknown"
        while not stop.is_set():
            run_pass(host, stop, out, clock, revision)
            wait(interval)
        print("[holo2] host sweep stopping on signal; lock released",
              file=out)
        return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        release_supervisor_lock(lock, pid)

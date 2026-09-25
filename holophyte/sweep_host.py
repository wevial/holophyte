"""holophyte.sweep_host: the host sweep, `factory.py --supervise [--once]`.

One run watches every project the host registry (`holophyte.host`) lists,
each in its own store. `supervise_host()` is the mode: `--once` is one run
and an exit, what the sweep timer's oneshot runs; without it the run
repeats every `[supervisor] sweep_sec` of `host.toml` for a host with no
service manager. Neither re-executes: a run is the code it started with,
and under the timer every run starts from the checkout's `HEAD`.

A run (`run_pass()`), under the home lock
(`HOLOPHYTE_HOME/supervisor.lock`, taken per run by `--once` and once for
its life by the loop form, with the per-target lock's exclusive create and
dead-pid reclaim):

1. Sweeps every store and writes that store's beat right after its sweep
   (`sweep_store()`): the stale-run sweep without acting, then the
   sentinel `supervisorHeartbeats` row `(0, since)`, one per store. A
   store's write waits at most `SWEEP_BUSY_MS`, not the store's own
   thirty seconds, and a store a recent run could not open is swept after
   the others, so a locked store holds no healthy one back. The loop
   restarts the sweep stamps reported are printed here, as they are
   stamped. A project whose own `supervisor.lock` names a live pid is
   skipped this run naming it, a dead one is reclaimed and said so, an
   ambiguous one is skipped naming the path. No `projects` row is written:
   a store with none for the path is listed and skipped, as is a disabled
   project.
2. Reconciles the swept stores round-robin from `reconcile_cursor`, under
   one deadline of half the interval with an even share of what is left
   for each project in turn (`reconcile_all()`): the trips acted on, the
   stale merge lock removed, the parked and failed pull requests, the
   board's closes and the loop owed its start, exactly the project form's
   steps. `holophyte.deadline.check()` stands before every unit of
   Linear and GitHub work and cuts the project at the next one once its
   share is spent, and `deadline.admit()` stands before every Linear and
   GitHub request, so a unit already begun sends no request past it. The
   next run starts at the project after the one cut, never at the cut one:
   a project whose own read can spend the whole deadline would otherwise
   be the only one reconciled, run after run. The cut project keeps what
   it did through its throttles and comes around again.

What a run must remember between processes that no store column holds
lives in `sweep.json` beside the lock, rewritten whole through a temporary
file and a rename after every project, so a killed run leaves its record:
`started`, `ended`, `pid`, `revision`, `exit`, `projects` (each
`ok`, `skipped: WHY` or `error: WHY`), `skipped` (consecutive runs a store
was locked or corrupt; three make it `unavailable`), `reconcile_cursor`,
`github_budget`, `mirror_asked_at` and `failed_asked` (the KO-723 and
KO-722 throttles, by project name), `since` (the sentinel beat's key) and
`interrupted` (the last run's start when it did not end). A missing or
unreadable file is an empty state and one printed line.

One project's failure is that project's `error`, never the run's; the run
exits 1 when any project errored, or when a signal stopped it early.
"""
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
from holophyte.host import HostError, settings
from holophyte.reconcile import GITHUB_BUDGET, _failed_pull_requests
from holophyte.runs import open_store
from holophyte.status import HOME_LOCK, SWEEP_STATE, load_sweep_state
from holophyte.supervisor import (
    HOST_SWEEP_PID,
    NEWER_SCHEMA,
    STOP_SIGNALS,
    ReconcileMemory,
    act_on_trip,
    factory_revision,
    reconcile_parked_pull_requests,
    sweep,
)
from holophyte.supervisor_lock import (
    SupervisorHeld,
    acquire_supervisor_lock,
    pid_alive,
    read_supervisor_lock,
    reclaim_turn,
    release_supervisor_lock,
    supervisor_lock_path,
)
from holophyte.sweep_report import merge_lock_lines, restart_lines, sweep_lines

# Consecutive runs a store may be locked or corrupt before it is listed
# `unavailable`: the project form's three skipped passes.
UNAVAILABLE_AFTER = 3
# The reconcile's share of the interval: the rest is the sweep's and slack
# under `TimeoutStartSec`, since no timeout cuts a Linear read in flight.
RECONCILE_SHARE = 0.5
# How long step 1 waits on one store's write lock. A loop's transactions are
# arithmetic and end in milliseconds; a store held past this is the `locked`
# the three-run rule counts, not a reason to hold every later store back
# for the store's own `BUSY_TIMEOUT_S`.
SWEEP_BUSY_MS = 5000


def _now_ms():
    return int(time.time() * 1000)


def entry_key(entry):
    """A registry entry's name in `sweep.json`: its `[serve] name`, or its
    path when its config gave none."""
    return entry.name or str(entry.path)


# --- sweep.json -----------------------------------------------------------


def load_state(home, out):
    """`sweep.json` as the last run left it; `{}`, and one line, when it is
    missing, unreadable or not an object."""
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
    """Write `state` whole: a temporary file of this pid's, then a rename,
    so a reader sees the old document or the new one."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / SWEEP_STATE
    temporary = path.with_name(f"{SWEEP_STATE}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, sort_keys=True, indent=1))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def memory_for(state, name):
    """The project's `ReconcileMemory` from `sweep.json`, ids as ints."""
    def table(key):
        saved = (state.get(key) or {}).get(name) or {}
        return {int(ident): at for ident, at in saved.items()}
    return ReconcileMemory(table("mirror_asked_at"), table("failed_asked"),
                           table("states_asked"))


def keep_memory(state, name, memory, conn):
    """Put the project's throttles back into `state`, the failed-run reads
    pruned to the runs whose pull request is still asked about."""
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
    """`GITHUB_BUDGET` as the last run left it: a fresh process would read
    every parked pull request each minute, the spend `RATE_FLOOR`
    protects."""
    saved = state.get("github_budget") or {}
    GITHUB_BUDGET.remaining = saved.get("remaining")
    GITHUB_BUDGET.reset_at = saved.get("reset_at")


def keep_budget(state):
    state["github_budget"] = {"remaining": GITHUB_BUDGET.remaining,
                              "reset_at": GITHUB_BUDGET.reset_at}


# --- step 1: sweep and beat every store ------------------------------------


def judge_target_lock(target, name, out):
    """None when no per-project supervisor holds `target`; otherwise why
    the project is skipped this run. A lock naming a dead pid is removed,
    under the same reclaim turn a starting supervisor takes, and said so."""
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
    """Bump the store's sentinel beat, `(0, since)`: the store's own pid-0
    row keeps its key, so one row per store holds; a store with none takes
    `sweep.json`'s `since`, this instant when that is new too."""
    row = conn.execute(
        "SELECT startedAt FROM supervisorHeartbeats WHERE pid = ?"
        " ORDER BY lastBeat DESC LIMIT 1", (HOST_SWEEP_PID,)).fetchone()
    since = row[0] if row is not None else state.setdefault("since", now)
    store.record_supervisor_heartbeat(conn, HOST_SWEEP_PID, since, now)


def sweep_store(entry, state, now, out):
    """`(outcome, sweep)` for one project: the sweep, not yet acted on, and
    its beat, or the reason it was skipped. Raises what opening or reading
    the store raises, for `sweep_project()` to judge."""
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
        if project_of(conn, target) is None:
            return (f"skipped: no project row for {target.path};"
                    f" `factory.py project add {target.path}` writes it"), None
        admission, note = admission_state(conn, target)
        if admission == "disabled":
            return f"skipped: disabled: {note}", None
        seen = sweep(target, conn, now)
        # Stamped reported by the sweep just committed: printed now, since a
        # cut or failed reconcile would otherwise lose them for good.
        for line in restart_lines(seen):
            print(f"[{entry_key(entry)}] {line}", file=out)
        beat(conn, state, now)
    finally:
        conn.close()
    return "ok", seen


def sweep_project(entry, state, now, out):
    """`sweep_store()` behind the project boundary: whatever one project
    raises is its outcome, and three runs in a row that find its store
    locked or corrupt make it `unavailable` until a run beats it again."""
    name = entry_key(entry)
    skipped = state.setdefault("skipped", {})
    try:
        outcome, seen = sweep_store(entry, state, now, out)
    except sqlite3.DatabaseError as bad:
        # Locked (`OperationalError`) or not a database at all (its parent,
        # `DatabaseError`): either is a store this run could not open.
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


# --- step 2: reconcile under the deadline -----------------------------------


def _provider(target):
    from provider import board_for
    return board_for(target)


def reconcile_store(entry, seen, state, now, out):
    """The project form's acting half for one swept store: act on the
    trips, remove a stale merge lock, reconcile pull requests and board
    closes, start the loop owed. The throttles go back into `state` on
    every way out, a cut included."""
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
    """Reconcile one project under its share, ending at `end`; returns
    `(cut, failure)`: why the share cut it and what it raised, each or
    None. A Linear or GitHub request `deadline.admit()` refused cuts the
    project as a `check()` does, though the call sites went on past it."""
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
    """Reconcile `swept`, `(entry, sweep)` pairs, round-robin from
    `reconcile_cursor`; returns `{name: error}` for the projects that
    raised.

    The cursor names where the next run starts. It is saved as each project
    starts, so a killed run resumes at the project in hand, and moved to
    the project after it as each one ends, whole or cut, with the state
    saved again, throttles and GitHub budget included. So a run the
    deadline ends early leaves the cursor on the first project it did not
    reach, and a cut project is never the next run's first: one whose
    single read spends the whole deadline would otherwise be the only
    project ever reconciled. A run that reaches every project moves the
    cursor one on from where it started, so no project is always first."""
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
        state["reconcile_cursor"] = names[(start + index + 1) % len(names)]
        save_state(home, state)
    state["reconcile_cursor"] = names[(start + 1) % len(names)]
    return errors


# --- the run and the mode ------------------------------------------------------


def _begin(home, state, out, now, revision):
    """Report a last run that did not end, then mark this one started."""
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
    """One host sweep run; the caller holds the home lock. `revision` is
    the code the process runs, recorded in `sweep.json`: the checkout's
    `HEAD` when omitted, which a oneshot run is. Returns the run's exit
    status."""
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
    # A store a recent run could not open goes last, so the healthy ones
    # beat before anything waits on it.
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
    """`factory.py --supervise [--once]` with no project: take the home
    lock, then one run (`once`) or a run every `[supervisor] sweep_sec`
    until SIGINT or SIGTERM; release the lock on every way out. A second
    run beside a live one exits 1 naming the holder's pid. Every run
    records the revision the process started from: the loop form never
    re-executes, so a `HEAD` that moves under it is not the code it runs."""
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

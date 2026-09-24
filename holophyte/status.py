"""holophyte.status: `--status`, what the factory is doing right now (KO-596).

`snapshot()` answers the plain question in one dict: the store's projects
and their admission, the live runs and their phase, the parked tickets and
what they ask, how many tickets are ready, the schema version, and who holds
the supervisor and merge locks. `render()` is the same as a few lines of
text; `status_report()` is `--status`'s whole body, `--json` printing the
dict instead. The keys are snake_case and stable: the JSON is the wire shape
a later `doctor` and the shadow seat read.

Reads only. The store is opened through `store.read.open_readonly()` and
queried with the reads the sweep, `/status` and `/attention` already make
(`live_runs()` over `SWEEPABLE_PHASES`, `blocked_tickets()`,
`ready_tickets()`); the locks are read with `read_supervisor_lock()` and
`read_merge_lock()` and judged, never removed. Nothing here calls Linear or
GitHub.

With no project, `host_status_report()` is the host form: the registry
(`holophyte.host`), the build, the home's sweep lock and `sweep.json`, and
each registered project's snapshot, one project's failure its own `error`.
"""
import json
import sys
from time import time

import store.read
from holophyte.gates import merge_lock_path, read_merge_lock
from holophyte.serve_runs import json_host
from holophyte.supervisor import SWEEPABLE_PHASES, factory_revision
from holophyte.supervisor_lock import (
    pid_alive,
    read_supervisor_lock,
    supervisor_lock_path,
)


def snapshot(target, conn, now=None):
    """The target's state as a plain, JSON-able dict; `now` is epoch ms.

    `heartbeat_age_s` is whole seconds since `runs.lastHeartbeat`, reported
    and not judged: whether that is stale is the sweep's rule. A lock is
    null when there is no lock file; a lock whose holder can be judged
    carries `stale` -- a supervisor pid the kernel no longer knows, a merge
    lock naming a run that has ended or is not in the store.
    """
    now = int(time() * 1000) if now is None else now
    projects = conn.execute(
        "SELECT repoPath, admission, holdNote FROM projects ORDER BY id")
    return {
        "target": str(target.path),
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "projects": [{"path": path, "admission": admission,
                      "hold_note": note}
                     for path, admission, note in projects],
        "live": [{"run": run.id, "ticket": run.linearIdentifier,
                  "phase": run.phase, "worker": json_host(target, run.host),
                  "heartbeat_age_s": (now - run.lastHeartbeat) // 1000}
                 for run in store.read.live_runs(conn, SWEEPABLE_PHASES)],
        "parked": [{"run": ticket.runId, "ticket": ticket.linearIdentifier,
                    "question": ticket.blockedQuestion}
                   for ticket in store.read.blocked_tickets(conn)],
        "ready": len(store.read.ready_tickets(conn)),
        "supervisor_lock": _supervisor_holder(supervisor_lock_path(target)),
        "merge_lock": _merge_holder(target, conn),
    }


def _supervisor_holder(path):
    """`{"pid", "stale"}` for the supervisor lock at `path`, None with no
    lock file: a project's, or the host sweep's in the home.

    A file that names no pid is reported with a null pid and a null
    `stale`: a lock, but not one whose holder can be judged.
    """
    if not path.exists():
        return None
    holder = read_supervisor_lock(path)
    if holder is None:
        return {"pid": None, "stale": None}
    return {"pid": holder[0], "stale": not pid_alive(holder[0])}


def _merge_holder(target, conn):
    """`{"run", "stale"}` for the merge lock, None with no lock file; the
    run judged as `merge_lock_lines()` judges it, by its `endedAt`."""
    holder = read_merge_lock(merge_lock_path(target))
    if holder is None:
        return None
    run_id = holder[0]
    if run_id is None:
        return {"run": None, "stale": None}
    run = store.read.run_snapshot(conn, run_id)
    return {"run": run_id, "stale": run is None or run.endedAt is not None}


def _lock_line(name, holder, key):
    """One lock as a line: free, held by whom, or stale."""
    if holder is None:
        return f"{name} lock: free"
    if holder[key] is None:
        return f"{name} lock: present, names no {key}"
    state = "stale" if holder["stale"] else "held"
    return f"{name} lock: {state}, {key} {holder[key]}"


def render(snap):
    """The snapshot as the lines `--status` prints."""
    lines = [f"project {snap['target']} (schema {snap['schema_version']})"]
    for project in snap["projects"]:
        note = f": {project['hold_note']}" if project["hold_note"] else ""
        lines.append(f"project {project['path']} {project['admission']}{note}")
    for run in snap["live"]:
        worker = f" on {run['worker']}" if run["worker"] else ""
        lines.append(f"live {run['ticket']} run {run['run']} {run['phase']}"
                     f"{worker}, heartbeat {run['heartbeat_age_s']}s ago")
    for parked in snap["parked"]:
        lines.append(f"parked {parked['ticket']} run {parked['run']}:"
                     f" {parked['question'] or '(no question)'}")
    lines.append(f"ready {snap['ready']}")
    lines.append(_lock_line("supervisor", snap["supervisor_lock"], "pid"))
    lines.append(_lock_line("merge", snap["merge_lock"], "run"))
    return lines


def status_report(target, as_json=False, out=None, now=None):
    """`--status`'s body: print the snapshot as text, or as one JSON object.

    A target with no store is reported rather than created, as `--sweep`
    and `--report` answer the same mistake; the exit is non-zero so a
    script reading the JSON is not handed an empty line as an answer.
    """
    out = out or sys.stdout
    if not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return 1
    conn = store.read.open_readonly(target.store_path)
    try:
        snap = snapshot(target, conn, now)
    finally:
        conn.close()
    print(json.dumps(snap) if as_json else "\n".join(render(snap)), file=out)
    return 0


# The host form: `factory.py --status` with no project reads the registry,
# the home's sweep lock and `sweep.json`, then each registered project's
# store as the project form does. One project's missing store, bad config
# or unreadable file is that project's `error`, never the report.
HOME_LOCK = "supervisor.lock"
SWEEP_STATE = "sweep.json"


def _sweep_state(home):
    """`sweep.json` as written, None when absent, `{"error"}` unreadable."""
    try:
        return json.loads((home / SWEEP_STATE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as bad:
        return {"error": str(bad)}


def _host_project(entry, now):
    """One registry entry as `{"name", "path", "store", "error"}`."""
    row = {"name": entry.name, "path": str(entry.path), "store": None,
           "error": entry.error}
    if entry.error:
        return row
    # The project boundary: whatever reading this one project raises -- a
    # locked or corrupt store, an unreadable lock file -- is its `error`,
    # and the report goes on to the next.
    try:
        if not entry.target.store_path.exists():
            row["error"] = f"no store at {entry.target.store_path}"
            return row
        conn = store.read.open_readonly(entry.target.store_path)
        try:
            row["store"] = snapshot(entry.target, conn, now)
        finally:
            conn.close()
    except Exception as bad:
        row["error"] = f"{type(bad).__name__}: {bad}"
    return row


def host_snapshot(host, now=None):
    """The host's state as one JSON-able dict: the build this checkout is
    at and the one the last sweep ran, the home lock, `sweep.json`, and
    every registered project's snapshot or error."""
    sweep = _sweep_state(host.home)
    return {
        "home": str(host.home),
        "registry": str(host.path),
        "build": {"head": factory_revision(),
                  "sweep": (sweep or {}).get("revision")},
        "sweep": sweep,
        "home_lock": _supervisor_holder(host.home / HOME_LOCK),
        "projects": [_host_project(entry, now) for entry in host.projects()],
    }


def render_host(snap):
    """The host snapshot as the lines `--status` prints; a project's lines
    are its project-form lines, each prefixed with `[NAME]`."""
    build = snap["build"]
    sweep = snap["sweep"]
    lines = [f"host {snap['home']}: {len(snap['projects'])} projects in"
             f" {snap['registry']}",
             f"build head {build['head'] or 'unknown'},"
             f" sweep {build['sweep'] or 'none'}"]
    if sweep is None:
        lines.append("sweep: none")
    else:
        lines.append("sweep: " + ", ".join(
            f"{key} {sweep[key]}" for key in
            ("started", "ended", "revision", "exit", "error") if key in sweep))
    lines.append(_lock_line("home", snap["home_lock"], "pid"))
    for project in snap["projects"]:
        prefix = f"[{project['name'] or project['path']}]"
        if project["store"] is None:
            lines.append(f"{prefix} project {project['path']}:"
                         f" {project['error']}")
            continue
        lines.extend(f"{prefix} {line}" for line in render(project["store"]))
    return lines


def host_status_report(host, as_json=False, out=None, now=None):
    """`--status` with no project: exit 1 when there is no registry or any
    project could not be read, so a script is not handed a partial answer
    as a whole one."""
    out = out or sys.stdout
    if not host.path.exists():
        print(f"[holo2] no host registry at {host.path}; `factory.py project"
              " add PATH` registers a project", file=out)
        return 1
    snap = host_snapshot(host, now)
    print(json.dumps(snap) if as_json else "\n".join(render_host(snap)),
          file=out)
    return 1 if any(project["error"] for project in snap["projects"]) else 0

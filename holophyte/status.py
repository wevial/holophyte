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
"""
import json
import sys
from time import time

import store.read
from holophyte.gates import merge_lock_path, read_merge_lock
from holophyte.serve_runs import json_host
from holophyte.supervisor import SWEEPABLE_PHASES
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
        "supervisor_lock": _supervisor_holder(target),
        "merge_lock": _merge_holder(target, conn),
    }


def _supervisor_holder(target):
    """`{"pid", "stale"}` for the supervisor lock, None with no lock file.

    A file that names no pid is reported with a null pid and a null
    `stale`: a lock, but not one whose holder can be judged.
    """
    path = supervisor_lock_path(target)
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
    lines = [f"target {snap['target']} (schema {snap['schema_version']})"]
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

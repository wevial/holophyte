import json
import sys
from collections import Counter
from time import time

import store.read
from holophyte.host.supervisor import SWEEPABLE_PHASES, factory_revision
from holophyte.host.supervisor_lock import (
    pid_alive,
    read_supervisor_lock,
    supervisor_lock_path,
)
from holophyte.loop.claim_store import store_mode
from holophyte.loop.gates import merge_lock_path, read_merge_lock
from holophyte.serve.console_build import console_state
from holophyte.serve.serve_runs import json_host
from holophyte.serve.server import CONSOLE_DIR
from holophyte.story.story_views import story_facts, story_lines
from store.steer_notes import steers


def snapshot(target, conn, now=None):
    now = int(time() * 1000) if now is None else now
    pending = Counter(note.ticket for note in steers(conn)
                      if note.event_id is None and note.consumed_by is None)
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
                  "heartbeat_age_s": (now - run.lastHeartbeat) // 1000,
                  "steer_pending": pending.get(run.linearIdentifier, 0)}
                 for run in store.read.live_runs(conn, SWEEPABLE_PHASES)],
        "parked": [{"run": ticket.runId, "ticket": ticket.linearIdentifier,
                    "question": ticket.blockedQuestion}
                   for ticket in store.read.blocked_tickets(conn)],
        "stories": story_facts(target, conn, now),
        "stranded": [{"run": run.id, "ticket": run.linearIdentifier,
                      "reason": run.outcomeReason, "ended_ms": run.endedAt}
                     for run in store.read.stranded_runs(conn)],
        "ready": _ready(target, conn),
        "supervisor_lock": _supervisor_holder(supervisor_lock_path(target)),
        "merge_lock": _merge_holder(target, conn),
    }


def _ready(target, conn):
    if not store_mode(target):
        return len(store.read.ready_tickets(conn))
    return sum(len(store.read.claimable(conn, project))
               for (project,) in conn.execute("SELECT id FROM projects"))


def _supervisor_holder(path):
    if not path.exists():
        return None
    holder = read_supervisor_lock(path)
    if holder is None:
        return {"pid": None, "stale": None}
    return {"pid": holder[0], "stale": not pid_alive(holder[0])}


def _merge_holder(target, conn):
    holder = read_merge_lock(merge_lock_path(target))
    if holder is None:
        return None
    run_id = holder[0]
    if run_id is None:
        return {"run": None, "stale": None}
    run = store.read.run_snapshot(conn, run_id)
    return {"run": run_id, "stale": run is None or run.endedAt is not None}


def _lock_line(name, holder, key):
    if holder is None:
        return f"{name} lock: free"
    if holder[key] is None:
        return f"{name} lock: present, names no {key}"
    state = "stale" if holder["stale"] else "held"
    return f"{name} lock: {state}, {key} {holder[key]}"


def render(snap):
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
    lines.extend(story_lines(snap["stories"]))
    for stranded in snap["stranded"]:
        # One ticket stays one line; the JSON keeps the reason as stored.
        reason = "\\n".join((stranded["reason"] or "").splitlines())
        lines.append(f"stranded {stranded['ticket']} run {stranded['run']}:"
                     f" {reason or '(no reason)'}")
    lines.append(f"ready {snap['ready']}")
    lines.append(_lock_line("supervisor", snap["supervisor_lock"], "pid"))
    lines.append(_lock_line("merge", snap["merge_lock"], "run"))
    return lines


def status_report(target, as_json=False, out=None, now=None):
    """A missing store exits non-zero so a JSON reader gets no empty answer."""
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


HOME_LOCK = "supervisor.lock"
SWEEP_STATE = "sweep.json"


def load_sweep_state(home):
    try:
        return json.loads((home / SWEEP_STATE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def read_sweep_state(home):
    try:
        return load_sweep_state(home)
    except (OSError, ValueError) as bad:
        return {"error": str(bad)}


def _host_project(entry, now):
    row = {"name": entry.name, "path": str(entry.path), "store": None,
           "error": entry.error}
    if entry.error:
        return row
    try:
        if not entry.target.store_path.exists():
            row["error"] = f"no store at {entry.target.store_path}"
            return row
        conn = store.read.open_readonly(entry.target.store_path)
        try:
            row["store"] = snapshot(entry.target, conn, now)
        finally:
            conn.close()
    # Whatever one project raises is its error; the report goes on.
    except (Exception, SystemExit) as bad:
        row["error"] = f"{type(bad).__name__}: {bad}"
    return row


def host_snapshot(host, now=None):
    sweep = read_sweep_state(host.home)
    return {
        "home": str(host.home),
        "registry": str(host.path),
        "build": {"head": factory_revision(),
                  "sweep": (sweep or {}).get("revision")},
        "console": console_state(CONSOLE_DIR, host.home),
        "sweep": sweep,
        "home_lock": _supervisor_holder(host.home / HOME_LOCK),
        "projects": [_host_project(entry, now) for entry in host.projects()],
    }


def render_host(snap):
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
    console = snap["console"]
    if console["stale"]:
        lines.append(f"console stale: serving {console['served'] or 'no build'},"
                     f" the checkout's tree is {console['tree']}:"
                     f" {console['reason'] or 'no failed build recorded'}")
    lines.append(_lock_line("home", snap["home_lock"], "pid"))
    swept = (sweep or {}).get("projects")
    for project in snap["projects"]:
        key = project["name"] or project["path"]
        prefix = f"[{key}]"
        if isinstance(swept, dict) and key in swept:
            lines.append(f"{prefix} last sweep: {swept[key]}")
        if project["store"] is None:
            lines.append(f"{prefix} project {project['path']}:"
                         f" {project['error']}")
            continue
        lines.extend(f"{prefix} {line}" for line in render(project["store"]))
    return lines


def host_status_report(host, as_json=False, out=None, now=None):
    """Exit 1 on any unreadable project, so a partial answer is not a whole."""
    out = out or sys.stdout
    if not host.path.exists():
        print(f"[holo2] no host registry at {host.path}; `factory.py project"
              " add PATH` registers a project", file=out)
        return 1
    snap = host_snapshot(host, now)
    print(json.dumps(snap) if as_json else "\n".join(render_host(snap)),
          file=out)
    return 1 if any(project["error"] for project in snap["projects"]) else 0

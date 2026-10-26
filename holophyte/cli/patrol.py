"""`--patrol`: each project's checks for the operator seat; writes nothing."""
import json
import re
from time import time

import store.read
from holophyte.config.config_tables import patrol_config
from holophyte.holo.render import age
from holophyte.host.supervisor import SWEEPABLE_PHASES
from holophyte.serve.views import attention

HOST_KINDS = frozenset(("sweep_stale", "project_error", "no_store",
                        "no_project_row"))
SECTIONS = ("in_flight", "stranded", "quiet", "repeat_failures")
REASON_WIDTH = 80


def first_line(text):
    return (text or "").partition("\n")[0].strip()


def reason_shape(text):
    return re.sub(r"\d+", "N", first_line(text))[:REASON_WIDTH]


def group_of(item):
    if item.get("park_kind") is not None:
        return f"{item['kind']}:{item['park_kind']}"
    if item["kind"] == "stuck_wait":
        return f"stuck_wait:{item['dependency_state']}"
    return item["kind"]


def attention_groups(items):
    groups = {}
    for item in items:
        groups.setdefault(group_of(item), []).append(item)
    return [{"group": group, "items": found} for group, found in groups.items()]


def in_flight(runs, now):
    return [{"ticket": run.linearIdentifier, "run": run.id,
             "phase": run.phase, "heartbeat_age_ms": now - run.lastHeartbeat}
            for run in runs]


def stranded(tickets):
    return [{"ticket": ticket.linearIdentifier, "run": ticket.runId,
             "outcome": ticket.outcome, "reason": ticket.outcomeReason,
             "ended_ms": ticket.endedAt} for ticket in tickets]


def quiet(flying, events, now, quiet_ms):
    found = []
    for run in flying:
        last = events.get(run["run"])
        event = None if last is None else now - last
        if run["heartbeat_age_ms"] > quiet_ms and (event is None
                                                   or event > quiet_ms):
            found.append({**run, "event_age_ms": event})
    return found


def repeat_failures(ended, count):
    found = []
    for ticket, runs in ended.items():
        if len(runs) < count:
            continue
        shapes = {(run.outcome, run.failureKind, reason_shape(run.outcomeReason))
                  for run in runs}
        if len(shapes) == 1 and runs[0].outcome == "failed":
            found.append({"ticket": ticket, "runs": [run.id for run in runs],
                          "failure_kind": runs[0].failureKind,
                          "reason": runs[0].outcomeReason})
    return found


def store_sections(target, now, quiet_ms, repeat_runs):
    conn = store.read.open_readonly(target.store_path)
    try:
        runs = store.read.live_runs(conn, SWEEPABLE_PHASES)
        events = store.read.last_event_at(conn, [run.id for run in runs])
        tickets = store.read.stranded_tickets(conn)
        ended = store.read.last_ended_runs(conn, repeat_runs)
    finally:
        conn.close()
    flying = in_flight(runs, now)
    return {"in_flight": flying, "stranded": stranded(tickets),
            "quiet": quiet(flying, events, now, quiet_ms),
            "repeat_failures": repeat_failures(ended, repeat_runs)}


def unignored(entries, ignore, hid):
    kept = []
    for entry in entries:
        if entry.get("ticket") in ignore:
            hid.add(entry["ticket"])
        else:
            kept.append(entry)
    return kept


def project_row(name, target, items, now):
    """`items` are the project's own `views.attention()` items."""
    knobs = patrol_config(target)
    sections = store_sections(target, now, knobs.quiet_ms, knobs.repeat_runs)
    hid = set()
    row = {"name": name, "path": str(target.path), "error": None}
    row["attention"] = attention_groups(unignored(items, knobs.ignore, hid))
    row.update({key: unignored(sections[key], knobs.ignore, hid)
                for key in SECTIONS})
    row["ignored"] = [key for key in dict.fromkeys(knobs.ignore) if key in hid]
    row["clean"] = not (row["attention"] or row["stranded"] or row["quiet"]
                        or row["repeat_failures"])
    return row


def error_row(name, path, error):
    return {"name": name, "path": str(path), "error": error, "clean": False,
            "attention": [], **{key: [] for key in SECTIONS}, "ignored": []}


def entry_name(target):
    from holophyte.host.registry import Host, HostError, registered_at
    try:
        entry = registered_at(Host.locate(), target)
    except HostError:
        return None
    return None if entry is None else entry.name


def project_snapshot(target):
    now = int(time() * 1000)
    name = entry_name(target)
    if not target.store_path.exists():
        row = error_row(name, target.path, f"no store at {target.store_path}")
    else:
        _, body = attention(target, now)
        row = project_row(name, target, body["items"], now)
    return {"now": now, "form": "project", "host": {"attention": []},
            "projects": [row]}


def host_entry(entry, now, stale_ms):
    from holophyte.serve.serve_host import _project_items
    found, _ = _project_items(entry, now, stale_ms)
    own = [item for item in found if item["kind"] not in HOST_KINDS]
    host = [item for item in found if item["kind"] in HOST_KINDS]
    errors = [item.get("error") for item in host
              if item["kind"] == "project_error"]
    if any(item["kind"] == "no_store" for item in host):
        errors.append(f"no store at {entry.target.store_path}")
    if errors:
        return host, error_row(entry.name, entry.path, errors[0])
    items = [{key: value for key, value in item.items() if key != "project"}
             for item in own]
    try:
        return host, project_row(entry.name, entry.target, items, now)
    # Whatever one project raises is its error; the patrol goes on.
    except (Exception, SystemExit) as bad:
        return host, error_row(entry.name, entry.path,
                               f"{type(bad).__name__}: {bad}")


def host_snapshot(host):
    from holophyte.host.registry import settings
    from holophyte.serve.serve_host import beat_stale_ms, sweep_stale_items
    now = int(time() * 1000)
    knobs = settings(host)
    items = sweep_stale_items(host.home, now, knobs.sweep_sec)
    projects = []
    for entry in host.projects():
        found, row = host_entry(entry, now, beat_stale_ms(knobs))
        items.extend(found)
        projects.append(row)
    return {"now": now, "form": "host", "host": {"attention": items},
            "projects": projects}


def item_line(item):
    text = first_line(item.get("question") or item.get("reason")
                      or item.get("note") or item.get("error")
                      or item.get("detail"))
    if text:
        return text
    if item.get("heartbeat_age_ms") is not None:
        return f"heartbeat {age(item['heartbeat_age_ms'] // 1000)}"
    return item.get("state") or item.get("phase") or "(no reason)"


def attention_lines(row):
    return [f"attention {group['group']}"
            f"{' ' + item['ticket'] if item.get('ticket') else ''}:"
            f" {item_line(item)}"
            for group in row["attention"] for item in group["items"]]


def section_lines(row, now):
    lines = [f"in flight {run['ticket']} run {run['run']} {run['phase']},"
             f" heartbeat {age(run['heartbeat_age_ms'] // 1000)}"
             for run in row["in_flight"]]
    for entry in row["stranded"]:
        if entry["run"] is None:
            lines.append(f"stranded {entry['ticket']} no run")
            continue
        lines.append(f"stranded {entry['ticket']} run {entry['run']}"
                     f" {entry['outcome']}"
                     f" {age((now - entry['ended_ms']) // 1000)} ago:"
                     f" {first_line(entry['reason']) or '(no reason)'}")
    lines.extend(f"quiet {run['ticket']} run {run['run']} {run['phase']}:"
                 f" heartbeat {age(run['heartbeat_age_ms'] // 1000)},"
                 f" last event {_event_age(run['event_age_ms'])}"
                 for run in row["quiet"])
    lines.extend(f"repeat {entry['ticket']} runs"
                 f" {', '.join(str(run) for run in entry['runs'])}"
                 f" {entry['failure_kind'] or '(no kind)'}:"
                 f" {first_line(entry['reason']) or '(no reason)'}"
                 for entry in row["repeat_failures"])
    return lines


def _event_age(ms):
    return "none" if ms is None else age(ms // 1000)


def project_lines(row, now):
    name = f"[{row['name'] or row['path']}]"
    if row["error"]:
        return [f"{name} {row['error']}"]
    ignored = f"ignored {', '.join(row['ignored'])}" if row["ignored"] else None
    if row["clean"]:
        tail = [f"{len(row['in_flight'])} in flight"] * bool(row["in_flight"])
        return [" · ".join([f"{name} nothing to do", *tail,
                            *[ignored] * bool(ignored)])]
    lines = attention_lines(row) + section_lines(row, now)
    return [name, *(f"  {line}" for line in lines + [ignored] * bool(ignored))]


def render(snap):
    lines = [f"[host] attention {item['kind']}"
             f"{' ' + item['project'] if item.get('project') else ''}:"
             f" {item_line(item)}" for item in snap["host"]["attention"]]
    for row in snap["projects"]:
        lines.extend(project_lines(row, snap["now"]))
    return lines


def show(snap, as_json):
    print(json.dumps(snap) if as_json else "\n".join(render(snap)))
    return 1 if any(row["error"] for row in snap["projects"]) else 0


def patrol_report(target, as_json=False):
    return show(project_snapshot(target), as_json)


def host_patrol_report(host, as_json=False):
    if not host.path.exists():
        print(f"[holo2] no host registry at {host.path}; `factory.py project"
              " add PATH` registers a project")
        return 1
    return show(host_snapshot(host), as_json)

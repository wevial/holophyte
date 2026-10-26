"""`--patrol`: each project's checks for the operator seat; writes nothing."""
import json
import re
from time import time

import store.read
from holophyte.config.config_tables import patrol_config
from holophyte.holo.render import age
from holophyte.host.host_health import host_health
from holophyte.host.supervisor import SWEEPABLE_PHASES
from holophyte.loop.gates import InfraFailure
from holophyte.pr.check_holds import main_checks, pull_checks
from holophyte.pr.pr_status import parse_pr_url
from holophyte.serve.views import attention

HOST_KINDS = frozenset(("sweep_stale", "project_error", "no_store",
                        "no_project_row"))
SECTIONS = ("in_flight", "stranded", "quiet", "repeat_failures")
REASON_WIDTH = 80
SIZE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


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
        blocked = store.read.blocked_tickets(conn)
    finally:
        conn.close()
    flying = in_flight(runs, now)
    return {"in_flight": flying, "stranded": stranded(tickets),
            "quiet": quiet(flying, events, now, quiet_ms),
            "repeat_failures": repeat_failures(ended, repeat_runs),
            "pulls": pull_candidates(runs, blocked)}


def pull_candidates(runs, blocked):
    found = [(run.prUrl, run.linearIdentifier, run.id, run.prSeenOpenThreads)
             for run in runs]
    found += [(ticket.prUrl, ticket.linearIdentifier, ticket.runId,
               ticket.prSeenOpenThreads) for ticket in blocked]
    first = {}
    for found_pull in found:
        if found_pull[0]:
            first.setdefault(found_pull[0], found_pull)
    return list(first.values())


def held_pulls(target, candidates):
    held, unreadable = [], []
    for url, ticket, run, open_threads in candidates:
        pull = parse_pr_url(url)
        try:
            failed = pull_checks(target, pull) if pull is not None else []
        except InfraFailure as bad:
            unreadable.append({"ticket": ticket, "run": run, "url": url,
                               "number": pull.number, "detail": str(bad)})
            continue
        if not failed:
            continue
        held.append({"ticket": ticket, "run": run, "url": url,
                     "number": pull.number, "failed": failed,
                     "open_threads": open_threads,
                     "checks_only": open_threads == 0})
    return held, unreadable


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
    row["main"] = main_checks(target)
    held, unreadable = held_pulls(target, sections["pulls"])
    row["pull_requests"] = unignored(held, knobs.ignore, hid)
    row["unreadable_pulls"] = unignored(unreadable, knobs.ignore, hid)
    row["ignored"] = [key for key in dict.fromkeys(knobs.ignore) if key in hid]
    row["clean"] = not (row["attention"] or row["stranded"] or row["quiet"]
                        or row["repeat_failures"] or row["pull_requests"]
                        or row["unreadable_pulls"]
                        or row["main"]["state"] in ("failure", "unknown"))
    return row


def error_row(name, path, error):
    return {"name": name, "path": str(path), "error": error, "clean": False,
            "attention": [], **{key: [] for key in SECTIONS}, "main": None,
            "pull_requests": [], "unreadable_pulls": [], "ignored": []}


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
        row = guarded_row(name, target, now,
                          lambda: attention(target, now)[1]["items"])
    return {"now": now, "form": "project",
            "host": host_health(host_entries(target), []), "projects": [row]}


def host_entries(target):
    from holophyte.host.registry import Host, HostError, HostProject
    host = Host.locate()
    try:
        entries = list(host.projects())
    except HostError:
        entries = []
    path = target.path.resolve()
    if not any(entry.path == path for entry in entries):
        entries.append(HostProject(name=None, path=path, target=target))
    return entries


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
    return host, guarded_row(entry.name, entry.target, now, lambda: items)


def guarded_row(name, target, now, items):
    try:
        return project_row(name, target, items(), now)
    # Whatever one project raises is its error; the patrol goes on.
    except (Exception, SystemExit) as bad:
        return error_row(name, target.path, f"{type(bad).__name__}: {bad}")


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
    return {"now": now, "form": "host",
            "host": host_health(host.projects(), items), "projects": projects}


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


def check_names(failed):
    return ", ".join(check["name"] + " (required)" * check["required"]
                     for check in failed)


def github_lines(row):
    lines = []
    if row["main"]["state"] == "failure":
        lines.append(f"main failure: {check_names(row['main']['failed'])}")
    elif row["main"]["state"] == "unknown":
        lines.append(f"github: unreadable ({row['main']['detail']})")
    lines.extend(f"pr {pull['ticket']} #{pull['number']} held by checks:"
                 f" {check_names(pull['failed'])}"
                 f"{' (checks only)' if pull['checks_only'] else ''}"
                 for pull in row["pull_requests"])
    lines.extend(f"pr {pull['ticket']} #{pull['number']} github: unreadable"
                 f" ({pull['detail']})" for pull in row["unreadable_pulls"])
    return lines


def project_lines(row, now):
    name = f"[{row['name'] or row['path']}]"
    if row["error"]:
        return [f"{name} {row['error']}"]
    ignored = f"ignored {', '.join(row['ignored'])}" if row["ignored"] else None
    if row["clean"]:
        tail = [f"{len(row['in_flight'])} in flight"] * bool(row["in_flight"])
        return [" · ".join([f"{name} nothing to do", *tail,
                            *[ignored] * bool(ignored)])]
    lines = attention_lines(row) + section_lines(row, now) + github_lines(row)
    return [name, *(f"  {line}" for line in lines + [ignored] * bool(ignored))]


def size(count):
    for unit in SIZE_UNITS:
        if count < 1024 or unit == SIZE_UNITS[-1]:
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024


def finding_lines(health):
    lines = [f"attention {item['kind']}"
             f"{' ' + item['project'] if item.get('project') else ''}:"
             f" {item_line(item)}" for item in health["attention"]]
    tmp, kernel = health["tmp"], health["kernel"]
    if tmp.get("full"):
        lines.append(f"/tmp {tmp['used_percent']}% used,"
                     f" {size(tmp['free_bytes'])} free")
    lines.extend(f"leftover pid {process['pid']} ({age(process['age_sec'])}):"
                 f" {process['cwd']} {process['args']}"
                 for process in health["leftovers"]["processes"])
    lines.extend(f"unit {unit['unit']} {unit['state']}"
                 for unit in health["units"]["findings"])
    if kernel["errors"]:
        lines.append(f"kernel {len(kernel['errors'])} I/O errors in the last"
                     f" hour: {kernel['errors'][-1]}")
    return lines


def host_lines(health):
    skipped = [f"{label}: {health[key]['detail']}" for key, label in
               (("tmp", "/tmp"), ("leftovers", "leftovers"),
                ("units", "units"), ("kernel", "kernel log"))
               if not health[key]["readable"]]
    head = "[host] nothing to do" if health["clean"] else "[host]"
    found = [] if health["clean"] else finding_lines(health)
    return [head, *(f"  {line}" for line in found + skipped)]


def render(snap):
    lines = host_lines(snap["host"])
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

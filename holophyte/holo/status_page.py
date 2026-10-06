"""`holo status` for a person: the `--status --json` object as a page."""
import json
import shlex
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from time import time
from typing import NamedTuple

from holophyte.holo.render import (
    age,
    age_since,
    clock,
    colour_on,
    day,
    short_hash,
    symbol,
    zone,
)
from holophyte.serve.serve_host import SWEEP_TIMEOUT_SEC

STATUS_JSON = ["--status", "--json"]
SWEEP_NOW = "systemctl --user start holophyte-sweep.service"
SWEEP_LOG_HINT = "journalctl --user -u holophyte-sweep.service -n 200"
REGISTRY_HINT = "holo project list"


class Row(NamedTuple):
    mark: str
    who: str
    what: str
    text: str
    wait: str = ""


def show(target, zone_name, out=None):
    out = sys.stdout if out is None else out
    from holophyte.cli.entry import _legacy_cli
    captured = StringIO()
    try:
        with redirect_stdout(captured):
            code = _legacy_cli([*target, *STATUS_JSON])
    except SystemExit:
        out.write(captured.getvalue())
        raise
    text = captured.getvalue()
    try:
        snap = json.loads(text)
    except ValueError:
        out.write(text)
        return code
    show_snap(snap, zone_name, out)
    return code


def show_snap(snap, zone_name, out=None):
    out = sys.stdout if out is None else out
    lines = page(snap, int(time() * 1000), zone(zone_name), colour_on(out))
    print("\n".join(lines), file=out)


def page(snap, now, tz=None, colour=False):
    sweep = sweep_of(snap)
    needs, running, quiet, problems = [], [], [], []
    for project in projects_of(snap):
        sections = project_rows(project, now, swept_errors(sweep))
        for rows, more in zip((needs, running, quiet, problems), sections):
            rows += more
    problems += host_rows(snap, sweep, now)
    rows = needs + running + quiet + problems
    widths = (max((len(row.who) for row in rows), default=0),
              max((len(row.what) for row in rows), default=0),
              max((len(row.text) for row in rows if row.wait), default=0))
    lines = [f"{place(snap)} · {day(now, tz)}, {clock(now, tz)}"]
    for title, section in (("Needs you", needs), ("Running", running),
                           ("Quiet", quiet), ("Problems", problems)):
        if section:
            counted = title if title == "Quiet" else f"{title} ({len(section)})"
            lines += ["", counted]
            lines += [line(row, widths, colour) for row in section]
    if "target" not in snap:
        lines += ["", footer_line(snap, sweep, now)]
    return lines


def project_rows(project, now, errors):
    key = project["name"] or project["path"]
    who = project["name"] or Path(project["path"]).name
    status = project["store"]
    if status is None:
        return [], [], [], [Row("✗", who, "", hinted(project["error"],
                                                    REGISTRY_HINT))]
    needs = (admission_rows(who, status) + parked_rows(who, status)
             + story_rows(who, status) + stranded_rows(who, status, now))
    running = running_rows(who, status)
    trouble = lock_rows(who, status, key)
    if key in errors:
        trouble.append(Row("✗", who, "", hinted(f"sweep: {errors[key]}",
                                                SWEEP_LOG_HINT)))
    if needs or running or trouble:
        return needs, running, [], trouble
    ready = status["ready"]
    return [], [], [Row("✓", who, "", f"{ready} ready" if ready
                        else "nothing ready")], []


def projects_of(snap):
    if "target" in snap:
        return [{"name": None, "path": snap["target"], "store": snap,
                 "error": None}]
    return snap["projects"]


def place(snap):
    return f"project {snap['target']}" if "target" in snap else f"host {snap['home']}"


def line(row, widths, colour):
    columns = [symbol(row.mark, colour), row.who.ljust(widths[0])]
    if row.what:
        columns.append(row.what.ljust(widths[1]))
    columns.append(row.text.ljust(widths[2]) if row.wait else row.text)
    if row.wait:
        columns.append(row.wait)
    return ("  " + "  ".join(columns)).rstrip()


def hinted(text, hint):
    return f"{text}  try: {hint}"


def first_line(text, missing):
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else missing


def parked_rows(who, status):
    return [Row("!", who, parked["ticket"] or "",
                first_line(parked["question"], "(no question)"))
            for parked in status["parked"]]


def admission_rows(who, status):
    rows = [row for row in status["projects"] if row["admission"] != "enabled"]
    ready = f" · {status['ready']} ready" if status["ready"] else ""
    return [Row("!", who, "", (f"{row['path']} " if len(status["projects"]) > 1
                               else "")
                + f"admission {row['admission']}: "
                + first_line(row["hold_note"], "(no note)") + ready)
            for row in rows]


def story_rows(who, status):
    rows = []
    for story in status.get("stories", []):
        title = f"story \"{first_line(story['title'], '')}\""
        if story["state"] == "planned":
            rows.append(Row("!", who, story["ticket"],
                            f"{title} planned, waiting on approval"))
        elif story["state"] == "parked" or story["decisions"]:
            count = story["decisions"]
            open_ = f", {count} open decision{'s' * (count != 1)}" if count else ""
            rows.append(Row("!", who, story["ticket"], f"{title} parked{open_}"))
    return rows


def stranded_rows(who, status, now):
    return [Row("!", who, stranded["ticket"] or "",
                first_line(stranded["reason"], "(no reason)"),
                age_since(stranded["ended_ms"], now)
                if stranded["ended_ms"] is not None else "")
            for stranded in status["stranded"]]


def running_rows(who, status):
    merge = status["merge_lock"]
    merging = merge["run"] if merge and merge["stale"] is False else None
    rows = []
    for run in status["live"]:
        text = f"{run['phase']} · heartbeat {age(run['heartbeat_age_s'])} ago"
        if run["run"] == merging:
            text += " · merge lock"
        rows.append(Row(">", who, run["ticket"] or "", text))
    if merging is not None and merging not in [run["run"] for run in status["live"]]:
        rows.append(Row(">", who, "merge lock", f"run {merging}"))
    supervisor = status["supervisor_lock"]
    if supervisor and supervisor["stale"] is False:
        rows.append(Row(">", who, "supervisor", f"pid {supervisor['pid']}"))
    return rows


def lock_rows(who, status, project):
    rows = []
    supervisor = status["supervisor_lock"]
    if supervisor and supervisor["pid"] is None:
        rows.append(Row("✗", who, "", hinted(
            "supervisor lock names no pid",
            "remove its supervisor.lock once no supervisor runs")))
    elif supervisor and supervisor["stale"]:
        rows.append(Row("✗", who, "", hinted(
            f"supervisor lock names dead pid {supervisor['pid']}", SWEEP_NOW)))
    merge = status["merge_lock"]
    if merge and merge["run"] is None:
        rows.append(Row("✗", who, "", hinted(
            "merge lock names no run", "remove its merge.lock once no merge runs")))
    elif merge and merge["stale"]:
        rows.append(Row("✗", who, "", hinted(
            f"merge lock names run {merge['run']}, which is not live",
            f"holo sweep --act -p {shlex.quote(project)}")))
    return rows


def host_rows(snap, sweep, now):
    if "target" in snap:
        return []
    rows = []
    home = snap["home_lock"]
    if home and home["pid"] is None:
        rows.append(Row("✗", "host", "", hinted(
            "home lock names no pid",
            f"remove {snap['home']}/supervisor.lock once no sweep runs")))
    elif home and home["stale"]:
        rows.append(Row("✗", "host", "", hinted(
            f"home lock names dead pid {home['pid']}", SWEEP_NOW)))
    readable = {project["name"] or project["path"] for project in snap["projects"]
                if project["store"] is not None}
    errors = swept_errors(sweep)
    reasons = [f"{name}: {outcome}" for name, outcome in errors.items()
               if name not in readable]
    failure = sweep_failure(sweep, now)
    exited = failure and not sweep.get("error") and not killed(sweep, now)
    if failure and not (exited and errors):
        reasons.insert(0, failure)
    if reasons:
        rows.append(Row("✗", "sweep", "", hinted("; ".join(reasons),
                                                 SWEEP_LOG_HINT)))
    return rows


def sweep_of(snap):
    sweep = snap.get("sweep")
    if sweep and not isinstance(sweep, dict):
        return {"error": "sweep.json is not a JSON object"}
    return sweep


def swept_errors(sweep):
    outcomes = (sweep or {}).get("projects")
    if not isinstance(outcomes, dict):
        return {}
    return {name: first_line(str(outcome), "") for name, outcome in outcomes.items()
            if str(outcome).startswith("error")}


def killed(sweep, now):
    started, ended = sweep.get("started"), sweep.get("ended")
    return (isinstance(started, int) and not isinstance(ended, int)
            and now - started > SWEEP_TIMEOUT_SEC * 1000)


def sweep_failure(sweep, now):
    if not sweep:
        return None
    if sweep.get("error"):
        return first_line(str(sweep["error"]), "")
    if killed(sweep, now):
        return (f"the sweep started {age_since(sweep['started'], now)} ago"
                " never ended")
    code = sweep.get("exit")
    if isinstance(code, int) and code != 0:
        return f"the sweep exited {code}"
    return None


def footer_line(snap, sweep, now):
    parts = [sweep_part(sweep, now)]
    home = snap["home_lock"]
    if home and home["stale"] is False:
        parts.append(f"supervisor pid {home['pid']}")
    head = short_hash(snap["build"]["head"])
    parts.append(f"build {head or 'unknown'}")
    return " · ".join(parts)


def sweep_part(sweep, now):
    if not sweep:
        return "Sweep none"
    ended, started = sweep.get("ended"), sweep.get("started")
    failed = sweep_failure(sweep, now)
    if isinstance(ended, int):
        return f"Sweep {'failed' if failed else 'ok'} {age_since(ended, now)} ago"
    if isinstance(started, int):
        state = "killed" if failed else "running"
        return f"Sweep {state}, started {age_since(started, now)} ago"
    return "Sweep failed" if failed else "Sweep none"

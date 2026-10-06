"""`holo status` for a person: the `--status --json` object as a page."""
import json
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

STATUS_JSON = ["--status", "--json"]
SUPERVISE_HINT = "holo supervise --once"
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
    lines = page(snap, int(time() * 1000), zone(zone_name), colour_on(out))
    print("\n".join(lines), file=out)
    return code


def page(snap, now, tz=None, colour=False):
    projects = projects_of(snap)
    needs, running, quiet, problems = [], [], [], []
    for project in projects:
        who = project["name"] or Path(project["path"]).name
        status = project["store"]
        if status is None:
            problems.append(Row("✗", who, "", hinted(project["error"],
                                                    REGISTRY_HINT)))
            continue
        own = (parked_rows(who, status) + stranded_rows(who, status, now))
        live = running_rows(who, status)
        trouble = lock_rows(who, status, project["name"] or project["path"])
        needs += own
        running += live
        problems += trouble
        if not (own or live or trouble):
            ready = status["ready"]
            quiet.append(Row("✓", who, "",
                             f"{ready} ready" if ready else "nothing ready"))
    problems += host_rows(snap, now)
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
    footer = footer_line(snap, now)
    if footer:
        lines += ["", footer]
    return lines


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
        rows.append(Row("✗", who, "", hinted("supervisor lock names no pid",
                                             SUPERVISE_HINT)))
    elif supervisor and supervisor["stale"]:
        rows.append(Row("✗", who, "", hinted(
            f"supervisor lock names dead pid {supervisor['pid']}",
            SUPERVISE_HINT)))
    merge = status["merge_lock"]
    sweep = f"holo sweep --act -p {project}"
    if merge and merge["run"] is None:
        rows.append(Row("✗", who, "", hinted("merge lock names no run", sweep)))
    elif merge and merge["stale"]:
        rows.append(Row("✗", who, "", hinted(
            f"merge lock names run {merge['run']}, which has ended", sweep)))
    return rows


def host_rows(snap, now):
    if "target" in snap:
        return []
    rows = []
    home = snap["home_lock"]
    if home and home["pid"] is None:
        rows.append(Row("✗", "host", "", hinted("home lock names no pid",
                                                SUPERVISE_HINT)))
    elif home and home["stale"]:
        rows.append(Row("✗", "host", "", hinted(
            f"home lock names dead pid {home['pid']}", SUPERVISE_HINT)))
    failure = sweep_failure(snap["sweep"])
    if failure:
        rows.append(Row("✗", "sweep", "", hinted(failure, SWEEP_LOG_HINT)))
    return rows


def sweep_failure(sweep):
    if not sweep:
        return None
    if sweep.get("error"):
        return first_line(str(sweep["error"]), "")
    code = sweep.get("exit")
    if not isinstance(code, int) or code == 0:
        return None
    outcomes = sweep.get("projects")
    errors = [f"{name}: {outcome}" for name, outcome in
              (outcomes.items() if isinstance(outcomes, dict) else ())
              if str(outcome).startswith("error")]
    return first_line(errors[0] if errors else "", f"exit {code}")


def footer_line(snap, now):
    if "target" in snap:
        return None
    parts = [sweep_part(snap["sweep"], now)]
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
    if isinstance(ended, int):
        state = "failed" if sweep_failure(sweep) else "ok"
        return f"Sweep {state} {age_since(ended, now)} ago"
    if isinstance(started, int):
        return f"Sweep running, started {age_since(started, now)} ago"
    return "Sweep failed" if sweep_failure(sweep) else "Sweep none"

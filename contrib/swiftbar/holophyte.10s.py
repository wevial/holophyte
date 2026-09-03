#!/usr/bin/env python3
"""Holophyte drawer v0: a SwiftBar plugin over one `--serve` daemon per target.

Reads `drawer.toml` from `$HOLOPHYTE_HOME` (default `~/.holophyte`), polls
`GET /status` on every `[[daemon]]` with a two-second timeout and prints
SwiftBar's line format: the two-leaf icon with the state dot drawn inside
it (a template glyph when idle), a "needs you" section when anything needs
the operator, then one block per target. Every age is rendered from the
daemon's own `now` and its `_ms` fields, never from this machine's clock, so
two renders of the same answer are byte-identical. Standard library only.

`--render FIXTURE.json [...]` prints the menu from files instead of the
network, so a test and an operator see the exact output. A fixture is a
`/status` answer, or `{"status": ..., "runs": ...}` carrying the `/runs`
answer an idle target's "last merge" row reads.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

GREEN, AMBER, RED = "#4EA876", "#F0B13A", "#FF5F57"
IDLE, WORKING, ATTENTION, CRITICAL = 0, 1, 2, 3
DOT = {WORKING: GREEN, ATTENTION: AMBER, CRITICAL: RED}
VARIANT = {WORKING: "ok", ATTENTION: "warn", CRITICAL: "bad"}
TIMEOUT_SEC = 2
ASSETS = Path(__file__).resolve().parents[2] / "assets"
HEADER = "size=11"
# Leading spaces indent a detail row under its target; SwiftBar keeps them
# only with `trim=false`. The `--` prefix would nest a submenu instead.
DETAIL = f"trim=false {HEADER} color=#8e8e93"
INDENT = "   "


def config_path():
    home = os.environ.get("HOLOPHYTE_HOME") or os.path.expanduser("~/.holophyte")
    return Path(home) / "drawer.toml"


def load_config(path):
    """`{"daemons": [{"name", "url"}, ...], "linear": URL}` from `drawer.toml`."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    daemons = [{"name": d["name"], "url": d["url"].rstrip("/")}
               for d in raw.get("daemon", [])]
    return {"daemons": daemons, "linear": raw.get("linear", "https://linear.app")}


def fetch(url, path="/status"):
    """The parsed JSON of `url + path`, or `{"unreachable": True, "error": TEXT}`.

    A 503 (no store yet) is an answer, not an outage: its body comes back as
    is, and `render()` shows the daemon's own `error` text in the block.
    """
    try:
        with urllib.request.urlopen(url + path, timeout=TIMEOUT_SEC) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except ValueError:
            return {"unreachable": True, "error": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return {"unreachable": True, "error": str(e)}


def age(ms):
    """`4s`, `12m`, `1h02m`; `?` when the daemon carried no number."""
    if ms is None:
        return "?"
    s = max(0, int(ms) // 1000)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def coarse_age(ms):
    """An age as the daemon's own tables print one: `12s`, `7m`, `2h`, `3d`,
    the largest whole unit that fits. For the supervisor and idle rows, where
    the question is "how long has this been so", not "how many minutes".
    """
    if ms is None:
        return "?"
    s = max(0, int(ms) // 1000)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def attention(statuses):
    """`(rows, level)`: the "needs you" rows as `(text, colour)` and the worst
    level over all targets, `WORKING` counting only when no row exists.

    Rows, in order per target: unreachable daemon (red), a run whose
    heartbeat is older than the daemon's own `thresholds.heartbeat_stale_ms`
    (amber), a supervisor that is `stale` or `none` (amber).
    """
    rows, level = [], IDLE
    for entry in statuses:
        name, status = entry["name"], entry["status"]
        if status.get("unreachable"):
            rows.append((f"{name} · unreachable", RED))
            level = max(level, CRITICAL)
            continue
        stale_ms = status.get("thresholds", {}).get("heartbeat_stale_ms")
        for run in status.get("runs", []):
            beat = run.get("heartbeat_age_ms")
            if stale_ms is not None and beat is not None and beat > stale_ms:
                rows.append((f"{name} · {run['ticket']} · heartbeat {age(beat)}",
                             AMBER))
                level = max(level, ATTENTION)
            else:
                level = max(level, WORKING)
        sup = status.get("supervisor") or {}
        if sup.get("state") in ("stale", "none"):
            text = f"{name} · supervisor {sup['state']}"
            if sup.get("heartbeat_age_ms") is not None:
                text += f" · {coarse_age(sup['heartbeat_age_ms'])}"
            rows.append((text, AMBER))
            level = max(level, ATTENTION)
    return rows, level


def icon_bytes(assets=None):
    """The idle menu-bar glyph: the 20 pt PDF template, else the 1x PNG,
    else None. Never the 2x PNG: SwiftBar sizes a base64 raster by its
    pixels, so 36 px draws at twice menu-bar height.
    """
    assets = ASSETS if assets is None else Path(assets)
    for name in ("menubar-template.pdf", "menubar-template@1x.png"):
        path = assets / name
        if path.is_file():
            return path.read_bytes()
    return None


def glyph(level, appearance, assets=None):
    """`(parameter, path)` for the title-line image, or `(None, None)`.

    Idle is the template glyph. Any other level is the pre-rendered variant
    with the dot drawn at the glyph's top-right: white leaves for the dark
    bar, black for the light one. SwiftBar sets `OS_APPEARANCE` to `Dark`
    or `Light`; anything but `Dark` is treated as light. A missing variant
    yields `(None, None)` so the caller can fall back to the template.
    """
    assets = ASSETS if assets is None else Path(assets)
    if level == IDLE:
        path = assets / "menubar-template.pdf"
        return ("templateImage", path) if path.is_file() else (None, None)
    bar = "dark" if appearance == "Dark" else "light"
    path = assets / f"menubar-{bar}-{VARIANT[level]}.pdf"
    return ("image", path) if path.is_file() else (None, None)


def title(level, assets=None, appearance=None):
    """The title line. A variant image carries the dot itself; the coloured
    `●` text appears only in the fallback when the variant file is missing.
    """
    if appearance is None:
        appearance = os.environ.get("OS_APPEARANCE", "")
    parameter, path = glyph(level, appearance, assets)
    if parameter == "image":
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f" | image={encoded}"
    raw = icon_bytes(assets)
    icon = f" templateImage={base64.b64encode(raw).decode('ascii')}" if raw else ""
    if level == IDLE:
        return f" |{icon}"
    return f"● |{icon} color={DOT[level]}"


def last_merge(runs):
    """The newest `/runs` row whose `outcome` is `merged`, or None.

    `/runs` lists the report table oldest first (and `?limit=N` keeps the
    first N), so the last merge is the last merged row of the whole answer.
    None for no such row, an error body, or no answer at all.
    """
    rows = (runs or {}).get("rows") or []
    for row in reversed(rows):
        if row.get("outcome") == "merged":
            return row
    return None


def idle_row(status, runs):
    """`idle · last merge KO-n · 12m ago` from the `/runs` answer, the age
    against the daemon's `now` when the row carries `ended_ms` and dropped
    when it does not; `idle · nothing merged yet` for an answer without a
    merged row; `idle · queue empty` when `/runs` gave no answer.
    """
    if runs is None or runs.get("unreachable") or "rows" not in runs:
        return "idle · queue empty"
    row = last_merge(runs)
    if row is None:
        return "idle · nothing merged yet"
    text = f"idle · last merge {row['ticket']}"
    if row.get("ended_ms") is not None and status.get("now") is not None:
        text += f" · {coarse_age(status['now'] - row['ended_ms'])} ago"
    return text


def supervisor_row(sup):
    """`supervisor live` in the detail grey; `supervisor stale · 7m` and
    `supervisor none` in amber. The age is shown only when the state is
    the problem: against a live supervisor it is a number the reader would
    have to judge against a threshold they do not know.
    """
    state = sup.get("state", "none")
    if state == "live":
        return f"{INDENT}supervisor live | {DETAIL}"
    text = f"supervisor {state}"
    if sup.get("heartbeat_age_ms") is not None:
        text += f" · {coarse_age(sup['heartbeat_age_ms'])}"
    return f"{INDENT}{text} | trim=false {HEADER} color={AMBER}"


def target_block(entry):
    name, status = entry["name"], entry["status"]
    host = status.get("host") or "?"
    lines = [f"{name} · {host} | {HEADER}"]
    if status.get("unreachable"):
        lines.append(f"unreachable · {status.get('error', '')} | color={RED}")
        return lines
    if "runs" not in status:
        lines.append(f"{status.get('error', 'no answer')} | color={AMBER}")
        return lines
    runs = status["runs"]
    if not runs:
        lines.append(idle_row(status, entry.get("runs")))
    for run in runs:
        lines.append(f"{run['ticket']} · {run['phase']}")
        lines.append(f"{INDENT}round {run.get('round', '—')} · "
                     f"{age(run['elapsed_ms'])} of {age(run['time_box_ms'])} · "
                     f"heartbeat {age(run['heartbeat_age_ms'])} | {DETAIL}")
    lines.append(supervisor_row(status.get("supervisor") or {}))
    return lines


def render(statuses, now, linear="https://linear.app"):
    """The menu lines. `now` is the reference moment in epoch milliseconds,
    taken from the daemons' own answers (see `reference_now`); the footer's
    "updated Ns ago" is the lag of the stalest answer behind it.
    """
    rows, level = attention(statuses)
    lines = [title(level), "---"]
    if rows:
        lines.append(f"NEEDS YOU | {HEADER}")
        lines.extend(f"{text} | color={colour}" for text, colour in rows)
        lines.append("---")
    for i, entry in enumerate(statuses):
        if i:
            lines.append("---")
        lines.extend(target_block(entry))
    lines.append("---")
    lines.append(f"Open in Linear | href={linear}")
    answered = [e["status"]["now"] for e in statuses
                if "now" in e["status"] and now is not None]
    updated = f"updated {age(now - min(answered))} ago" if answered else "no answer"
    lines.append(f"{footer_counts(statuses)} · {updated} | {HEADER}")
    return lines


def footer_counts(statuses):
    """`N targets · M hosts`: one target per daemon, M the distinct `host`
    values the daemons report (an unreachable daemon reports none). The
    `targets` noun is fixed (the ticket's verify step greps `targets ·` on
    the one-daemon fixture); `host` takes the singular at one.
    """
    hosts = {e["status"].get("host") for e in statuses if e["status"].get("host")}
    host = "host" if len(hosts) == 1 else "hosts"
    return f"{len(statuses)} targets · {len(hosts)} {host}"


def fixture_entry(path):
    """A `--render` entry from a file: a bare `/status` answer, or an object
    with `status` and `runs` keys carrying both answers."""
    raw = json.loads(Path(path).read_text())
    if "status" in raw and "runs" in raw and "now" not in raw:
        status, runs = raw["status"], raw["runs"]
    else:
        status, runs = raw, None
    return {"name": Path(path).stem, "url": str(path), "status": status,
            "runs": runs}


def poll(daemon):
    """One daemon's entry: `/status`, then `/runs` only for an idle target,
    so a working daemon still costs one request."""
    status = fetch(daemon["url"])
    runs = fetch(daemon["url"], "/runs") if status.get("runs") == [] else None
    return {"name": daemon["name"], "url": daemon["url"], "status": status,
            "runs": runs}


def reference_now(statuses):
    """The latest `now` any daemon answered with, None when none did."""
    nows = [e["status"]["now"] for e in statuses if "now" in e["status"]]
    return max(nows) if nows else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--render", nargs="+", metavar="FIXTURE.json",
                        help="render these /status answers instead of polling")
    parser.add_argument("--config", type=Path, default=None,
                        help=f"drawer.toml to read (default {config_path()})")
    args = parser.parse_args(argv)
    linear = "https://linear.app"
    if args.render:
        statuses = [fixture_entry(p) for p in args.render]
    else:
        config = load_config(args.config or config_path())
        linear = config["linear"]
        statuses = [poll(d) for d in config["daemons"]]
    for line in render(statuses, reference_now(statuses), linear):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

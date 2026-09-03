#!/usr/bin/env python3
"""Holophyte drawer v0: a SwiftBar plugin over one `--serve` daemon per target.

Reads `drawer.toml` from `$HOLOPHYTE_HOME` (default `~/.holophyte`), polls
`GET /status` on every `[[daemon]]` with a two-second timeout and prints
SwiftBar's line format: the two-leaf template icon with a coloured dot beside
it, a "needs you" section when anything needs the operator, then one block
per target. Every age is rendered from the daemon's own `now` and its `_ms`
fields, never from this machine's clock, so two renders of the same answer
are byte-identical. Standard library only.

`--render FIXTURE.json [...]` prints the menu from files instead of the
network, so a test and an operator see the exact output.
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
TIMEOUT_SEC = 2
ICON = Path(__file__).resolve().parents[2] / "assets" / "menubar-template@2x.png"
DETAIL = "size=12 font=Menlo"
HEADER = "size=11"


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


def fetch(url):
    """The parsed `/status` JSON, or `{"unreachable": True, "error": TEXT}`.

    A 503 (no store yet) is an answer, not an outage: its body comes back as
    is, and `render()` shows the daemon's own `error` text in the block.
    """
    try:
        with urllib.request.urlopen(url + "/status", timeout=TIMEOUT_SEC) as r:
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
                text += f" · {age(sup['heartbeat_age_ms'])}"
            rows.append((text, AMBER))
            level = max(level, ATTENTION)
    return rows, level


def title(level):
    icon = base64.b64encode(ICON.read_bytes()).decode("ascii")
    if level == IDLE:
        return f" | templateImage={icon}"
    return f"● | templateImage={icon} color={DOT[level]}"


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
        lines.append("idle · queue empty")
    for run in runs:
        lines.append(f"{run['ticket']} · {run['phase']}")
        lines.append(f"--round {run.get('round', '—')} · "
                     f"{age(run['elapsed_ms'])} of {age(run['time_box_ms'])} · "
                     f"heartbeat {age(run['heartbeat_age_ms'])} | {DETAIL}")
    sup = status.get("supervisor") or {}
    beat = sup.get("heartbeat_age_ms")
    tail = f" · {age(beat)}" if beat is not None else ""
    lines.append(f"--supervisor {sup.get('state', 'none')}{tail} | {DETAIL}")
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
    lines.append(f"{len(statuses)} hosts · {updated} | {HEADER}")
    return lines


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
        statuses = [{"name": Path(p).stem, "url": p,
                     "status": json.loads(Path(p).read_text())}
                    for p in args.render]
    else:
        config = load_config(args.config or config_path())
        linear = config["linear"]
        statuses = [{"name": d["name"], "url": d["url"],
                     "status": fetch(d["url"])} for d in config["daemons"]]
    for line in render(statuses, reference_now(statuses), linear):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

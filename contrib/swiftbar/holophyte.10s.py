#!/usr/bin/env python3
"""Holophyte drawer v0: a SwiftBar plugin over one `--serve` daemon per target.

Reads `drawer.toml` from `$HOLOPHYTE_HOME` (default `~/.holophyte`), polls
`GET /status` and `GET /attention` on every `[[daemon]]` with a two-second
timeout and prints
SwiftBar's line format: the two-leaf icon with the state dot drawn inside
it (a template glyph when idle), a "needs you" section when anything needs
the operator, then one block per target. Every age is rendered from the
daemon's own `now` and its `_ms` fields, never from this machine's clock, so
two renders of the same answer are byte-identical. Standard library only.

`--render FIXTURE.json [...]` prints the menu from files instead of the
network, so a test and an operator see the exact output. A fixture is a
`/status` answer, or `{"status": ..., "runs": ..., "attention": ...}`
carrying the `/runs` answer an idle target's "last merge" row reads and the
`/attention` answer the "needs you" section renders.

"Needs you" is the daemon's own `/attention` items when it answers them;
a daemon too old for the path (404) gets the local rule over its `/status`
instead, so a half-upgraded tailnet still renders. Any other `/attention`
failure (a timeout, a 5xx) is a red row of its own, since the items it hid
are the ones this side cannot compute.
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
    is, and `render()` shows the daemon's own `error` text in the block. An
    error body is tagged with its code as `http_status`, so a caller can
    tell a 404 (a daemon older than the path) from a 500.
    """
    try:
        with urllib.request.urlopen(url + path, timeout=TIMEOUT_SEC) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            return {"unreachable": True, "error": f"HTTP {e.code}",
                    "http_status": e.code}
        if isinstance(body, dict):
            body.setdefault("http_status", e.code)
        return body
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


LEVELS = {"none": IDLE, "working": WORKING, "attention": ATTENTION,
          "critical": CRITICAL}
QUESTION_CHARS, REASON_CHARS = 60, 40


def cut(text, limit):
    """`text` on one line, at most `limit` characters, an ellipsis when cut."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def item_row(name, item, now):
    """The "needs you" text for one `/attention` item, target-prefixed, or
    None for a kind this drawer does not know (a newer daemon's item is
    skipped rather than misrendered)."""
    kind, ticket = item.get("kind"), item.get("ticket")
    if kind == "blocked":
        question = cut(item.get("question"), QUESTION_CHARS)
        return f"{name} · {ticket} · blocked: {question}"
    if kind == "stale_run":
        return f"{name} · {ticket} · heartbeat {age(item.get('heartbeat_age_ms'))}"
    if kind == "failed":
        text = f"{name} · {ticket} · failed"
        if item.get("ended_ms") is not None and now is not None:
            text += f" {coarse_age(now - item['ended_ms'])} ago"
        if item.get("reason"):
            text += f": {cut(item['reason'], REASON_CHARS)}"
        return text
    if kind == "supervisor":
        text = f"{name} · supervisor {item.get('state', 'none')}"
        if item.get("heartbeat_age_ms") is not None:
            text += f" · {coarse_age(item['heartbeat_age_ms'])}"
        return text
    return None


def daemon_rows(entry):
    """`(rows, level)` from the daemon's `/attention` answer: one row per
    item in the daemon's order, the level the daemon's own word."""
    name, answer = entry["name"], entry["attention"]
    rows = []
    for item in answer.get("items", []):
        text = item_row(name, item, answer.get("now"))
        if text is not None:
            rows.append((text, DOT.get(LEVELS.get(item.get("level"), ATTENTION),
                                       AMBER)))
    return rows, LEVELS.get(answer.get("level"), ATTENTION if rows else IDLE)


def local_rows(entry):
    """`(rows, level)` computed from `/status` alone, for a daemon that
    does not answer `/attention`: a run whose heartbeat is older than the
    daemon's own `thresholds.heartbeat_stale_ms` (amber), a supervisor
    that is `stale` or `none` (amber)."""
    name, status = entry["name"], entry["status"]
    rows, level = [], IDLE
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


def attention_error(answer):
    """Why an `/attention` answer cannot be rendered, or None when it can
    or when it is the 404 of a daemon older than the path (a fixture with
    no `attention` key is that case too). Anything else, a timeout or a
    5xx after a good `/status`, is a failure the operator must see: the
    items it would have carried are exactly the ones this drawer cannot
    compute itself."""
    if answer is None:
        return None
    if not isinstance(answer, dict):
        return "malformed answer"
    if isinstance(answer.get("items"), list):
        return None
    if answer.get("http_status") == 404:
        return None
    if answer.get("unreachable"):
        return str(answer.get("error") or "unreachable")
    if answer.get("http_status") is not None:
        return f"HTTP {answer['http_status']}"
    return "no items in answer"


def attention_rows(entry):
    """`(rows, level)` for one target: `NAME · unreachable` in red and
    `CRITICAL` when the daemon did not answer `/status`; else the daemon's
    own `/attention` items when `entry["attention"]` carries them; else
    the local rule over `/status` for a daemon that answers 404 there (or
    a fixture with no `attention` key). Any other `/attention` failure
    is a red `NAME · /attention failed: WHY` row at `CRITICAL`, followed
    by whatever the local rule still shows, never a silent fallback."""
    if entry["status"].get("unreachable"):
        return [(f"{entry['name']} · unreachable", RED)], CRITICAL
    answer = entry.get("attention")
    if isinstance(answer, dict) and isinstance(answer.get("items"), list):
        return daemon_rows(entry)
    rows, level = local_rows(entry)
    why = attention_error(answer)
    if why is not None:
        row = (f"{entry['name']} · /attention failed: {cut(why, REASON_CHARS)}", RED)
        return [row] + rows, CRITICAL
    return rows, level


def attention(statuses):
    """`(rows, level)`: the "needs you" rows as `(text, colour)` over all
    targets in config order and the worst level among them."""
    rows, level = [], IDLE
    for entry in statuses:
        got, got_level = attention_rows(entry)
        rows.extend(got)
        level = max(level, got_level)
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


def glyph(level, assets=None):
    """`(parameter, path)` for the title-line image, or `(None, None)`.

    Idle is the template glyph. Any other level is the pre-rendered variant
    with the dot drawn at the glyph's top-right. One design per level:
    macOS tints the menu bar from the wallpaper, not the system appearance,
    and nothing SwiftBar passes says which, so the variant carries its own
    contrast (white leaves with a thin dark outline). A missing variant
    yields `(None, None)` so the caller can fall back to the template.
    """
    assets = ASSETS if assets is None else Path(assets)
    if level == IDLE:
        path = assets / "menubar-template.pdf"
        return ("templateImage", path) if path.is_file() else (None, None)
    path = assets / f"menubar-{VARIANT[level]}.pdf"
    return ("image", path) if path.is_file() else (None, None)


def title(level, assets=None):
    """The title line. A variant image carries the dot itself; the coloured
    `●` text appears only in the fallback when the variant file is missing.
    """
    parameter, path = glyph(level, assets)
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
    with a `status` key and optional `runs` and `attention` keys carrying
    those answers too."""
    raw = json.loads(Path(path).read_text())
    if "status" in raw and "now" not in raw:
        status, runs, att = raw["status"], raw.get("runs"), raw.get("attention")
    else:
        status, runs, att = raw, None, None
    return {"name": Path(path).stem, "url": str(path), "status": status,
            "runs": runs, "attention": att}


def poll(daemon):
    """One daemon's entry: `/status` and `/attention`, then `/runs` only for
    an idle target. A daemon that did not answer `/status` is not asked
    again, so a dead host costs one timeout, not three."""
    status = fetch(daemon["url"])
    if status.get("unreachable"):
        att, runs = None, None
    else:
        att = fetch(daemon["url"], "/attention")
        runs = fetch(daemon["url"], "/runs") if status.get("runs") == [] else None
    return {"name": daemon["name"], "url": daemon["url"], "status": status,
            "runs": runs, "attention": att}


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

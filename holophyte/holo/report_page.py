"""`holo report`: a window's counts first, then its runs, its notes on request."""
import json
import re
import statistics
import sys
from pathlib import Path
from time import time

import store.read
from holophyte.cli.report import ended_rows
from holophyte.config.project import Project
from holophyte.holo.render import age_since, clock, colour_on, day, symbol, zone
from holophyte.serve.serve_runs import json_host, no_store
from store.gap_layers import gap_finder_counts, gap_layer_counts

DEFAULT_SINCE = "7d"
ALL = "all"
SPAN = re.compile(r"([1-9][0-9]*)([hd])")
UNIT_MS = {"h": 3_600_000, "d": 24 * 3_600_000}
UNIT_WORD = {"h": "hour", "d": "day"}
FORMS = "Nh, Nd or all, such as 24h, 7d, 30d or all"
SHIPPED = ("merged", "abandoned", "failed")
OPEN_LAYER = "none"
SEND_BACK = "operator_note"
MARKS = {"merged": "✓", "failed": "✗"}
LABEL = 10


def window_ms(since):
    if since == ALL:
        return None
    match = SPAN.fullmatch(since)
    if match is None:
        raise ValueError(f"--since takes {FORMS}, not {since!r}")
    return int(match[1]) * UNIT_MS[match[2]]


def window_words(since):
    if since == ALL:
        return "all time"
    count, unit = SPAN.fullmatch(since).groups()
    return f"last {count} {UNIT_WORD[unit]}{'s' * (count != '1')}"


def within(since):
    return "in the store's history" if since == ALL else f"in the {window_words(since)}"


def plural(count, word):
    return f"{count} {word}{'s' * (count != 1)}"


def median(values):
    values = [value for value in values if value is not None]
    return statistics.median(values) if values else None


def run_row(project, row):
    (ticket, actual, agent, verify, estimate, ratio, rounds, outcome, host,
     ended_at, merge_sha, wall_min, run_id) = row
    return {"run": run_id, "ticket": ticket, "actual_min": actual,
            "agent_min": agent, "verify_min": verify, "estimate_min": estimate,
            "ratio": ratio, "rounds": rounds, "outcome": outcome,
            "host": json_host(project, host), "ended_ms": ended_at,
            "merge_sha": merge_sha, "wall_min": wall_min}


def shipped(rows):
    merged = [row for row in rows if row["outcome"] == "merged"]
    counts = {outcome: sum(row["outcome"] == outcome for row in rows)
              for outcome in SHIPPED}
    return {**counts,
            "median_min": median(row["actual_min"] for row in merged),
            "median_estimate_min": median(row["estimate_min"] for row in merged)}


def failures(conn, start):
    return dict(conn.execute(
        "SELECT COALESCE(failureKind, 'unclassified'), COUNT(*) FROM runs"
        " WHERE outcome = 'failed' AND endedAt >= ?"
        " GROUP BY 1 ORDER BY COUNT(*) DESC, 1", (start,)))


def gaps(conn):
    layers = gap_layer_counts(conn)
    return {"open": layers[OPEN_LAYER], "layers": layers,
            "found_by": gap_finder_counts(conn)}


def hands_on(conn, start):
    by_action = store.read.toil_since(conn, start).by_action
    send_backs = by_action.pop(SEND_BACK, 0)
    return {"interventions": sum(by_action.values()), "by_action": by_action,
            "send_backs": send_backs}


def consumed_notes(conn, start):
    rows = conn.execute(
        "SELECT c.runId, t.linearIdentifier, c.payload, c.at, n.payload"
        " FROM runEvents c"
        " JOIN runEvents n ON n.id = json_extract(c.payload, '$.event_id')"
        " JOIN runs r ON r.id = c.runId JOIN tickets t ON t.id = r.ticketId"
        " WHERE c.kind = 'operator_note_consumed' AND c.at >= ?"
        " ORDER BY c.at DESC, c.id DESC", (start,))
    notes = []
    for run_id, ticket, consumed, at, payload in rows:
        consumption, instruction = json.loads(consumed), json.loads(payload)
        notes.append({"run": run_id, "ticket": ticket,
                      "round": consumption["round"],
                      "event_id": consumption["event_id"],
                      "author": instruction["author"],
                      "note": instruction["note"], "consumed_ms": at})
    return notes


def report(conn, project, since, now):
    span = window_ms(since)
    start = 0 if span is None else now - span
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        rows = [run_row(project, row) for row in ended_rows(conn)
                if row[9] >= start]
        body = {"project": str(project.path),
                "window": {"since": since,
                           "from_ms": None if span is None else start,
                           "now_ms": now},
                "shipped": shipped(rows), "failures": failures(conn, start),
                "gaps": gaps(conn), "hands_on": hands_on(conn, start),
                "runs": rows, "notes": consumed_notes(conn, start)}
    finally:
        if owns_transaction:
            conn.rollback()
    return body


def counted(parts, empty):
    return " · ".join(f"{name} {count}" for name, count in parts if count) or empty


def minutes(value):
    return "-" if value is None else f"{value:.0f}"


def shipped_line(body):
    counts = body["shipped"]
    if not any(counts[outcome] for outcome in SHIPPED):
        return f"nothing shipped {within(body['window']['since'])}"
    text = " · ".join(f"{counts[outcome]} {outcome}" for outcome in SHIPPED)
    if counts["median_min"] is not None:
        text += f" · median {minutes(counts['median_min'])} min per ticket"
        if counts["median_estimate_min"] is not None:
            text += f" (estimate {minutes(counts['median_estimate_min'])})"
    return text


def count_lines(body):
    gap = body["gaps"]
    hands = body["hands_on"]
    by_action = counted(hands["by_action"].items(), "")
    return [
        ("Shipped", shipped_line(body)),
        ("Failures", counted(body["failures"].items(), "none")),
        ("Gaps", " · ".join([f"{gap['open']} open"] + [
            f"{layer} {count}" for layer, count in gap["layers"].items()
            if count and layer != OPEN_LAYER])),
        ("Hands-on", plural(hands["interventions"], "intervention")
         + (f" ({by_action})" if by_action else "")
         + f" · {plural(hands['send_backs'], 'send-back')}"),
    ]


def run_lines(body, colour):
    now = body["window"]["now_ms"]
    table = [(row["ticket"] or "", row["outcome"],
              f"{minutes(row['actual_min'])} of {minutes(row['estimate_min'])} min",
              f"{row['rounds']} rounds", f"{age_since(row['ended_ms'], now)} ago")
             for row in body["runs"]]
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [f"Runs ({len(table)})"]
    for row, cells in zip(body["runs"], table):
        mark = MARKS.get(row["outcome"])
        columns = [symbol(mark, colour) if mark else "·"]
        columns += [cell.ljust(width) for cell, width in zip(cells, widths)]
        lines.append(("  " + "  ".join(columns)).rstrip())
    return lines


def note_lines(body, tz):
    lines = [f"Notes ({len(body['notes'])})"]
    for note in body["notes"]:
        at = note["consumed_ms"]
        author = repr(note["author"])[1:-1]
        text = repr(note["note"])[1:-1]
        lines.append(f"  {day(at, tz)}, {clock(at, tz)}  {note['ticket']}"
                     f" run {note['run']} round {note['round']}"
                     f"  {author}: {text}")
    return lines


def page(body, tz=None, colour=False, notes=False):
    name = Path(body["project"]).name
    lines = [f"{name} · {window_words(body['window']['since'])}", ""]
    lines += [f"{label.ljust(LABEL)}{text}" for label, text in count_lines(body)]
    if body["runs"]:
        lines += [""] + run_lines(body, colour)
    if notes:
        lines += [""] + note_lines(body, tz)
    return lines


def show(args, target, zone_name, out=None):
    out = sys.stdout if out is None else out
    since = args.since[-1] if args.since else DEFAULT_SINCE
    try:
        window_ms(since)
    except ValueError as bad:
        args.leaf.error(str(bad))
    project = Project.locate(Path(target), adopt=False)
    if not project.store_path.exists():
        missing = no_store(project)
        print(f"[holo2] report: {missing['error']}: {missing['detail']}",
              file=sys.stderr)
        return 1
    conn = store.read.open_readonly(project.store_path)
    try:
        body = report(conn, project, since, int(time() * 1000))
    finally:
        conn.close()
    if args.json:
        print(json.dumps(body), file=out)
        return 0
    lines = page(body, zone(zone_name), colour_on(out), args.notes)
    print("\n".join(lines), file=out)
    return 0

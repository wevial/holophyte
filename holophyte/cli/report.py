import json
import statistics
import time
from collections import Counter
from datetime import datetime, timezone

import store.read
import store.schema
from holophyte.config.config_tables import report_config
from holophyte.story.story_views import story_report_lines
from store.gap_layers import gap_finder_counts, gap_layer_counts
from store.working import agent_work, effective_work, verify_work

REPORT_HEADERS = ("ticket", "actual", "agent", "verify", "estimate", "ratio",
                  "rounds", "outcome", "rejected", "host")
REPORT_GAP = "  "


def migration_line(note, version):
    detail = json.loads(note)
    at = datetime.fromtimestamp(detail["at"] / 1000, timezone.utc).isoformat()
    argv = detail["argv"]
    return (f"store schema {version} (migrated from {detail['from']} at {at}"
            f" by {detail['build']}, pid {detail['pid']},"
            f" {argv[0] if argv else 'unknown'})")


def migration_header(conn):
    note = store.schema.latest_migration_note(conn)
    if note is None:
        return []
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    return [migration_line(note, version)]


def live_rows(conn):
    return conn.execute(
        "SELECT t.linearIdentifier, r.phase, r.startedAt, r.lastHeartbeat,"
        " r.prUrl FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NULL ORDER BY r.startedAt").fetchall()


def live_lines(conn, now):
    rows = live_rows(conn)
    if not rows:
        return ["in flight: none"]
    table = [(ticket, phase, format_age(now - started),
              format_age(now - heartbeat), url or "")
             for ticket, phase, started, heartbeat, url in rows]
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = []
    for row in table:
        cells = [cell.rjust(width) if i in (2, 3) else cell.ljust(width)
                 for i, (cell, width) in enumerate(zip(row, widths))]
        cells[3] = "hb " + cells[3]
        lines.append(REPORT_GAP.join(cells).rstrip())
    return ["in flight:"] + lines


def report_rows(conn):
    return [row[:9] for row in ended_rows(conn)]


def ended_rows(conn):
    rows = []
    for run in store.read.ended_runs(conn):
        actual, agent, verify = (
            ms / 60000 if ms is not None else None
            for ms in (effective_work(run, run.endedAt),
                       agent_work(run, run.endedAt),
                       verify_work(run, run.endedAt)))
        estimate = run.timeBoxMs / 60000 if run.timeBoxMs else None
        rows.append((run.linearIdentifier, actual, agent, verify, estimate,
                     actual / estimate if estimate and actual is not None else None,
                     run.reviewRoundCount, run.outcome or "ended", run.host,
                     run.endedAt, run.mergeSha,
                     (run.endedAt - run.startedAt) / 60000, run.id))
    return rows


def report_summary(rows):
    ratios = [row[5] for row in rows if row[5] is not None]
    if not ratios:
        return f"{len(rows)} runs · no estimates to compare against"
    counted = (f"{len(rows)} runs" if len(ratios) == len(rows)
               else f"{len(rows)} runs · {len(ratios)} with an estimate")
    return (f"{counted} · mean ratio {statistics.fmean(ratios):.2f}"
            f" · median ratio {statistics.median(ratios):.2f}")


def failure_lines(conn):
    return [f"failures {kind}: {count}" for kind, count in conn.execute(
        "SELECT COALESCE(failureKind, 'unclassified'), COUNT(*) FROM runs"
        " WHERE outcome = 'failed' GROUP BY 1 ORDER BY 1")]


def flaky_lines(conn):
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM runEvents WHERE kind = 'verify_flaky'").fetchone()
    return [f"verify flaky: {count}"] if count else []


TRIM_OUTCOMES = ("kept", "partial", "reverted", "skipped", "nothing")


def trim_lines(conn):
    results = [json.loads(payload) for (payload,) in conn.execute(
        "SELECT payload FROM runEvents WHERE kind = 'trim_result'")]
    if not results:
        return []
    counts = Counter(result["outcome"] for result in results)
    net = sum(result["lines_after"] - result["lines_before"] for result in results)
    return [f"trim: {len(results)} runs · "
            + " · ".join(f"{outcome} {counts[outcome]}" for outcome in TRIM_OUTCOMES)
            + f" · net {net:+d} lines"]


TOIL_WINDOWS = (("24h", 24 * 3_600_000), ("7d", 7 * 24 * 3_600_000))


def toil_status(conn, now):
    body = {}
    for label, span in TOIL_WINDOWS:
        toil = store.read.toil_since(conn, now - span)
        count = sum(toil.by_action.values())
        body[label] = {
            "interventions": count, "merged": toil.merged,
            "per_merge": count / toil.merged if toil.merged else None,
            "by_action": toil.by_action}
    return body


def toil_lines(conn, now):
    lines = []
    for label, window in toil_status(conn, now).items():
        rate = ("" if window["per_merge"] is None
                else f", {window['per_merge']:.2f} per merge")
        actions = ", ".join(f"{action} {n}"
                            for action, n in window["by_action"].items())
        lines.append(f"toil {label}: {window['interventions']} human"
                     f" interventions, {window['merged']} merged{rate}"
                     + (f" ({actions})" if actions else ""))
    return lines


def approval_lines(conn):
    rows = conn.execute(
        "SELECT t.linearIdentifier, r.id, r.approvedBy, r.approvedAt"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.approvedAt IS NOT NULL ORDER BY r.id").fetchall()
    return [
        f"{ticket} run {run}: approved by {operator} at "
        f"{datetime.fromtimestamp(at / 1000, timezone.utc).isoformat()}"
        for ticket, run, operator, at in rows
    ]


def report_lines(conn, target=None):
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        now = int(time.time() * 1000)
        live = live_lines(conn, now) + [""]
        rows = report_rows(conn)
        from store.operator_notes import report_lines as note_lines
        live += note_lines(conn)
        live += approval_lines(conn)
        live += failure_lines(conn)
        live += flaky_lines(conn)
        live += trim_lines(conn)
        live += toil_lines(conn, now)
        live.append("gap layers: " + ", ".join(
            f"{layer} {count}"
            for layer, count in gap_layer_counts(conn).items()))
        finders = gap_finder_counts(conn)
        live.append(f"gaps found: witness {finders['witness']},"
                    f" operator {finders['operator']}")
        live += story_report_lines(conn)
    finally:
        if owns_transaction:
            conn.rollback()
    if not rows:
        return live + ["no completed runs yet"]
    table = [REPORT_HEADERS]
    for (ticket, actual, agent, verify, estimate, ratio, rounds, outcome,
         host) in rows:
        table.append((
            ticket,
            *(f"{minutes:.1f}" if minutes is not None else "n/a"
              for minutes in (actual, agent, verify)),
            f"{estimate:.0f}" if estimate is not None else "n/a",
            f"{ratio:.2f}" if ratio is not None else "n/a",
            str(rounds),
            outcome,
            str(int(outcome == "rejected")),
            host_label(target, host),
        ))
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [
        REPORT_GAP.join(
            cell.ljust(width) if i in (0, 7, 9) else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))).rstrip()
        for row in table
    ]
    return live + lines + [report_summary(rows)]


def format_age(ms):
    seconds = max(0, int(ms // 1000))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def host_name(host):
    return "?" if host is None else host


def host_label(target, host):
    """The store keeps the real hostname for the own-host checks."""
    label = report_config(target).host_label if target is not None else None
    return host_name(host) if host is None or label is None else label

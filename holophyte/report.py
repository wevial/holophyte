"""Live runs and the finished estimate-vs-actual table, read-only.

The report shares age and host formatting with supervisor and sweep output.
Opening the store belongs to the callers in operator and supervisor.
"""
import statistics
import time

import store.read
from holophyte.config_tables import report_config

# --- estimate vs actual ------------------------------------------------------
# The rows already carry every number a burndown needs: when a run started and
# ended, the estimate it was claimed under, how many rounds it took. So the
# report is a query and an aligned print rather than a grep over FINDINGS.md --
# the ledger line was the only reading of this data until now, and a rendering
# of the newest 25 entries is not something a calibration question can be
# asked of. Nothing here writes, claims or calls Linear.
REPORT_HEADERS = ("ticket", "actual", "estimate", "ratio", "rounds", "outcome",
                  "rejected", "host")
REPORT_GAP = "  "


def live_rows(conn):
    """Unfinished runs as (ticket, phase, started_at, heartbeat, pr_url).
    Include every phase with no end timestamp, oldest claim first.
    """
    return conn.execute(
        "SELECT t.linearIdentifier, r.phase, r.startedAt, r.lastHeartbeat,"
        " r.prUrl FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NULL ORDER BY r.startedAt").fetchall()


def live_lines(conn, now):
    """An aligned in-flight block, with ages relative to epoch milliseconds."""
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
    """Ended runs, oldest first: ticket, actual_min, estimate_min, ratio,
    rounds, outcome, host. Missing estimates and ratios are None and excluded
    from averages; a missing host marks a run older than that column.
    """
    return [row[:-2] for row in ended_rows(conn)]


def ended_rows(conn):
    """Report tuples with ended_at (epoch ms) and merge_sha appended.

    The daemon uses these to show when a run ended and link its merge;
    report_rows() drops them to preserve the terminal table's shape.
    """
    rows = []
    for run in store.read.ended_runs(conn):
        actual = (run.endedAt - run.startedAt) / 60000
        estimate = run.timeBoxMs / 60000 if run.timeBoxMs else None
        rows.append((run.linearIdentifier, actual, estimate,
                     actual / estimate if estimate else None,
                     run.reviewRoundCount, run.outcome or "ended", run.host,
                     run.endedAt, run.mergeSha))
    return rows


def report_summary(rows):
    """Run count and mean/median ratios, excluding runs with no estimate."""
    ratios = [row[3] for row in rows if row[3] is not None]
    if not ratios:
        return f"{len(rows)} runs · no estimates to compare against"
    counted = (f"{len(rows)} runs" if len(ratios) == len(rows)
               else f"{len(rows)} runs · {len(ratios)} with an estimate")
    return (f"{counted} · mean ratio {statistics.fmean(ratios):.2f}"
            f" · median ratio {statistics.median(ratios):.2f}")


def report_lines(conn, target=None):
    """Live work, then the finished table and its summary, as lines.

    Columns are padded to the widest cell in them so the numbers line up in a
    terminal; the ticket, the outcome and the host read left, everything
    numeric reads right. A store with no ended run says so rather than printing a header
    over nothing. `target` is where the `[report] host_label` comes from;
    without one the host column is the hostname the store holds.
    """
    live = live_lines(conn, int(time.time() * 1000)) + [""]
    rows = report_rows(conn)
    if not rows:
        return live + ["no completed runs yet"]
    table = [REPORT_HEADERS]
    for ticket, actual, estimate, ratio, rounds, outcome, host in rows:
        table.append((
            ticket,
            f"{actual:.1f}",
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
            cell.ljust(width) if i in (0, 5, 7) else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))).rstrip()
        for row in table
    ]
    return live + lines + [report_summary(rows)]


def format_age(ms):
    """An age in milliseconds as an operator reads one: `12s`, `9m`, `3h`.

    Whole units, largest that fits, because the question the age answers --
    is the watcher a minute quiet or an evening quiet -- is not one that
    turns on the seconds past the hour.
    """
    seconds = max(0, int(ms // 1000))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def host_name(host):
    """A `host` column as printed: the hostname, or `?` for a row without one."""
    return "?" if host is None else host


def host_label(target, host):
    """A `host` column as a rendering shows it: the label, or `host_name()`.

    `[report] host_label` in `target`'s config replaces the hostname wherever
    the factory renders one -- the report and sweep tables and the
    supervisor's lines; the FINDINGS window a public repository commits has
    no host column to replace -- while the store goes on holding the real
    hostname for the supervisor's own-host checks.
    With no label (or no `target`), this is `host_name(host)` exactly; so
    is a `host` of None, label or not: a row older than the column has no
    recorded host, and calling it the writer would state something the
    store does not know.
    """
    label = report_config(target).host_label if target is not None else None
    return host_name(host) if host is None or label is None else label

"""The estimate-vs-actual report: ended runs as an aligned table.

One tuple per ended run, the summary line with the mean and median ratio,
the padded lines an operator reads, and the two formatting helpers the
report shares with the supervisor's liveness line and the sweep table --
`format_age` for a heartbeat's age, `host_name` for a `host` column and
`host_label` for that column as a public rendering shows it.
Read-only: `store.read` and the standard library, and nothing that writes,
claims or calls Linear. Opening the store is `report()`'s job in
`holophyte.loop` and `supervisor_liveness_line()`'s in `holophyte.supervisor`.

Fourth slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import statistics

import store.read
from holophyte.config import report_config

# --- estimate vs actual ------------------------------------------------------
# The rows already carry every number a burndown needs: when a run started and
# ended, the estimate it was claimed under, how many rounds it took. So the
# report is a query and an aligned print rather than a grep over FINDINGS.md --
# the ledger line was the only reading of this data until now, and a rendering
# of the newest 25 entries is not something a calibration question can be
# asked of. Nothing here writes, claims or calls Linear.
REPORT_HEADERS = ("ticket", "actual", "estimate", "ratio", "rounds", "outcome",
                  "host")
REPORT_GAP = "  "


def report_rows(conn):
    """Every ended run, oldest first, as the report's own tuple.

    `(ticket, actual_min, estimate_min, ratio, rounds, outcome, host)`, with
    `estimate` and `ratio` None when the run was claimed against no estimate
    -- an older run, or a ticket Linear gave no points. None rather than zero
    because "not comparable" is not a ratio of nothing, and the summary below
    leaves those runs out of its averages instead of dragging them to 0.
    `host` is the machine the run was claimed on, None for a row older than
    the column: a store read from another machine says where each run ran.
    """
    rows = []
    for run in store.read.ended_runs(conn):
        actual = (run.endedAt - run.startedAt) / 60000
        estimate = run.timeBoxMs / 60000 if run.timeBoxMs else None
        rows.append((run.linearIdentifier, actual, estimate,
                     actual / estimate if estimate else None,
                     run.reviewRoundCount, run.outcome or "ended", run.host))
    return rows


def report_summary(rows):
    """The last line: how many runs, and how the ratios sit.

    Mean and median together, because they answer different halves of the
    calibration question -- the mean carries the one run that blew its budget,
    the median says what a typical ticket costs -- and the early signal worth
    watching (actuals running several times under estimate) is a claim about
    the middle, not about the total.
    """
    ratios = [row[3] for row in rows if row[3] is not None]
    if not ratios:
        return f"{len(rows)} runs · no estimates to compare against"
    counted = (f"{len(rows)} runs" if len(ratios) == len(rows)
               else f"{len(rows)} runs · {len(ratios)} with an estimate")
    return (f"{counted} · mean ratio {statistics.fmean(ratios):.2f}"
            f" · median ratio {statistics.median(ratios):.2f}")


def report_lines(conn, target=None):
    """The whole report as lines: a header, one line per ended run, a summary.

    Columns are padded to the widest cell in them so the numbers line up in a
    terminal; the ticket, the outcome and the host read left, everything
    numeric reads right. A store with no ended run says so rather than printing a header
    over nothing. `target` is where the `[report] host_label` comes from;
    without one the host column is the hostname the store holds.
    """
    rows = report_rows(conn)
    if not rows:
        return ["no completed runs yet"]
    table = [REPORT_HEADERS]
    for ticket, actual, estimate, ratio, rounds, outcome, host in rows:
        table.append((
            ticket,
            f"{actual:.1f}",
            f"{estimate:.0f}" if estimate is not None else "n/a",
            f"{ratio:.2f}" if ratio is not None else "n/a",
            str(rounds),
            outcome,
            host_label(target, host),
        ))
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [
        REPORT_GAP.join(
            cell.ljust(width) if i in (0, 5, 6) else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths))).rstrip()
        for row in table
    ]
    return lines + [report_summary(rows)]


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
    """A `host` column as printed: the hostname, or `?` for a row without one.

    Rows older than the column are not backfilled, and a blank cell at the
    end of a line is invisible; `?` says "unknown" where unknown is the truth.
    """
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

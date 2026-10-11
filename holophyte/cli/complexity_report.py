import json
import statistics

import store.read
from holophyte.cli.shadow_report import shadow_side
from store.working import effective_work

COMPLEXITY_HEADERS = ("ticket", "verdict", "conf", "profile", "source", "first",
                      "outcome", "rounds", "min", "shadow")
COMPLEXITY_GAP = "  "
VERDICT_ORDER = ("small", "medium", "large", "none")
VERIFY_CELLS = {True: "verified", False: "failed", None: "n/a"}


def newest_payload(conn, run_id, kind):
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
        " ORDER BY seq DESC LIMIT 1", (run_id, kind)).fetchone()
    return None if row is None else json.loads(row[0])


def classified_runs(conn):
    runs = []
    for run in store.read.ended_runs(conn):
        verdict = newest_payload(conn, run.id, "complexity")
        if verdict is None:
            continue
        profile = newest_payload(conn, run.id, "implementer_profile") or {}
        shadow = newest_payload(conn, run.id, "shadow_result")
        (attempt,) = conn.execute("SELECT attempt FROM runs WHERE id = ?",
                                  (run.id,)).fetchone()
        working = effective_work(run, run.endedAt)
        runs.append({
            "ticket": run.linearIdentifier,
            "verdict": verdict.get("verdict") or "none",
            "confidence": verdict.get("confidence"),
            "profile": profile.get("profile") or "default",
            "source": profile.get("source") or "-",
            "attempt": attempt, "outcome": run.outcome or "ended",
            "rounds": run.reviewRoundCount,
            "minutes": None if working is None else working / 60000,
            "shadow": None if shadow is None else shadow_side(shadow)})
    return runs


def first_cell(run):
    if run["attempt"] != 1:
        return "-"
    return "pass" if run["outcome"] == "merged" else "fail"


def shadow_cell(shadow):
    if shadow is None:
        return ""
    return f"{VERIFY_CELLS[shadow['verified']]} {shadow['verdict'] or 'n/a'}"


def row_cells(run):
    confidence = run["confidence"]
    return (run["ticket"], run["verdict"],
            "-" if confidence is None else f"{confidence:.2f}",
            run["profile"], run["source"], first_cell(run), run["outcome"],
            str(run["rounds"]),
            "n/a" if run["minutes"] is None else f"{run['minutes']:.1f}",
            shadow_cell(run["shadow"]))


def summary_line(verdict, runs):
    first = [run for run in runs if run["attempt"] == 1]
    passed = sum(run["outcome"] == "merged" for run in first)
    minutes = [run["minutes"] for run in runs if run["minutes"] is not None]
    median_min = (f"{statistics.median(minutes):.1f}" if minutes else "n/a")
    line = (f"complexity {verdict}: {len(runs)} runs · first-attempt pass"
            f" {passed}/{len(first)} · median rounds"
            f" {statistics.median(run['rounds'] for run in runs):g}"
            f" · median min {median_min}")
    shadows = [run["shadow"] for run in runs if run["shadow"] is not None]
    if shadows:
        verified = sum(shadow["verified"] is True for shadow in shadows)
        approved = sum(shadow["verdict"] == "APPROVE" for shadow in shadows)
        line += (f" · shadowed {len(shadows)} (verified {verified},"
                 f" round-1 approvals {approved})")
    return line


def complexity_lines(conn):
    runs = classified_runs(conn)
    if not runs:
        return []
    table = [COMPLEXITY_HEADERS, *(row_cells(run) for run in runs)]
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [COMPLEXITY_GAP.join(
        cell.rjust(width) if i in (2, 7, 8) else cell.ljust(width)
        for i, (cell, width) in enumerate(zip(row, widths))).rstrip()
        for row in table]
    verdicts = [*VERDICT_ORDER, *sorted({run["verdict"] for run in runs}
                                        - set(VERDICT_ORDER))]
    summaries = [summary_line(verdict, [run for run in runs
                                        if run["verdict"] == verdict])
                 for verdict in verdicts
                 if any(run["verdict"] == verdict for run in runs)]
    return ["complexity classifier:", *lines, *summaries]

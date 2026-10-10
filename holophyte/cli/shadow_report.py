import json
import statistics
from collections import Counter

from store import SEVERITIES

SHADOW_HEADERS = ("ticket", "side", "route", "min", "in", "out", "cost", "verify",
                  "round 1", "findings", "rounds", "all turns", "outcome")
SHADOW_GAP = "  "
ROUND_VERDICTS = {"pass": "APPROVE", "changes_requested": "REQUEST_CHANGES"}
USAGE_KEYS = ("input_tokens", "output_tokens", "cost_usd")


def usage_total(usages, key):
    values = [usage.get(key) if isinstance(usage, dict) else None
              for usage in usages]
    return None if not values or None in values else sum(values)


def first_implementation(turns):
    first = []
    for turn in turns:
        if turn.get("role") == "review":
            break
        if turn.get("role") == "implement":
            first.append(turn)
    usages = [turn.get("usage") for turn in first]
    return {"route": first[0].get("label") if first else None,
            "minutes": sum(turn.get("seconds") or 0 for turn in first) / 60,
            **{key: usage_total(usages, key) for key in USAGE_KEYS}}


def primary_round(conn, run_id):
    row = conn.execute(
        "SELECT verdict, findings, verificationResults FROM reviewRounds"
        " WHERE runId = ? AND round = 1", (run_id,)).fetchone()
    if row is None:
        return {"verified": None, "verdict": None, "findings": None}
    verdict, findings, results = row
    results = json.loads(results)
    return {"verified": all(result.get("exitCode") == 0 for result in results)
            if results else None,
            "verdict": ROUND_VERDICTS.get(verdict, verdict),
            "findings": Counter(
                finding.get("severity") for finding in json.loads(findings)
                if finding.get("evidence_only") is not True)}


def primary_side(conn, run_id, rounds, outcome):
    turns = [json.loads(payload) for (payload,) in conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'agent_turn'"
        " ORDER BY seq", (run_id,))]
    return {**first_implementation(turns), **primary_round(conn, run_id),
            "rounds": rounds, "outcome": outcome or "in flight",
            "all_turns": usage_total(
                [turn.get("usage") for turn in turns
                 if turn.get("role") in ("implement", "trim")], "cost_usd")}


def shadow_side(result):
    usage = result.get("usage")
    verify = result.get("verify")
    review = result.get("review") or {}
    return {"route": result.get("route"),
            "minutes": (result.get("seconds") or 0) / 60,
            **{key: usage_total([usage], key) for key in USAGE_KEYS},
            "verified": verify.get("ok") if isinstance(verify, dict) else None,
            "verdict": review.get("verdict"), "findings": review.get("findings")}


def shadowed_runs(conn):
    latest = {}
    for run_id, payload in conn.execute(
            "SELECT runId, payload FROM runEvents WHERE kind = 'shadow_result'"
            " ORDER BY runId, seq"):
        latest[run_id] = json.loads(payload)
    runs = []
    for run_id, result in latest.items():
        ticket, rounds, outcome = conn.execute(
            "SELECT t.linearIdentifier, r.reviewRoundCount, r.outcome"
            " FROM runs r JOIN tickets t ON t.id = r.ticketId WHERE r.id = ?",
            (run_id,)).fetchone()
        runs.append((ticket, primary_side(conn, run_id, rounds, outcome),
                     shadow_side(result)))
    return runs


def dollars(cost):
    return "n/a" if cost is None else f"${cost:.2f}"


def findings_cell(findings):
    if findings is None:
        return "n/a"
    if not findings:
        return "none"
    order = [*SEVERITIES, *sorted(set(findings) - set(SEVERITIES))]
    return ", ".join(f"{severity} {findings[severity]}"
                     for severity in order if findings.get(severity))


def side_cells(ticket, label, side):
    verify = {True: "verified", False: "failed", None: "n/a"}[side["verified"]]
    return [ticket, label, side["route"] or "n/a", f"{side['minutes']:.1f}",
            *("n/a" if side[key] is None else str(side[key])
              for key in ("input_tokens", "output_tokens")),
            dollars(side["cost_usd"]), verify, side["verdict"] or "n/a",
            findings_cell(side["findings"])]


def summary_line(runs):
    n = len(runs)
    sides = [[run[1] for run in runs], [run[2] for run in runs]]
    verified = [sum(side["verified"] is True for side in mine) for mine in sides]
    approved = [sum(side["verdict"] == "APPROVE" for side in mine)
                for mine in sides]
    medians = [statistics.median(side["minutes"] for side in mine)
               for mine in sides]
    costs = [usage_total(mine, "cost_usd") for mine in sides]
    return (f"shadows {n} · verified primary {verified[0]}/{n},"
            f" shadow {verified[1]}/{n} · round-1 approvals primary"
            f" {approved[0]}/{n}, shadow {approved[1]}/{n}"
            f" · median first-implementation min primary {medians[0]:.1f},"
            f" shadow {medians[1]:.1f} · first-implementation cost primary"
            f" {dollars(costs[0])}, shadow {dollars(costs[1])}")


def shadow_lines(conn):
    runs = shadowed_runs(conn)
    if not runs:
        return []
    table = [SHADOW_HEADERS]
    for ticket, primary, shadow in runs:
        table.append((*side_cells(ticket, "primary", primary),
                      str(primary["rounds"]), dollars(primary["all_turns"]),
                      primary["outcome"]))
        table.append((*side_cells(ticket, "shadow", shadow), "", "", ""))
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [SHADOW_GAP.join(
        cell.rjust(width) if 3 <= i <= 6 or i in (10, 11) else cell.ljust(width)
        for i, (cell, width) in enumerate(zip(row, widths))).rstrip()
        for row in table]
    return ["shadow implementer:", *lines, summary_line(runs)]

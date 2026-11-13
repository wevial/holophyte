from __future__ import annotations

import dataclasses
import re
from statistics import median
from time import time
from typing import TypedDict

import store.read
from holophyte.cli.report import revert_counts
from holophyte.serve.day_log import THREAD_PARK
from holophyte.serve.serve_runs import (
    INTEGER,
    SQLITE_MAX_DIGITS,
    SQLITE_MAX_INT,
    commit_url,
    json_host,
    no_store,
    origin_web_url,
    parse_filter,
    parse_limit,
    parse_since,
    parse_until,
    served_round_caps,
)
from store.operate import ENDED_PHASES, PARKED_PHASES
from store.run_reads import (
    FAILURE_KINDS,
    HISTORY_ORDERS,
    HISTORY_SORTS,
    STATE_GROUPS,
    RunQuery,
)
from store.working import effective_work

HISTORY_LIMIT = 200
HISTORY_CAP = 5000
FLOW_WEEKS = 6
FLOW_WEEKS_CAP = 26
WEEK_MS = 7 * 24 * 3600 * 1000
HOUR_MS = 3600 * 1000
WORKING_PHASES = frozenset({"working", "verifying", "reviewing", "addressing"})
OPERATOR_PHASE = "blocked_on_operator"
PHASE_CHANGE = re.compile(r"^(\w+) -> (\w+)(?:: (.*))?$", re.DOTALL)
BEHIND_MAIN = "behind main"
WAIT_REASONS = (
    ("thread", re.compile(re.escape(THREAD_PARK) + r"|thread\(s\)")),
    ("behind_main", re.compile(BEHIND_MAIN)),
    ("main_red", re.compile(r"main is red")),
    ("rounds_used", re.compile(r"rounds are used up|review rounds"
                               r"|passes made|refreshes made")),
    ("pending_checks", re.compile(r"pending checks|checks (are )?pending"
                                  r"|never reported|did not report"
                                  r"|quiet wait")),
    ("checks_failing", re.compile(r"checks (are )?(failure|failed|failing)"
                                  r"|failed on (the )?merge group"
                                  r"|check fix round")),
    ("approval", re.compile(r"to say merge")),
)
WAIT_GROUPS = (*(name for name, _ in WAIT_REASONS), "other")


class FlowHours(TypedDict):
    working: float
    waiting: float
    operator: float


class FlowWeek(TypedDict):
    start_ms: int
    merged: int
    rounds_median: float | None
    interventions: int
    hours: FlowHours


class FlowWaits(TypedDict):
    pending_checks: float
    checks_failing: float
    thread: float
    rounds_used: float
    behind_main: float
    main_red: float
    approval: float
    other: float


class FlowReverted(TypedDict):
    count: int
    merged: int


class Flow(TypedDict):
    now: int
    weeks: list[FlowWeek]
    phases: dict[str, float]
    waits: FlowWaits
    reverted: FlowReverted | None


def parse_offset(query):
    text = parse_filter(query, "offset")
    if text is None:
        return 0
    digits = text.lstrip("-").lstrip("0")
    if not INTEGER.fullmatch(text) or (text.startswith("-") and digits):
        raise ValueError(f"offset must be a non-negative integer, got {text!r}")
    if len(digits) > SQLITE_MAX_DIGITS:
        return SQLITE_MAX_INT
    return min(int(digits or "0"), SQLITE_MAX_INT)


def parse_list(query, name, allowed=None):
    text = parse_filter(query, name)
    if text is None or (allowed is None and not text):
        return ()
    values = tuple(text.split(","))
    for value in values:
        if allowed is not None and value not in allowed:
            raise ValueError(f"{name} must be a comma list of"
                             f" {', '.join(allowed)}, got {value!r}")
    return values


def parse_query_text(query):
    text = parse_filter(query, "q")
    return text if text and text.strip() else None


def parse_history_query(query):
    return RunQuery(
        offset=parse_offset(query),
        sort=parse_filter(query, "sort", HISTORY_SORTS) or "started",
        order=parse_filter(query, "order", HISTORY_ORDERS) or "desc",
        q=parse_query_text(query),
        states=parse_list(query, "state", STATE_GROUPS),
        failures=parse_list(query, "failure", FAILURE_KINDS),
        implementers=parse_list(query, "implementer"))


def history(project, query, now=None):
    try:
        since = parse_since(query)
        until = parse_until(query)
        if until is not None and until <= since:
            raise ValueError("until must be after since")
        limit = parse_limit(query, default=HISTORY_LIMIT, cap=HISTORY_CAP)
        wanted = parse_history_query(query)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    now = int(time() * 1000) if now is None else now
    wanted = dataclasses.replace(wanted, now=now)
    conn = store.read.open_readonly(project.store_path)
    try:
        runs, total = store.read.recent_runs(conn, since, limit, until,
                                             query=wanted)
        counts = store.read.run_counts(conn, since, until, wanted)
        caps = served_round_caps(conn, project, [run.id for run in runs])
    finally:
        conn.close()
    origin = (origin_web_url(project) if any(run.mergeSha for run in runs)
              else None)
    return 200, {"rows": [history_row(project, run, now, caps[run.id], origin)
                          for run in runs],
                 "since": since, "until": until, "limit": limit,
                 "offset": wanted.offset, "sort": wanted.sort,
                 "order": wanted.order,
                 "truncated": total > wanted.offset + limit, "total": total,
                 "counts": counts,
                 "implementers": list(counts["implementer"]), "now": now}


def history_row(project, run, now, max_rounds, origin):
    live = run.endedAt is None
    return {"id": run.id, "ticket": run.linearIdentifier,
            "ticket_url": run.ticketUrl, "title": run.title,
            "attempt": run.attempt, "phase": run.phase,
            "outcome": run.outcome,
            "outcome_reason": (run.outcomeReason[:400]
                               if run.outcomeReason is not None else None),
            "failure_kind": run.failureKind, "park_kind": run.parkKind,
            "started_ms": run.startedAt, "ended_ms": run.endedAt,
            "elapsed_ms": (now if live else run.endedAt) - run.startedAt,
            "estimate_ms": run.timeBoxMs,
            "working_ms": effective_work(run, now if live else run.endedAt),
            "heartbeat_age_ms": now - run.lastHeartbeat if live else None,
            "rounds": run.reviewRoundCount,
            "max_rounds": max_rounds,
            "findings": run.findingCount, "merge_sha": run.mergeSha,
            "commit_url": commit_url(project, run.mergeSha, origin),
            "additions": run.prSeenAdditions,
            "deletions": run.prSeenDeletions,
            "resumed_as": run.resumedAs,
            "implementer": run.implementer,
            "pr_url": run.prUrl, "host": json_host(project, run.host)}


def parse_weeks(query):
    text = parse_filter(query, "weeks")
    if text is None:
        return FLOW_WEEKS
    if not INTEGER.fullmatch(text) or not 1 <= int(text) <= FLOW_WEEKS_CAP:
        raise ValueError(f"weeks must be an integer from 1 to"
                         f" {FLOW_WEEKS_CAP}, got {text!r}")
    return int(text)


def flow(project, query, now=None):
    try:
        weeks = parse_weeks(query)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    now = int(time() * 1000) if now is None else now
    since = now - weeks * WEEK_MS
    conn = store.read.open_readonly(project.store_path)
    try:
        merged = conn.execute(
            "SELECT endedAt, reviewRoundCount FROM runs WHERE outcome = 'merged'"
            " AND endedAt BETWEEN ? AND ?", (since, now)).fetchall()
        interventions = conn.execute(
            "SELECT at FROM interventions WHERE at BETWEEN ? AND ?",
            (since, now)).fetchall()
        events = conn.execute(
            "SELECT r.id, r.startedAt, r.endedAt, e.at, e.summary"
            " FROM runs r LEFT JOIN runEvents e ON e.runId = r.id"
            " AND e.kind = 'phase_change' WHERE r.startedAt BETWEEN ? AND ?"
            " ORDER BY r.id, e.seq", (since, now)).fetchall()
        reverts = revert_counts(conn, project.path, now)
    finally:
        conn.close()
    keys, count = (reverts or {}).get("30d", (None, None))
    return 200, {"now": now,
                 **flow_weeks(since, weeks, merged, interventions, events, now),
                 "reverted": (None if keys is None
                              else {"count": len(keys), "merged": count})}


def flow_weeks(since, weeks, merged, interventions, events, now):
    def week_of(at):
        return rows[min((at - since) // WEEK_MS, weeks - 1)]

    rows = [{"start_ms": since + index * WEEK_MS, "rounds": [],
             "interventions": 0,
             "hours": {"working": 0, "waiting": 0, "operator": 0}}
            for index in range(weeks)]
    for ended_at, rounds in merged:
        week_of(ended_at)["rounds"].append(rounds)
    for (at,) in interventions:
        week_of(at)["interventions"] += 1
    phases, waits = {}, dict.fromkeys(WAIT_GROUPS, 0)
    for started_at, phase, ms, wait in run_stays(events, now):
        if phase in ENDED_PHASES:
            continue
        week_of(started_at)["hours"][phase_group(phase)] += ms
        phases[phase] = phases.get(phase, 0) + ms
        if wait is not None:
            waits[wait] += ms
    return {"weeks": [{"start_ms": row["start_ms"],
                       "merged": len(row["rounds"]),
                       "rounds_median": (median(row["rounds"])
                                         if row["rounds"] else None),
                       "interventions": row["interventions"],
                       "hours": {group: in_hours(ms)
                                 for group, ms in row["hours"].items()}}
                      for row in rows],
            "phases": {phase: in_hours(ms) for phase, ms in phases.items()},
            "waits": {reason: in_hours(ms) for reason, ms in waits.items()}}


def run_stays(events, now):
    runs = {}
    for run_id, started_at, ended_at, at, summary in events:
        changes = runs.setdefault(run_id, (started_at, ended_at, []))[2]
        if at is not None:
            changes.append((at, summary))
    for started_at, ended_at, changes in runs.values():
        yield from stays(started_at, ended_at, changes, now)


def stays(started_at, ended_at, changes, now):
    phase, note, entered = None, None, started_at
    if not changes:
        phase = "claimed"
    for at, summary in changes:
        match = PHASE_CHANGE.match(summary)
        if match is None:
            continue
        phase = phase or match.group(1)
        yield started_at, phase, at - entered, wait_reason(phase, note)
        phase, note, entered = match.group(2), match.group(3), at
    if phase is not None:
        end = now if ended_at is None else min(ended_at, now)
        yield (started_at, phase, max(end - entered, 0),
               wait_reason(phase, note))


def wait_reason(phase, note):
    if phase in PARKED_PHASES:
        return next((reason for reason, pattern in WAIT_REASONS
                     if pattern.search(note or "")), "other")
    if phase == "merge_gate" and BEHIND_MAIN in (note or ""):
        return "behind_main"
    return None


def phase_group(phase):
    if phase in WORKING_PHASES:
        return "working"
    return "operator" if phase == OPERATOR_PHASE else "waiting"


def in_hours(ms):
    return round(ms / HOUR_MS, 3)

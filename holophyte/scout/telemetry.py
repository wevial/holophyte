import json
from collections import defaultdict
from typing import NamedTuple

from holophyte.agents.harness import SCOUT_LEAD
from store.enums import ScoutTaskPhase
from store.scouts import ENDED

MILLION = 1_000_000
MINUTE_MS = 60_000
IN_ENDED = f"s.phase IN ({', '.join('?' * len(ENDED))})"


class Turn(NamedTuple):
    route: str
    scout: int
    ms: float
    tokens: int | None
    cost: float | None


class Task(NamedTuple):
    route: str
    scout: int
    id: int
    phase: str
    ms: int
    tokens: int | None
    cost: float | None


def turn_cost(price, tokens, reported):
    if reported is not None:
        return reported
    if price is None or tokens is None:
        return None
    return round(tokens * price / MILLION, 6)


def usage_tokens(usage):
    if usage is None:
        return None
    if "total_tokens" in usage:
        return usage["total_tokens"]
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens")]
    counts = [count for count in counts if isinstance(count, int)]
    return sum(counts) if counts else None


def plural(count, word):
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def cost_text(turns):
    costs = [turn.cost for turn in turns if turn.cost is not None]
    if not costs:
        return "cost not reported"
    unpriced = sum(1 for turn in turns
                   if turn.tokens is not None and turn.cost is None)
    return f"${sum(costs):.4f}" + (f" ({unpriced} unpriced)" if unpriced
                                   else "")


def minutes(turns):
    return f"{sum(turn.ms for turn in turns) / MINUTE_MS:.1f} min"


def tokens(turns):
    return f"{sum(turn.tokens or 0 for turn in turns)} tokens"


def lead_turn(scout_id, lead_route, payload):
    payload = json.loads(payload or "{}")
    if payload.get("role") != SCOUT_LEAD:
        return None
    usage = payload.get("usage")
    return Turn(lead_route or payload.get("route"), scout_id,
                (payload.get("seconds") or 0) * 1000, usage_tokens(usage),
                (usage or {}).get("cost_usd"))


def lead_turns(conn):
    rows = conn.execute(
        "SELECT e.scoutId, s.leadRoute, e.payload FROM runEvents e"
        " JOIN scouts s ON s.id = e.scoutId"
        f" WHERE e.kind = 'agent_turn' AND {IN_ENDED}",
        ENDED).fetchall()
    return [turn for turn in (lead_turn(*row) for row in rows)
            if turn is not None]


def helper_tasks(conn):
    return [Task(*row) for row in conn.execute(
        "SELECT COALESCE(t.route, s.helperRoute), t.scoutId, t.id, t.phase,"
        " COALESCE(t.durationMs, 0), t.tokens, t.costUsd FROM scoutTasks t"
        f" JOIN scouts s ON s.id = t.scoutId WHERE {IN_ENDED} ORDER BY t.id",
        ENDED)]


def finding_tasks(report):
    findings = json.loads(report).get("findings")
    for finding in findings if isinstance(findings, list) else ():
        ids = finding.get("tasks") if isinstance(finding, dict) else None
        if isinstance(ids, list):
            yield from (task for task in ids if type(task) is int)


def cited_tasks(conn):
    return {(scout_id, task) for scout_id, report in conn.execute(
        "SELECT r.scoutId, r.report FROM scoutReports r"
        f" JOIN scouts s ON s.id = r.scoutId WHERE {IN_ENDED}", ENDED)
        for task in finding_tasks(report)}


def by_route(items):
    routes = defaultdict(list)
    for item in items:
        routes[item.route].append(item)
    return sorted(routes.items(), key=lambda route: str(route[0]))


def scouts_line(ended, turns):
    counts = " · ".join(f"{phase} {sum(row[0] == phase for row in ended)}"
                        for phase in ENDED)
    spent = sum(ms for _, ms in ended) / MINUTE_MS
    return (f"scouts: {len(ended)} ended ({counts}) · {spent:.1f} min · "
            f"{cost_text(turns)}")


def lead_line(route, turns):
    return (f"scout lead {route}: "
            f"{plural(len({turn.scout for turn in turns}), 'scout')} · "
            f"{plural(len(turns), 'turn')} · {minutes(turns)} · "
            f"{tokens(turns)} · {cost_text(turns)}")


def helper_line(route, tasks, cited):
    done = [task for task in tasks if task.phase == ScoutTaskPhase.DONE.value]
    used = sum((task.scout, task.id) in cited for task in done)
    return (f"scout helper {route}: {plural(len(tasks), 'task')} · "
            f"done {len(done)} · cited {used} · {minutes(tasks)} · "
            f"{tokens(tasks)} · {cost_text(tasks)}")


def scout_report_lines(conn):
    ended = conn.execute(
        "SELECT s.phase, COALESCE(s.endedAt - s.startedAt, 0) FROM scouts s"
        f" WHERE {IN_ENDED}", ENDED).fetchall()
    if not ended:
        return []
    turns, tasks, cited = lead_turns(conn), helper_tasks(conn), cited_tasks(conn)
    return ([scouts_line(ended, turns + tasks)]
            + [lead_line(route, mine) for route, mine in by_route(turns)]
            + [helper_line(route, mine, cited)
               for route, mine in by_route(tasks)])

"""The daemon's scout reads: the list, one scout whole, and the launch pickers."""
import json
import re
import time
from typing import get_args

import store.read
from holophyte.agents.harness import SCOUT_HELPER, SCOUT_LEAD
from holophyte.scout.report import GUARDRAIL, read_status
from holophyte.scout.scout_config import scout_config
from holophyte.scout.scout_contract import ActionKind
from holophyte.serve.serve_runs import (
    SQLITE_MAX_DIGITS,
    SQLITE_MAX_INT,
    json_host,
    no_store,
    parse_limit,
)
from holophyte.serve.views import route_probe_view
from store.scout_thread import messages
from store.scouts import ENDED, QUEUED, TASK_ENDED, scout

SCOUTS_PATH = "/scouts"
SCOUT_PATH = re.compile(r"^/scouts/([^/]+)$")
SCOUT_ROUTES_PATH = re.compile(r"^/scouts/(routes)$")
SCOUT_ID = re.compile(r"^0*([1-9][0-9]*)$")
LIMIT, LIMIT_CAP = 20, 200
ROW_COLUMNS = ("id", "ask", "kind", "phase", "reason", "leadRoute",
               "helperRoute", "swarm", "createdAt", "startedAt", "endedAt",
               "heartbeatAt", "subjectKind", "subjectRef")
ENDED_MARKS = ", ".join("?" * len(TASK_ENDED))
ROWS = (f"SELECT {', '.join(f's.{column}' for column in ROW_COLUMNS)},"
        " (SELECT COUNT(*) FROM scoutTasks t WHERE t.scoutId = s.id"
        f" AND t.phase IN ({ENDED_MARKS})),"
        " (SELECT COUNT(*) FROM scoutTasks t WHERE t.scoutId = s.id)"
        " FROM scouts s")
TASK_COLUMNS = ("id", "pass", "wave", "question", "paths", "mode", "route",
                "phase", "startedAt", "endedAt", "durationMs", "tokens",
                "costUsd", "excerpt")
TASK_KEYS = ("id", "pass", "wave", "question", "paths", "mode", "route",
             "phase", "started_ms", "ended_ms", "duration_ms", "tokens",
             "cost_usd", "excerpt")
ACTION_KEYS = ("title", "why", "kind", "body")
HYPOTHESIS_KEYS = ("hypothesis", "separates", "verdict")
PRIOR_KEYS = ("scout", "finding", "verdict", "why")
ACTION_KINDS = get_args(ActionKind)


def now_ms(now):
    return int(time.time() * 1000) if now is None else now


def scout_row(row, now):
    (scout_id, ask, kind, phase, reason, lead, helper, swarm, created,
     started, ended, beat, subject_kind, subject_ref, done, total) = row
    live = phase != QUEUED and phase not in ENDED and beat is not None
    return {"id": scout_id, "ask": ask, "kind": kind, "phase": phase,
            "reason": reason, "lead": lead, "helper": helper, "swarm": swarm,
            "tasks_done": done, "tasks_total": total, "created_ms": created,
            "started_ms": started, "ended_ms": ended,
            "elapsed_ms": (None if started is None
                           else (now if ended is None else ended) - started),
            "heartbeat_age_ms": now - beat if live else None,
            "subject": (None if subject_kind is None
                        else {"kind": subject_kind, "ref": subject_ref})}


def scouts(project, query, now=None):
    try:
        limit = parse_limit(query, default=LIMIT, cap=LIMIT_CAP)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    now = now_ms(now)
    conn = store.read.open_readonly(project.store_path)
    try:
        rows = conn.execute(f"{ROWS} ORDER BY s.id DESC LIMIT ?",
                            (*TASK_ENDED, limit + 1)).fetchall()
    finally:
        conn.close()
    return 200, {"scouts": [scout_row(row, now) for row in rows[:limit]],
                 "limit": limit, "truncated": len(rows) > limit}


def parse_scout_id(text):
    match = SCOUT_ID.match(text)
    if match is None:
        raise ValueError(f"scout id must be a positive integer, got {text!r}")
    digits = match.group(1)
    if len(digits) > SQLITE_MAX_DIGITS or int(digits) > SQLITE_MAX_INT:
        return None
    return int(digits)


def served_evidence(item):
    if isinstance(item, str):
        return {"text": item}
    if "file" in item:
        verified = item.get("verified") is True
        return {"file": item["file"], "line": item["line"],
                "verified": verified,
                "why": None if verified else item.get("why")}
    if "command" in item:
        return {"command": item["command"], "output": item["output"]}
    if "url" in item:
        return {"url": item["url"]}
    return {"text": item["text"]}


def served_evidence_list(evidence):
    items = [evidence] if isinstance(evidence, str) else evidence
    return [served_evidence(item) for item in items]


def guardrail_keys(action):
    if action["kind"] != GUARDRAIL:
        return {"layer": None, "covers": None}
    return {"layer": action["layer"], "covers": action["covers"]}


def served_report_action(action):
    kind = action["kind"]
    return {**{key: action[key] for key in ACTION_KEYS},
            "kind": kind if kind in ACTION_KINDS else "investigate",
            **guardrail_keys(action)}


def served_reproduction(reproduction):
    if reproduction is None:
        return None
    return {"level": reproduction["level"],
            "hypotheses": [{key: hypothesis[key] for key in HYPOTHESIS_KEYS}
                           for hypothesis in reproduction["hypotheses"]]}


def served_report(report):
    return {"summary": report["summary"],
            "findings": [{"title": finding["title"],
                          "status": read_status(finding["status"]),
                          "evidence": served_evidence_list(
                              finding["evidence"])}
                         for finding in report["findings"]],
            "actions": [served_report_action(action)
                        for action in report["actions"]],
            "reproduction": served_reproduction(report.get("reproduction")),
            "cut": report.get("cut"),
            "prior": (None if report.get("prior") is None
                      else [{key: judgement[key] for key in PRIOR_KEYS}
                            for judgement in report["prior"]])}


def served_action(item):
    return {**{key: item[key] for key in ACTION_KEYS},
            **guardrail_keys(item), "revises": item.get("revises")}


def served_message(message):
    return {"id": message.id, "author": message.author, "mode": message.mode,
            "text": message.text, "blocks": message.blocks,
            "answers": message.answers_id, "at": message.at,
            "consumed_by": message.consumed_by,
            "consumed_ms": message.consumed_at}


def scout_parts(conn, scout_id):
    tasks = conn.execute(
        f"SELECT {', '.join(TASK_COLUMNS)} FROM scoutTasks WHERE scoutId = ?"
        " ORDER BY pass, wave, id", (scout_id,)).fetchall()
    reports = conn.execute(
        "SELECT pass, at, report FROM scoutReports WHERE scoutId = ?"
        " ORDER BY pass", (scout_id,)).fetchall()
    actions = conn.execute(
        "SELECT position, pass, item, ticketKey, filedAt FROM scoutActions"
        " WHERE scoutId = ? ORDER BY position", (scout_id,)).fetchall()
    events = conn.execute(
        "SELECT id, at, kind, summary FROM runEvents WHERE scoutId = ?"
        " ORDER BY seq", (scout_id,)).fetchall()
    return {
        "tasks": [{**dict(zip(TASK_KEYS, task)), "paths": json.loads(task[4])}
                  for task in tasks],
        "reports": [{"pass": number, "at": at,
                     "report": served_report(json.loads(report))}
                    for number, at, report in reports],
        "actions": [{"position": position, "pass": number,
                     **served_action(json.loads(item)),
                     "ticket": ticket, "filed_ms": filed}
                    for position, number, item, ticket, filed in actions],
        "events": [{"id": event_id, "at": at, "kind": kind,
                    "summary": summary}
                   for event_id, at, kind, summary in events],
        "thread": [served_message(message)
                   for message in messages(conn, scout_id)],
    }


def scout_detail(project, text, now=None):
    try:
        scout_id = parse_scout_id(text)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not project.store_path.exists():
        return 503, no_store(project)
    if scout_id is None:
        return 404, {"error": "no such scout", "scout": text}
    now = now_ms(now)
    conn = store.read.open_readonly(project.store_path)
    try:
        conn.execute("BEGIN")
        row = scout(conn, scout_id)
        if row is None:
            return 404, {"error": "no such scout", "scout": scout_id}
        (listed,) = conn.execute(f"{ROWS} WHERE s.id = ?",
                                 (*TASK_ENDED, scout_id)).fetchall()
        parts = scout_parts(conn, scout_id)
    finally:
        conn.close()
    return 200, {"scout": {**scout_row(listed, now), "base_sha": row.base_sha,
                           "host": json_host(project, row.host),
                           "budget": {"minutes": row.budget_min,
                                      "helper_turns": row.helper_turns,
                                      "waves": row.waves},
                           "totals": row.totals},
                 **parts}


def offered(project, routes, role, default, now):
    probes = route_probe_view(project, [f"{role}:{name}" for name in routes], now)
    listed = []
    for name, seat in routes.items():
        probe = probes[f"{role}:{name}"]["primary"]
        if probe is not None and not probe["ok"]:
            continue
        listed.append({"name": name, "harness": seat.adapter.name,
                       "model": seat.options["model"],
                       "default": name == default, "probe": probe})
    return listed


def scout_routes(project, _segment=None, now=None):
    now = now_ms(now)
    config = scout_config(project)
    return 200, {
        "leads": offered(project, config.leads, SCOUT_LEAD, config.lead, now),
        "helpers": offered(project, config.helpers, SCOUT_HELPER,
                           config.helper, now),
        "defaults": {"lead": config.lead, "helper": config.helper,
                     "swarm": config.swarm, "minutes": config.budget_min,
                     "helper_turns": config.helper_turns,
                     "waves": config.waves}}

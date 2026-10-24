"""A scout run's record: its phase, reports, helper tasks, actions and events."""
import json
import socket
import time
from typing import NamedTuple

from . import EVENT_LEVELS, _redact_event
from .enums import ScoutPhase, ScoutTaskPhase
from .schema import _transaction

QUEUED, PLANNING = ScoutPhase.QUEUED.value, ScoutPhase.PLANNING.value
SWARMING, SYNTHESIZING = ScoutPhase.SWARMING.value, ScoutPhase.SYNTHESIZING.value
ENDED = (ScoutPhase.DONE.value, ScoutPhase.FAILED.value,
         ScoutPhase.STOPPED.value)
EDGES = {
    QUEUED: (PLANNING,),
    PLANNING: (SWARMING, SYNTHESIZING, QUEUED),
    SWARMING: (SYNTHESIZING,),
    SYNTHESIZING: (SWARMING,),
}
TASK_ENDED = (ScoutTaskPhase.DONE.value, ScoutTaskPhase.FAILED.value,
              ScoutTaskPhase.STOPPED.value)
COLUMNS = ("id", "projectId", "ask", "kind", "links", "leadRoute",
           "leadSession", "helperRoute", "swarm", "helperTurns", "waves",
           "budgetMin", "phase", "reason", "baseSha", "host", "pid",
           "createdAt", "startedAt", "heartbeatAt", "endedAt", "totals")
SELECT = f"SELECT {', '.join(COLUMNS)} FROM scouts"


class ScoutRefused(Exception):
    def __init__(self, scout_id, phase, detail):
        super().__init__(f"scout {scout_id} is {phase}: {detail}")
        self.scout_id, self.phase = scout_id, phase


class Scout(NamedTuple):
    id: int
    project_id: int
    ask: str
    kind: str
    links: list
    lead_route: str | None
    lead_session: str | None
    helper_route: str | None
    swarm: int | None
    helper_turns: int | None
    waves: int | None
    budget_min: int
    phase: str
    reason: str | None
    base_sha: str | None
    host: str | None
    pid: int | None
    created_at: int
    started_at: int | None
    heartbeat_at: int | None
    ended_at: int | None
    totals: dict | None


def _now(now):
    return int(time.time() * 1000) if now is None else now


def _phase(conn, scout_id):
    row = conn.execute("SELECT phase FROM scouts WHERE id = ?",
                       (scout_id,)).fetchone()
    if row is None:
        raise ValueError(f"no scout {scout_id}")
    return row[0]


def _live(conn, scout_id):
    phase = _phase(conn, scout_id)
    if phase in ENDED:
        raise ScoutRefused(scout_id, phase, "the scout has already ended")
    return phase


def create_scout(conn, project_id, ask, kind, budget_min, *, links=(),
                 lead_route=None, helper_route=None, swarm=None,
                 helper_turns=None, waves=None, now=None):
    if not isinstance(ask, str) or not ask.strip():
        raise ValueError("ask must be non-blank text")
    with _transaction(conn):
        return conn.execute(
            "INSERT INTO scouts (projectId, ask, kind, links, leadRoute,"
            " helperRoute, swarm, helperTurns, waves, budgetMin, phase,"
            " createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, ask.strip(), kind, json.dumps(list(links)),
             lead_route, helper_route, swarm, helper_turns, waves, budget_min,
             QUEUED, _now(now))).lastrowid


def _unclaimed(conn, scout_id, phase):
    (owner,) = conn.execute("SELECT pid FROM scouts WHERE id = ?",
                            (scout_id,)).fetchone()
    if owner is not None:
        raise ScoutRefused(scout_id, phase,
                           f"its process (pid {owner}) has claimed it")


def move_scout(conn, scout_id, phase, *, base_sha=None, host=None, now=None):
    with _transaction(conn):
        current = _live(conn, scout_id)
        if phase not in EDGES.get(current, ()):
            raise ScoutRefused(scout_id, current,
                               f"no move from {current} to {phase}")
        if phase == PLANNING and not base_sha:
            raise ValueError("a scout moves to planning with a base sha")
        if phase == QUEUED:
            _unclaimed(conn, scout_id, current)
            conn.execute(
                "UPDATE scouts SET phase = ?, baseSha = NULL, startedAt = NULL,"
                " heartbeatAt = NULL, host = NULL WHERE id = ?",
                (phase, scout_id))
        elif phase == PLANNING:
            now = _now(now)
            conn.execute(
                "UPDATE scouts SET phase = ?, baseSha = ?, startedAt = ?,"
                " heartbeatAt = ?, host = ? WHERE id = ?",
                (phase, base_sha, now, now, host or socket.gethostname(),
                 scout_id))
        else:
            conn.execute("UPDATE scouts SET phase = ? WHERE id = ?",
                         (phase, scout_id))


def beat_scout(conn, scout_id, pid=None, now=None):
    with _transaction(conn):
        _live(conn, scout_id)
        conn.execute("UPDATE scouts SET heartbeatAt = ?,"
                     " pid = COALESCE(pid, ?) WHERE id = ?",
                     (_now(now), pid, scout_id))


def claim_scout(conn, scout_id, pid, host=None, now=None):
    with _transaction(conn):
        phase = _live(conn, scout_id)
        (owner,) = conn.execute("SELECT pid FROM scouts WHERE id = ?",
                                (scout_id,)).fetchone()
        if phase != PLANNING or owner is not None:
            raise ScoutRefused(scout_id, phase,
                               f"not a planning scout without a pid (pid {owner})")
        conn.execute("UPDATE scouts SET pid = ?, host = ?, heartbeatAt = ?"
                     " WHERE id = ?",
                     (pid, host or socket.gethostname(), _now(now), scout_id))


def end_scout(conn, scout_id, phase, reason, totals=None, now=None):
    if phase not in ENDED:
        raise ValueError(f"a scout ends {', '.join(ENDED)}, not {phase!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason must be non-blank text")
    with _transaction(conn):
        _live(conn, scout_id)
        conn.execute(
            "UPDATE scouts SET phase = ?, reason = ?, totals = ?, endedAt = ?"
            " WHERE id = ?",
            (phase, reason.strip(),
             None if totals is None else json.dumps(totals), _now(now),
             scout_id))


def set_scout_session(conn, scout_id, session_id):
    with _transaction(conn):
        _live(conn, scout_id)
        conn.execute("UPDATE scouts SET leadSession = ? WHERE id = ?",
                     (session_id, scout_id))


def record_scout_report(conn, scout_id, pass_number, report, now=None):
    with _transaction(conn):
        phase = _live(conn, scout_id)
        if conn.execute("SELECT 1 FROM scoutReports WHERE scoutId = ?"
                        " AND pass = ?", (scout_id, pass_number)).fetchone():
            raise ScoutRefused(scout_id, phase,
                               f"pass {pass_number} already has a report")
        return conn.execute(
            "INSERT INTO scoutReports (scoutId, pass, report, at)"
            " VALUES (?, ?, ?, ?)",
            (scout_id, pass_number, json.dumps(report), _now(now))).lastrowid


def record_scout_tasks(conn, scout_id, pass_number, wave, tasks):
    with _transaction(conn):
        _live(conn, scout_id)
        return [conn.execute(
            "INSERT INTO scoutTasks (scoutId, pass, wave, question, paths,"
            " mode, route, phase) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scout_id, pass_number, wave, task["question"],
             json.dumps(list(task.get("paths") or ())), task["mode"],
             task.get("route"), ScoutTaskPhase.QUEUED.value)).lastrowid
            for task in tasks]


def _task(conn, task_id):
    row = conn.execute(
        "SELECT t.scoutId, s.phase, t.phase, t.startedAt FROM scoutTasks t"
        " JOIN scouts s ON s.id = t.scoutId WHERE t.id = ?",
        (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"no scout task {task_id}")
    return row


def start_scout_task(conn, task_id, now=None):
    with _transaction(conn):
        scout_id, scout_phase, phase, _ = _task(conn, task_id)
        _live(conn, scout_id)
        if phase != ScoutTaskPhase.QUEUED.value:
            raise ScoutRefused(scout_id, scout_phase,
                               f"task {task_id} is {phase}, not queued")
        conn.execute("UPDATE scoutTasks SET phase = ?, startedAt = ?"
                     " WHERE id = ?",
                     (ScoutTaskPhase.RUNNING.value, _now(now), task_id))


def end_scout_task(conn, task_id, phase, *, tokens=None, cost_usd=None,
                   excerpt=None, now=None):
    if phase not in TASK_ENDED:
        raise ValueError(f"a task ends {', '.join(TASK_ENDED)}, not {phase!r}")
    now = _now(now)
    with _transaction(conn):
        scout_id, scout_phase, current, started_at = _task(conn, task_id)
        _live(conn, scout_id)
        if current in TASK_ENDED:
            raise ScoutRefused(scout_id, scout_phase,
                               f"task {task_id} has already ended {current}")
        if current != ScoutTaskPhase.RUNNING.value:
            raise ScoutRefused(scout_id, scout_phase,
                               f"task {task_id} is {current}, not running")
        conn.execute(
            "UPDATE scoutTasks SET phase = ?, endedAt = ?, durationMs = ?,"
            " tokens = ?, costUsd = ?, excerpt = ? WHERE id = ?",
            (phase, now, now - started_at, tokens, cost_usd, excerpt, task_id))


def record_scout_actions(conn, scout_id, pass_number, items):
    with _transaction(conn):
        _live(conn, scout_id)
        (last,) = conn.execute(
            "SELECT COALESCE(MAX(position), 0) FROM scoutActions"
            " WHERE scoutId = ?", (scout_id,)).fetchone()
        positions = []
        for position, item in enumerate(items, last + 1):
            conn.execute("INSERT INTO scoutActions (scoutId, pass, position,"
                         " item) VALUES (?, ?, ?, ?)",
                         (scout_id, pass_number, position, json.dumps(item)))
            positions.append(position)
        return positions


def file_scout_action(conn, scout_id, position, ticket_key, now=None):
    if not isinstance(ticket_key, str) or not ticket_key.strip():
        raise ValueError("ticket key must be non-blank text")
    with _transaction(conn):
        phase = _phase(conn, scout_id)
        if phase != ScoutPhase.DONE.value:
            raise ScoutRefused(scout_id, phase, "only a done scout's items file")
        row = conn.execute("SELECT ticketKey FROM scoutActions"
                           " WHERE scoutId = ? AND position = ?",
                           (scout_id, position)).fetchone()
        if row is None:
            raise ValueError(f"scout {scout_id} has no item {position}")
        if row[0] is not None:
            raise ScoutRefused(scout_id, phase,
                               f"item {position} is already filed as {row[0]}")
        conn.execute("UPDATE scoutActions SET ticketKey = ?, filedAt = ?"
                     " WHERE scoutId = ? AND position = ?",
                     (ticket_key, _now(now), scout_id, position))


def record_scout_event(conn, scout_id, kind, summary, level="narrative",
                       payload=None, now=None):
    if level not in EVENT_LEVELS:
        raise ValueError(f"unknown event level {level!r}")
    if payload is not None and level != "detail":
        raise ValueError("payload is a detail-level field")
    summary, payload = _redact_event(summary, payload)
    with _transaction(conn):
        _live(conn, scout_id)
        (project_id,) = conn.execute("SELECT projectId FROM scouts"
                                     " WHERE id = ?", (scout_id,)).fetchone()
        (seq,) = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM runEvents"
            " WHERE scoutId = ?", (scout_id,)).fetchone()
        conn.execute(
            "INSERT INTO runEvents (runId, projectId, scoutId, seq, level,"
            " kind, summary, payload, at) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, scout_id, seq, level, kind, summary, payload, _now(now)))
        return seq


def _scout(row):
    values = dict(zip(COLUMNS, row))
    values["links"] = json.loads(values["links"])
    if values["totals"] is not None:
        values["totals"] = json.loads(values["totals"])
    return Scout(*values.values())


def scout(conn, scout_id):
    row = conn.execute(f"{SELECT} WHERE id = ?", (scout_id,)).fetchone()
    return None if row is None else _scout(row)


def scouts(conn, phases):
    phases = list(phases)
    rows = conn.execute(
        f"{SELECT} WHERE phase IN"
        f" ({', '.join('?' * len(phases))}) ORDER BY createdAt, id",
        phases).fetchall()
    return [_scout(row) for row in rows]

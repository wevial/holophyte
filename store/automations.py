"""Automation firings: one `automationFirings` row per firing key, reserved once."""
import json
from typing import NamedTuple

import store

from .enums import FiringMode, FiringState, PauseActor, check_clause

PROPOSED = FiringState.PROPOSED.value
LEASED = FiringState.LEASED.value
CONFIRMED = FiringState.CONFIRMED.value
UNKNOWN = FiringState.UNKNOWN.value
CANCELED = FiringState.CANCELED.value
SETTLED = frozenset((FiringState.REFUSED.value, CONFIRMED, UNKNOWN, CANCELED))
OPERATOR = PauseActor.OPERATOR.value

FIRINGS_DDL = f"""
CREATE TABLE IF NOT EXISTS automationFirings (
    id         INTEGER PRIMARY KEY,
    projectId  INTEGER NOT NULL REFERENCES projects (id),
    automation TEXT    NOT NULL,
    revision   INTEGER NOT NULL,
    subject    TEXT    NOT NULL,
    cause      TEXT    NOT NULL,
    firingKey  TEXT    NOT NULL UNIQUE,
    mode       TEXT    NOT NULL {check_clause('mode', FiringMode)},
    state      TEXT    NOT NULL {check_clause('state', FiringState)},
    reason     TEXT    NOT NULL,
    detail     TEXT    NOT NULL DEFAULT '{{}}'
        CHECK (json_type(detail) = 'object'),
    createdAt  INTEGER NOT NULL,
    settledAt  INTEGER,
    leaseUntil INTEGER,
    workerPid  INTEGER
);
"""

PAUSES_DDL = f"""
CREATE TABLE IF NOT EXISTS automationPauses (
    id         INTEGER PRIMARY KEY,
    automation TEXT    NOT NULL,
    note       TEXT    NOT NULL,
    actor      TEXT    NOT NULL {check_clause('actor', PauseActor)},
    createdAt  INTEGER NOT NULL,
    clearedAt  INTEGER,
    clearNote  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS automationPauses_open
    ON automationPauses (automation) WHERE clearedAt IS NULL;
"""

HOLDS_DDL = """
CREATE TABLE IF NOT EXISTS mergeHolds (
    id        INTEGER PRIMARY KEY,
    projectId INTEGER NOT NULL REFERENCES projects (id),
    sha       TEXT    NOT NULL,
    checks    TEXT    NOT NULL CHECK (json_type(checks) = 'array'),
    firingId  INTEGER NOT NULL REFERENCES automationFirings (id),
    heldAt    INTEGER NOT NULL,
    endedAt   INTEGER,
    endReason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS mergeHolds_open
    ON mergeHolds (projectId) WHERE endedAt IS NULL;
"""


class MergeHold(NamedTuple):
    id: int
    projectId: int
    sha: str
    checks: list
    firingId: int
    heldAt: int
    endedAt: int | None
    endReason: str | None


class HeldRun(NamedTuple):
    runId: int
    ticketId: int
    identifier: str
    lastRunId: int | None
    phase: str
    endedAt: int | None
    touched: bool
    reparked: bool


SUPERSEDED = "superseded by "


class FiringIntervention(NamedTuple):
    id: int
    runId: int | None
    identifier: str | None
    action: str
    source: str
    actor: str | None
    note: str | None
    at: int


class Pause(NamedTuple):
    id: int
    automation: str
    note: str
    actor: str
    createdAt: int
    clearedAt: int | None
    clearNote: str | None


class Firing(NamedTuple):
    id: int
    projectId: int
    automation: str
    revision: int
    subject: str
    cause: str
    firingKey: str
    mode: str
    state: str
    reason: str
    detail: dict
    createdAt: int
    settledAt: int | None
    leaseUntil: int | None
    workerPid: int | None


def _firing_key(automation, revision, project, subject, cause):
    return ":".join((automation, str(revision), str(project), subject, cause))


def reserve_firing(conn, project, automation, revision, subject, cause, *,
                   mode, reason, detail, now, lease_until=None):
    """The id of a new row, or of a proposal on the key a lease takes over."""
    key = _firing_key(automation, revision, project, subject, cause)
    state = PROPOSED if lease_until is None else LEASED
    with store.transaction(conn):
        row = conn.execute(
            "INSERT INTO automationFirings (projectId, automation, revision,"
            " subject, cause, firingKey, mode, state, reason, detail,"
            " createdAt, leaseUntil) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (firingKey) DO UPDATE SET mode = excluded.mode,"
            " state = excluded.state, reason = excluded.reason,"
            " detail = excluded.detail, leaseUntil = excluded.leaseUntil"
            " WHERE automationFirings.state = ? AND excluded.state = ?"
            " RETURNING id",
            (project, automation, revision, subject, cause, key, mode,
             state, reason, json.dumps(detail, sort_keys=True), now,
             lease_until, PROPOSED, LEASED)).fetchone()
        return None if row is None else row[0]


def _selected(conn):
    present = {row[1] for row in
               conn.execute("PRAGMA table_info(automationFirings)")}
    return ", ".join(field if field in present else f"NULL AS {field}"
                     for field in Firing._fields)


def _firing(row):
    return Firing(*row[:10], json.loads(row[10]), *row[11:])


def firings(conn, limit=-1):
    """None read from a store an older build last wrote, which lacks the table."""
    if not _has_table(conn, "automationFirings"):
        return []
    rows = conn.execute(
        f"SELECT {_selected(conn)} FROM automationFirings"
        " ORDER BY createdAt DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [_firing(row) for row in rows]


def keyed_firing(conn, project, automation, revision, subject, cause):
    key = _firing_key(automation, revision, project, subject, cause)
    return conn.execute("SELECT id FROM automationFirings WHERE firingKey = ?",
                        (key,)).fetchone()[0]


def firing(conn, firing_id):
    if not _has_table(conn, "automationFirings"):
        return None
    row = conn.execute(f"SELECT {_selected(conn)} FROM automationFirings"
                       " WHERE id = ?", (firing_id,)).fetchone()
    return None if row is None else _firing(row)


def _has_column(conn, table, name):
    return any(row[1] == name for row in
               conn.execute(f"PRAGMA table_info({table})"))


def firing_interventions(conn, firing_id):
    if not _has_column(conn, "interventions", "firingId"):
        return []
    rows = conn.execute(
        'SELECT i.id, i.runId, t.linearIdentifier, i."action", i.source,'
        " i.actor, i.note, i.at FROM interventions AS i"
        " LEFT JOIN runs AS r ON r.id = i.runId"
        " LEFT JOIN tickets AS t ON t.id = r.ticketId"
        " WHERE i.firingId = ? ORDER BY i.id", (firing_id,)).fetchall()
    return [FiringIntervention(*row) for row in rows]


def firing_counts(conn, since_ms):
    states = dict.fromkeys((state.value for state in FiringState), 0)
    if not _has_table(conn, "automationFirings"):
        return {"states": states, "holds": 0, "released": 0}
    states.update(conn.execute(
        "SELECT state, COUNT(*) FROM automationFirings WHERE createdAt >= ?"
        " GROUP BY state", (since_ms,)).fetchall())
    holds = conn.execute(
        "SELECT COUNT(*) FROM mergeHolds WHERE heldAt >= ?",
        (since_ms,)).fetchone()[0] if _has_table(conn, "mergeHolds") else 0
    released = conn.execute(
        "SELECT COUNT(*) FROM interventions WHERE source = 'automation'"
        " AND \"action\" = 'babysit' AND at >= ?", (since_ms,)).fetchone()[0]
    return {"states": states, "holds": holds, "released": released}


def settle_unknown(conn, firing_id, state, note, login, *, now):
    if state not in (CONFIRMED, CANCELED):
        raise ValueError(f"an unknown firing settles as {CONFIRMED} or"
                         f" {CANCELED}, not {state!r}")
    with store.transaction(conn):
        cursor = conn.execute(
            "UPDATE automationFirings SET state = ?, detail = json_set(detail,"
            " '$.settled_by', json_object('note', ?, 'login', ?, 'at', ?))"
            " WHERE id = ? AND state = ?",
            (state, note, login, now, firing_id, UNKNOWN))
        return cursor.rowcount == 1


def take_firing(conn, firing_id, pid, *, now):
    with store.transaction(conn):
        cursor = conn.execute(
            "UPDATE automationFirings SET workerPid = ? WHERE id = ?"
            " AND state = ? AND workerPid IS NULL AND leaseUntil > ?",
            (pid, firing_id, LEASED, now))
        return firing(conn, firing_id) if cursor.rowcount else None


def settle_firing(conn, firing_id, pid, state, reason, *, now, detail=None):
    if state not in SETTLED:
        raise ValueError(f"a firing settles as one of {sorted(SETTLED)},"
                         f" not {state!r}")
    with store.transaction(conn):
        cursor = conn.execute(
            "UPDATE automationFirings SET state = ?, reason = ?,"
            " detail = COALESCE(?, detail), settledAt = ?"
            " WHERE id = ? AND state = ? AND workerPid IS ?",
            (state, reason, None if detail is None
             else json.dumps(detail, sort_keys=True), now, firing_id,
             LEASED, pid))
        return cursor.rowcount == 1


def replay_firing(conn, firing_id, *, lease_until, now):
    with store.transaction(conn):
        cursor = conn.execute(
            "UPDATE automationFirings SET leaseUntil = ?, detail = json_set("
            "detail, '$.replays', COALESCE(json_extract(detail, '$.replays'),"
            " 0) + 1) WHERE id = ? AND state = ? AND workerPid IS NULL"
            " AND leaseUntil <= ?", (lease_until, firing_id, LEASED, now))
        return cursor.rowcount == 1


def record_result(conn, firing_id, result, reason, *, now):
    with store.transaction(conn):
        cursor = conn.execute(
            "UPDATE automationFirings SET reason = ?, detail = json_set("
            "detail, '$.result', ?, '$.result_at', ?) WHERE id = ?"
            " AND state = ? AND json_extract(detail, '$.result') IS NULL",
            (reason, result, now, firing_id, CONFIRMED))
        return cursor.rowcount == 1


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                        " AND name = ?", (name,)).fetchone() is not None


def automation_pause(conn, automation):
    if not _has_table(conn, "automationPauses"):
        return None
    row = conn.execute(
        f"SELECT {', '.join(Pause._fields)} FROM automationPauses"
        " WHERE automation = ? AND clearedAt IS NULL", (automation,)).fetchone()
    return None if row is None else Pause(*row)


def pause_automation(conn, automation, note, *, actor, now):
    with store.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO automationPauses (automation, note, actor, createdAt)"
            " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (automation, note, actor, now))
        return cursor.lastrowid if cursor.rowcount else None


def resume_automation(conn, automation, note, *, now):
    with store.transaction(conn):
        standing = automation_pause(conn, automation)
        if standing is None:
            return None
        conn.execute("UPDATE automationPauses SET clearedAt = ?, clearNote = ?"
                     " WHERE id = ?", (now, note, standing.id))
        return standing.id


def open_merge_hold(conn, project):
    if not _has_table(conn, "mergeHolds"):
        return None
    row = conn.execute(
        f"SELECT {', '.join(MergeHold._fields)} FROM mergeHolds"
        " WHERE projectId = ? AND endedAt IS NULL", (project,)).fetchone()
    return None if row is None else MergeHold(*row[:3], json.loads(row[3]),
                                              *row[4:])


def firing_holds(conn, firing):
    if not _has_table(conn, "mergeHolds"):
        return []
    rows = conn.execute(
        f"SELECT {', '.join(MergeHold._fields)} FROM mergeHolds"
        " WHERE firingId = ? OR id = ? ORDER BY id",
        (firing.id, firing.detail.get("hold"))).fetchall()
    return [MergeHold(*row[:3], json.loads(row[3]), *row[4:]) for row in rows]


def held_runs(conn, hold_id):
    rows = conn.execute(
        "WITH hold AS (SELECT id, projectId FROM mergeHolds WHERE id = ?),"
        " chain AS (SELECT m.id FROM mergeHolds AS m, hold"
        " WHERE m.projectId = hold.projectId AND m.id <= hold.id"
        " AND m.id > COALESCE((SELECT MAX(p.id) FROM mergeHolds AS p"
        " WHERE p.projectId = hold.projectId AND p.id < hold.id"
        " AND p.endReason NOT LIKE ? || '%'), 0))"
        " SELECT e.runId, r.ticketId, t.linearIdentifier, t.lastRunId, r.phase,"
        " r.endedAt, EXISTS (SELECT 1 FROM interventions AS i"
        " WHERE i.runId = e.runId AND i.source = 'human' AND i.at >= e.at),"
        " EXISTS (SELECT 1 FROM runEvents AS p WHERE p.runId = e.runId"
        " AND p.id > e.id AND p.kind IN ('pr_wake_breaker', 'ci_expired'))"
        " OR (SELECT COUNT(*) FROM runEvents AS p WHERE p.runId = e.runId"
        " AND p.id > e.id AND p.kind = 'phase_change'"
        " AND p.summary GLOB '* -> awaiting_merge_approval*') > 1"
        " FROM runEvents AS e JOIN runs AS r ON r.id = e.runId"
        " JOIN tickets AS t ON t.id = r.ticketId"
        " WHERE e.kind = 'main_hold_parked'"
        " AND json_extract(e.payload, '$.hold') IN (SELECT id FROM chain)"
        " AND e.id = (SELECT MAX(id) FROM runEvents"
        " WHERE runId = e.runId AND kind = 'main_hold_parked')"
        " ORDER BY e.runId", (hold_id, SUPERSEDED)).fetchall()
    return [HeldRun(*row[:6], bool(row[6]), bool(row[7])) for row in rows]


def end_merge_hold(conn, hold_id, reason, *, now):
    with store.transaction(conn):
        cursor = conn.execute(
            "UPDATE mergeHolds SET endedAt = ?, endReason = ?"
            " WHERE id = ? AND endedAt IS NULL", (now, reason, hold_id))
        return cursor.rowcount == 1


def place_merge_hold(conn, project, firing_id, sha, checks, reason, *, now):
    with store.transaction(conn):
        standing = open_merge_hold(conn, project)
        if standing is not None:
            end_merge_hold(conn, standing.id, f"{SUPERSEDED}{sha[:12]}",
                           now=now)
        hold = conn.execute(
            "INSERT INTO mergeHolds (projectId, sha, checks, firingId, heldAt)"
            " VALUES (?, ?, ?, ?, ?)",
            (project, sha, json.dumps(list(checks)), firing_id, now)).lastrowid
        conn.execute(
            "UPDATE automationFirings SET state = ?, reason = ?, settledAt = ?"
            " WHERE id = ? AND state = ?",
            (FiringState.CONFIRMED.value, reason, now, firing_id, LEASED))
        return hold

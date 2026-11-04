"""Automation firings: one `automationFirings` row per firing key, reserved once."""
import json
from typing import NamedTuple

import store

from .enums import FiringMode, FiringState, PauseActor, check_clause

PROPOSED = FiringState.PROPOSED.value
REFUSED = FiringState.REFUSED.value
LEASED = FiringState.LEASED.value
CONFIRMED = FiringState.CONFIRMED.value
UNKNOWN = FiringState.UNKNOWN.value
CANCELED = FiringState.CANCELED.value
SETTLED = frozenset((REFUSED, CONFIRMED, UNKNOWN, CANCELED))
OPERATOR = PauseActor.OPERATOR.value
AUTOMATION = PauseActor.AUTOMATION.value

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


class ActCounts(NamedTuple):
    queued: int
    hour: int
    today: int


def act_counts(conn, project, automation, action, *, hour_since, day_since):
    if not _has_table(conn, "automationFirings"):
        return ActCounts(0, 0, 0)
    row = conn.execute(
        "SELECT COALESCE(SUM(state = ?), 0), COALESCE(SUM(createdAt >= ?), 0),"
        " COALESCE(SUM(createdAt >= ?), 0) FROM automationFirings"
        " WHERE projectId = ? AND automation = ? AND mode = 'act'"
        " AND json_extract(detail, '$.action') = ?"
        " AND json_extract(detail, '$.limit') IS NULL",
        (LEASED, hour_since, day_since, project, automation, action)
    ).fetchone()
    return ActCounts(*row)


def reserve_firing(conn, project, automation, revision, subject, cause, *,
                   mode, reason, detail, now, lease_until=None, limit=None):
    """The id of a new row, or of a proposal on the key a lease takes over."""
    key = _firing_key(automation, revision, project, subject, cause)
    state, settled = PROPOSED if lease_until is None else LEASED, None
    with store.transaction(conn):
        limited = None if state == PROPOSED or limit is None else limit(conn)
        if limited is not None:
            state, reason, settled, lease_until = REFUSED, limited, now, None
            detail = {**detail, "limit": limited}
        row = conn.execute(
            "INSERT INTO automationFirings (projectId, automation, revision,"
            " subject, cause, firingKey, mode, state, reason, detail,"
            " createdAt, settledAt, leaseUntil)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (firingKey) DO UPDATE SET mode = excluded.mode,"
            " state = excluded.state, reason = excluded.reason,"
            " detail = excluded.detail, createdAt = excluded.createdAt,"
            " settledAt = excluded.settledAt, leaseUntil = excluded.leaseUntil"
            " WHERE automationFirings.state = ? AND excluded.state IN (?, ?)"
            " RETURNING id",
            (project, automation, revision, subject, cause, key, mode,
             state, reason, json.dumps(detail, sort_keys=True), now, settled,
             lease_until, PROPOSED, LEASED, REFUSED)).fetchone()
        return None if row is None else row[0]


def _selected(conn):
    present = {row[1] for row in
               conn.execute("PRAGMA table_info(automationFirings)")}
    return ", ".join(field if field in present else f"NULL AS {field}"
                     for field in Firing._fields)


def _firing(row):
    return Firing(*row[:10], json.loads(row[10]), *row[11:])


def firings(conn):
    """None read from a store an older build last wrote, which lacks the table."""
    if not _has_table(conn, "automationFirings"):
        return []
    rows = conn.execute(
        f"SELECT {_selected(conn)} FROM automationFirings"
        " ORDER BY createdAt DESC, id DESC").fetchall()
    return [_firing(row) for row in rows]


def keyed_firing(conn, project, automation, revision, subject, cause):
    key = _firing_key(automation, revision, project, subject, cause)
    return conn.execute("SELECT id FROM automationFirings WHERE firingKey = ?",
                        (key,)).fetchone()[0]


def firing(conn, firing_id):
    row = conn.execute(f"SELECT {_selected(conn)} FROM automationFirings"
                       " WHERE id = ?", (firing_id,)).fetchone()
    return None if row is None else _firing(row)


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


def record_result(conn, firing_id, result, reason, checks, *, now):
    with store.transaction(conn):
        cursor = conn.execute(
            "UPDATE automationFirings SET reason = ?, detail = json_set("
            "detail, '$.result', ?, '$.result_at', ?, '$.result_checks',"
            " json(?)) WHERE id = ? AND state = ?"
            " AND json_extract(detail, '$.result') IS NULL",
            (reason, result, now, json.dumps(sorted(checks)), firing_id,
             CONFIRMED))
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


def held_runs(conn, hold_id):
    return conn.execute(
        "SELECT COUNT(*) FROM runEvents AS e JOIN runs AS r ON r.id = e.runId"
        " WHERE e.kind = 'main_hold_parked' AND r.endedAt IS NULL"
        " AND r.phase = 'awaiting_merge_approval'"
        " AND json_extract(e.payload, '$.hold') = ? AND e.id = (SELECT MAX(id)"
        " FROM runEvents WHERE runId = e.runId AND kind = 'main_hold_parked')",
        (hold_id,)).fetchone()[0]


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
            end_merge_hold(conn, standing.id, f"superseded by {sha[:12]}",
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

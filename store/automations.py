"""Automation firings: one `automationFirings` row per firing key, reserved once."""
import json
from typing import NamedTuple

import store

from .enums import FiringMode, FiringState, PauseActor, check_clause

PROPOSED = FiringState.PROPOSED.value
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
    settledAt  INTEGER
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


def _firing_key(automation, revision, project, subject, cause):
    return ":".join((automation, str(revision), str(project), subject, cause))


def reserve_firing(conn, project, automation, revision, subject, cause, *,
                   mode, reason, detail, now):
    """The new row's id, or None when its key is already reserved."""
    key = _firing_key(automation, revision, project, subject, cause)
    with store.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO automationFirings (projectId, automation, revision,"
            " subject, cause, firingKey, mode, state, reason, detail,"
            " createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (firingKey) DO NOTHING",
            (project, automation, revision, subject, cause, key, mode,
             PROPOSED, reason, json.dumps(detail, sort_keys=True), now))
        return cursor.lastrowid if cursor.rowcount else None


def firings(conn, since=None):
    """None read from a store an older build last wrote, which lacks the table."""
    if not _has_table(conn, "automationFirings"):
        return []
    rows = conn.execute(
        f"SELECT {', '.join(Firing._fields)} FROM automationFirings"
        " WHERE createdAt >= ? ORDER BY createdAt DESC, id DESC",
        (0 if since is None else since,)).fetchall()
    return [Firing(*row[:10], json.loads(row[10]), *row[11:])
            for row in rows]


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

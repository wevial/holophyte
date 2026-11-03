"""Automation firings: one `automationFirings` row per firing key, reserved once."""
import json
from typing import NamedTuple

import store

from .enums import FiringMode, FiringState, check_clause

PROPOSED = FiringState.PROPOSED.value

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


def firings(conn, since=0):
    """None read from a store an older build last wrote, which lacks the table."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                    " AND name = 'automationFirings'").fetchone() is None:
        return []
    rows = conn.execute(
        f"SELECT {', '.join(Firing._fields)} FROM automationFirings"
        " WHERE createdAt >= ? ORDER BY createdAt DESC, id DESC",
        (since,)).fetchall()
    return [Firing(*row[:10], json.loads(row[10]), *row[11:])
            for row in rows]

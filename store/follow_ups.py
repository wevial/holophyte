"""A merged run's FOLLOW_UP lines: captured pending, settled once at the merge."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from . import record_event
from .enums import FollowUpKind
from .schema import _transaction


@dataclass(frozen=True)
class FollowUp:
    id: int
    runId: int
    ticketId: int
    commitSha: str
    kind: str
    kindGiven: bool
    text: str
    path: str | None
    line: int | None
    fingerprint: str


_COLUMNS = ("id, runId, ticketId, commitSha, kind, kindGiven, text, path,"
            " line, fingerprint")


def _now(now):
    return int(time.time() * 1000) if now is None else now


def _payload(row_id, kind, fingerprint, key=None):
    payload = {"id": row_id, "kind": kind, "fingerprint": fingerprint}
    if key is not None:
        payload["key"] = key
    return json.dumps(payload)


def record_follow_up(conn, run_id, commit_sha, kind, kind_given, text,
                     fingerprint, path=None, line=None, now=None):
    """None when the run already holds this fingerprint."""
    kinds = [member.value for member in FollowUpKind]
    if kind not in kinds:
        raise ValueError(f"kind {kind!r} is not one of {', '.join(kinds)}")
    now = _now(now)
    with _transaction(conn):
        row = conn.execute("SELECT ticketId FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            "INSERT INTO followUps (runId, ticketId, commitSha, kind,"
            " kindGiven, text, path, line, fingerprint, createdAt)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (runId, fingerprint) DO NOTHING",
            (run_id, row[0], commit_sha, kind, int(bool(kind_given)), text,
             path, line, fingerprint, now))
        if not cursor.rowcount:
            return None
        row_id = cursor.lastrowid
        if not kind_given:
            record_event(conn, run_id, "follow_up_kind_missing",
                         f"follow-up {row_id}: kind not given; filed as {kind}",
                         level="detail", now=now,
                         payload=_payload(row_id, kind, fingerprint))
    return row_id


def pending_follow_ups(conn, run_id):
    return [FollowUp(*row[:5], bool(row[5]), *row[6:]) for row in conn.execute(
        f"SELECT {_COLUMNS} FROM followUps WHERE runId = ?"
        " AND settledAt IS NULL ORDER BY id", (run_id,))]


def filed_drafts(conn, follow_up_id):
    """Keys earlier rows of this project filed for the fingerprint, newest first."""
    return [key for (key,) in conn.execute(
        "SELECT f.filedAs FROM followUps f JOIN runs r ON r.id = f.runId"
        " JOIN followUps this ON this.id = ?"
        " JOIN runs thisRun ON thisRun.id = this.runId"
        " WHERE r.projectId = thisRun.projectId AND f.id < this.id"
        " AND f.fingerprint = this.fingerprint AND f.kind = 'feature'"
        " AND f.filedAs IS NOT NULL ORDER BY f.id DESC", (follow_up_id,))]


def _settle(conn, follow_up_id, event, summary, now, **columns):
    now = _now(now)
    sets = "".join(f", {column} = ?" for column in columns)
    with _transaction(conn):
        row = conn.execute(
            "SELECT runId, kind, fingerprint FROM followUps WHERE id = ?",
            (follow_up_id,)).fetchone()
        if row is None:
            raise ValueError(f"no follow-up {follow_up_id}")
        run_id, kind, fingerprint = row
        changed = conn.execute(
            f"UPDATE followUps SET settledAt = ?{sets}"
            " WHERE id = ? AND settledAt IS NULL",
            (now, *columns.values(), follow_up_id)).rowcount
        if not changed:
            return False
        key = columns.get("filedAs") or columns.get("duplicateOf")
        record_event(conn, run_id, event,
                     f"follow-up {follow_up_id}: {summary}", level="detail",
                     now=now, payload=_payload(follow_up_id, kind, fingerprint,
                                               key))
    return True


def settle_filed(conn, follow_up_id, key, now=None):
    return _settle(conn, follow_up_id, "follow_up_filed",
                   f"filed as {key}", now, filedAs=key)


def settle_duplicate(conn, follow_up_id, key, now=None):
    return _settle(conn, follow_up_id, "follow_up_duplicate",
                   f"duplicate of open draft {key}", now, duplicateOf=key)


def settle_ledger(conn, follow_up_id, now=None):
    return _settle(conn, follow_up_id, "follow_up_ledger",
                   "kept in the findings ledger", now)


def settle_unfiled(conn, follow_up_id, error, now=None):
    return _settle(conn, follow_up_id, "follow_up_unfiled",
                   f"not filed: {error}", now, error=error)

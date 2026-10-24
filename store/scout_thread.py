"""A scout's thread: a person's asks and steers, and the scout's answers."""
import json
import time
from typing import NamedTuple

from holophyte.redact import redact_document, redact_values

from .enums import ScoutPhase, ThreadMode
from .schema import _transaction
from .scouts import (
    ANSWERING,
    PASS_STARTED,
    PLANNING,
    _reopen_scout,
    record_scout_event,
)

ASK, STEER, ANSWER = (mode.value for mode in ThreadMode)
DONE = ScoutPhase.DONE.value
BLOCK_TYPES = ("finding", "action", "proposal")
ANSWER_STARTED = "scout_answer_started"
COLUMNS = ("id", "projectId", "scoutId", "author", "mode", "text", "blocks",
           "answersId", "at", "consumedBy", "consumedAt")
SELECT = f"SELECT {', '.join(COLUMNS)} FROM threadMessages"


class ThreadRefused(Exception):
    def __init__(self, subject, detail):
        super().__init__(f"{subject}: {detail}")
        self.subject = subject


class Message(NamedTuple):
    id: int
    project_id: int
    scout_id: int | None
    author: str
    mode: str
    text: str
    blocks: list
    answers_id: int | None
    at: int
    consumed_by: int | None
    consumed_at: int | None


def _now(now):
    return int(time.time() * 1000) if now is None else now


def _waiting(conn, scout_id):
    row = conn.execute(
        "SELECT id FROM threadMessages WHERE scoutId = ? AND mode IN (?, ?)"
        " AND consumedBy IS NULL ORDER BY at, id LIMIT 1",
        (scout_id, ASK, STEER)).fetchone()
    return None if row is None else row[0]


def _blocks(blocks):
    blocks = list(blocks)
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in BLOCK_TYPES:
            raise ThreadRefused("block", f"type {kind!r} is not one of"
                                f" {', '.join(BLOCK_TYPES)}")
    return blocks


def post_message(conn, scout_id, author, mode, text, *, blocks=(),
                 answers_id=None, now=None):
    for name, value in (("text", text), ("author", author)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-blank text")
    if mode not in (ASK, STEER, ANSWER):
        raise ValueError(f"unknown thread mode {mode!r}")
    blocks = json.dumps(redact_document(_blocks(blocks)))
    with _transaction(conn):
        row = conn.execute("SELECT projectId, phase FROM scouts WHERE id = ?",
                           (scout_id,)).fetchone()
        if row is None:
            raise ThreadRefused(f"scout {scout_id}", "no such scout")
        project_id, phase = row
        if mode != ANSWER:
            if phase != DONE:
                raise ThreadRefused(f"scout {scout_id}",
                                    f"it is {phase}, not done")
            waiting = _waiting(conn, scout_id)
            if waiting is not None:
                raise ThreadRefused(f"scout {scout_id}",
                                    f"message {waiting} is not taken yet")
        if answers_id is not None:
            answered = conn.execute("SELECT scoutId FROM threadMessages"
                                    " WHERE id = ?", (answers_id,)).fetchone()
            if answered is None or answered[0] != scout_id:
                raise ThreadRefused(f"message {answers_id}",
                                    f"not a message of scout {scout_id}")
        return conn.execute(
            "INSERT INTO threadMessages (projectId, scoutId, author, mode,"
            " text, blocks, answersId, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, scout_id, author.strip(), mode,
             redact_values(text.strip()), blocks, answers_id,
             _now(now))).lastrowid


def take_message(conn, message_id, host=None, now=None):
    now = _now(now)
    with _transaction(conn):
        row = conn.execute(
            "SELECT m.scoutId, m.mode, m.consumedBy, s.phase FROM"
            " threadMessages m JOIN scouts s ON s.id = m.scoutId"
            " WHERE m.id = ?", (message_id,)).fetchone()
        if row is None:
            raise ThreadRefused(f"message {message_id}", "no such message")
        scout_id, mode, consumed_by, phase = row
        subject = f"message {message_id}"
        if consumed_by is not None:
            raise ThreadRefused(subject, f"already taken by event {consumed_by}")
        if mode == ANSWER:
            raise ThreadRefused(subject, "an answer is not taken")
        if phase != DONE:
            raise ThreadRefused(subject, f"scout {scout_id} is {phase}, not done")
        if mode == ASK:
            _reopen_scout(conn, scout_id, ANSWERING, host, now)
            payload = {"message": message_id}
            seq = record_scout_event(
                conn, scout_id, ANSWER_STARTED,
                f"answering message {message_id}", level="detail",
                payload=json.dumps(payload), now=now)
        else:
            (last,) = conn.execute(
                "SELECT COALESCE(MAX(pass), 0) FROM scoutReports"
                " WHERE scoutId = ?", (scout_id,)).fetchone()
            _reopen_scout(conn, scout_id, PLANNING, host, now)
            payload = {"message": message_id, "pass": last + 1}
            seq = record_scout_event(
                conn, scout_id, PASS_STARTED,
                f"pass {last + 1} steered by message {message_id}",
                level="detail", payload=json.dumps(payload), now=now)
        (event_id,) = conn.execute(
            "SELECT id FROM runEvents WHERE scoutId = ? AND seq = ?",
            (scout_id, seq)).fetchone()
        conn.execute("UPDATE threadMessages SET consumedBy = ?, consumedAt = ?"
                     " WHERE id = ?", (event_id, now, message_id))
        return event_id


def _message(row):
    values = dict(zip(COLUMNS, row))
    values["blocks"] = json.loads(values["blocks"])
    return Message(*values.values())


def messages(conn, scout_id):
    rows = conn.execute(f"{SELECT} WHERE scoutId = ? ORDER BY at, id",
                        (scout_id,)).fetchall()
    return [_message(row) for row in rows]


def waiting_messages(conn):
    rows = conn.execute(
        f"{SELECT} WHERE mode IN (?, ?) AND consumedBy IS NULL AND scoutId IN"
        " (SELECT id FROM scouts WHERE phase = ?) ORDER BY at, id",
        (ASK, STEER, DONE)).fetchall()
    return [_message(row) for row in rows]

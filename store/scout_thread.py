"""A scout's thread: a person's asks and steers, and the scout's answers."""
import json
from typing import NamedTuple

from holophyte.redact import redact_document, redact_values

from .enums import ThreadMode
from .schema import _transaction
from .scouts import (
    ANSWERING,
    DONE,
    PASS_STARTED,
    PLANNING,
    _now,
    _reopen_scout,
    move_scout,
    record_scout_event,
)

ASK, STEER, ANSWER = (mode.value for mode in ThreadMode)
BLOCK_TYPES = ("finding", "action", "proposal")
ANSWER_STARTED = "scout_answer_started"
START_FAILED = "scout_start_failed"
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


def _waiting(conn, scout_id):
    row = conn.execute(
        "SELECT id FROM threadMessages WHERE scoutId = ? AND mode IN (?, ?)"
        " AND consumedBy IS NULL ORDER BY at, id LIMIT 1",
        (scout_id, ASK, STEER)).fetchone()
    return None if row is None else row[0]


def _blocks(scout_id, blocks):
    blocks = list(blocks)
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in BLOCK_TYPES:
            raise ThreadRefused(f"scout {scout_id}",
                                f"block type {kind!r} is not one of"
                                f" {', '.join(BLOCK_TYPES)}")
    return blocks


def _answered(conn, scout_id, answers_id):
    row = conn.execute("SELECT scoutId, mode, blocks FROM threadMessages"
                       " WHERE id = ?", (answers_id,)).fetchone()
    if row is None or row[0] != scout_id:
        raise ThreadRefused(f"message {answers_id}",
                            f"not a message of scout {scout_id}")
    return row[1], json.loads(row[2])


def _proposes(blocks):
    return any(block.get("type") == "proposal" for block in blocks)


def _anchor(conn, scout_id, answers_id, blocks):
    if answers_id is None:
        raise ThreadRefused(f"scout {scout_id}", "an answer names its ask")
    mode, _ = _answered(conn, scout_id, answers_id)
    if mode != ASK and not (mode == STEER and _proposes(blocks)):
        raise ThreadRefused(f"message {answers_id}",
                            f"it is {mode}; an answer answers an ask, and a"
                            " card an ask or a steer")


def _card(conn, scout_id, answers_id):
    mode, blocks = _answered(conn, scout_id, answers_id)
    if mode != ANSWER or not _proposes(blocks):
        raise ThreadRefused(f"message {answers_id}",
                            f"it is {mode}, not a card of the scout")
    taken = conn.execute(
        "SELECT id FROM threadMessages WHERE answersId = ? AND mode IN (?, ?)"
        " ORDER BY id LIMIT 1", (answers_id, ASK, STEER)).fetchone()
    if taken is not None:
        raise ThreadRefused(f"message {answers_id}",
                            f"message {taken[0]} already answers the card")


def _person(conn, scout_id, phase, answers_id):
    if phase != DONE:
        raise ThreadRefused(f"scout {scout_id}", f"it is {phase}, not done")
    if answers_id is not None:
        _card(conn, scout_id, answers_id)
    waiting = _waiting(conn, scout_id)
    if waiting is not None:
        raise ThreadRefused(f"scout {scout_id}",
                            f"message {waiting} is not taken yet")


def post_message(conn, scout_id, author, mode, text, *, blocks=(),
                 answers_id=None, now=None):
    for name, value in (("text", text), ("author", author)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be non-blank text")
    if mode not in (ASK, STEER, ANSWER):
        raise ValueError(f"unknown thread mode {mode!r}")
    listed = _blocks(scout_id, blocks)
    with _transaction(conn):
        row = conn.execute("SELECT projectId, phase FROM scouts WHERE id = ?",
                           (scout_id,)).fetchone()
        if row is None:
            raise ThreadRefused(f"scout {scout_id}", "no such scout")
        project_id, phase = row
        if mode == ANSWER:
            _anchor(conn, scout_id, answers_id, listed)
        else:
            _person(conn, scout_id, phase, answers_id)
        return conn.execute(
            "INSERT INTO threadMessages (projectId, scoutId, author, mode,"
            " text, blocks, answersId, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, scout_id, author.strip(), mode,
             redact_values(text.strip()),
             json.dumps(redact_document(listed)), answers_id,
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
            to, kind, payload = ANSWERING, ANSWER_STARTED, {"message": message_id}
            summary = f"answering message {message_id}"
        else:
            (last,) = conn.execute(
                "SELECT COALESCE(MAX(pass), 0) FROM scoutReports"
                " WHERE scoutId = ?", (scout_id,)).fetchone()
            to, kind = PLANNING, PASS_STARTED
            payload = {"message": message_id, "pass": last + 1}
            summary = f"pass {last + 1} steered by message {message_id}"
        _reopen_scout(conn, scout_id, to, host, now)
        seq = record_scout_event(conn, scout_id, kind, summary, level="detail",
                                 payload=json.dumps(payload), now=now)
        (event_id,) = conn.execute(
            "SELECT id FROM runEvents WHERE scoutId = ? AND seq = ?",
            (scout_id, seq)).fetchone()
        conn.execute("UPDATE threadMessages SET consumedBy = ?, consumedAt = ?"
                     " WHERE id = ?", (event_id, now, message_id))
        return event_id


def release_message(conn, message_id, reason, now=None):
    with _transaction(conn):
        row = conn.execute(
            "SELECT m.scoutId, m.consumedBy, s.phase FROM threadMessages m"
            " JOIN scouts s ON s.id = m.scoutId WHERE m.id = ?",
            (message_id,)).fetchone()
        if row is None:
            raise ThreadRefused(f"message {message_id}", "no such message")
        scout_id, consumed_by, phase = row
        if consumed_by is None or phase != ANSWERING:
            raise ThreadRefused(f"message {message_id}",
                                f"not an answer in progress (scout {scout_id}"
                                f" is {phase})")
        record_scout_event(conn, scout_id, START_FAILED,
                           f"could not start: {reason}; message {message_id}"
                           " waits for the next pass", level="detail",
                           payload=json.dumps({"message": message_id,
                                               "reason": reason}), now=now)
        conn.execute("UPDATE threadMessages SET consumedBy = NULL,"
                     " consumedAt = NULL WHERE id = ?", (message_id,))
        move_scout(conn, scout_id, DONE)


def _message(row):
    message = Message(*row)
    return message._replace(blocks=json.loads(message.blocks))


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

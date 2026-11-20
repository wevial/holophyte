"""Each attempt of a pipeline stage within a run and the agent turns it owns."""
import json
import time
from typing import NamedTuple

from holophyte.redact import redact_document

from .schema import _has_table, _transaction


class StageAttempt(NamedTuple):
    stage: str
    attempt: int
    started_ms: int
    ended_ms: int | None
    input_sha: str | None
    output_sha: str | None
    outcome: str | None
    edge: str | None
    seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    detail: dict


def _now(now):
    return int(time.time() * 1000) if now is None else now


def _next_attempt(conn, run_id, stage_id, open_seq=None):
    if conn.execute("SELECT 1 FROM runs WHERE id = ?",
                    (run_id,)).fetchone() is None:
        raise ValueError(f"no run {run_id}")
    (attempt,) = conn.execute(
        "SELECT COALESCE(MAX(attempt), 0) + 1 FROM stageAttempts"
        " WHERE runId = ? AND stageId = ?", (run_id, stage_id)).fetchone()
    (seq,) = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM runEvents WHERE runId = ?",
        (run_id,)).fetchone()
    return attempt, seq if open_seq is None else open_seq


def _document(detail):
    return json.dumps(redact_document(dict(detail or {})), sort_keys=True)


def open_stage(conn, run_id, stage_id, input_sha=None, detail=None, now=None,
               open_seq=None):
    with _transaction(conn):
        attempt, seq = _next_attempt(conn, run_id, stage_id, open_seq)
        return conn.execute(
            "INSERT INTO stageAttempts (runId, stageId, attempt, startedAt,"
            " inputSha, openSeq, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, stage_id, attempt, _now(now), input_sha, seq,
             _document(detail))).lastrowid


def close_stage(conn, attempt_id, outcome, edge, output_sha=None, roles=(),
                detail=None, now=None):
    roles = list(roles)
    with _transaction(conn):
        row = conn.execute(
            "SELECT runId, openSeq, endedAt, detail FROM stageAttempts"
            " WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise ValueError(f"no stage attempt {attempt_id}")
        run_id, open_seq, ended_at, stored = row
        if ended_at is not None:
            raise ValueError(f"stage attempt {attempt_id} is already closed")
        marks = ", ".join("?" * len(roles))
        seconds, input_tokens, output_tokens, cost = conn.execute(
            "SELECT SUM(json_extract(payload, '$.seconds')),"
            " SUM(json_extract(payload, '$.usage.input_tokens')),"
            " SUM(json_extract(payload, '$.usage.output_tokens')),"
            " SUM(json_extract(payload, '$.usage.cost_usd'))"
            " FROM runEvents WHERE runId = ? AND kind = 'agent_turn'"
            " AND seq > ?"
            f" AND json_extract(payload, '$.role') IN ({marks})",
            (run_id, open_seq, *roles)).fetchone()
        conn.execute(
            "UPDATE stageAttempts SET endedAt = ?, outputSha = ?, outcome = ?,"
            " edge = ?, agentSeconds = ?, inputTokens = ?, outputTokens = ?,"
            " costUsd = ?, detail = ? WHERE id = ?",
            (_now(now), output_sha, outcome, edge, seconds, input_tokens,
             output_tokens, cost,
             _document({**json.loads(stored), **(detail or {})}), attempt_id))


def skip_stage(conn, run_id, stage_id, reason, sha=None, now=None):
    now = _now(now)
    with _transaction(conn):
        attempt, seq = _next_attempt(conn, run_id, stage_id)
        return conn.execute(
            "INSERT INTO stageAttempts (runId, stageId, attempt, startedAt,"
            " endedAt, inputSha, outputSha, outcome, edge, openSeq, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'skipped', 'next', ?, ?)",
            (run_id, stage_id, attempt, now, now, sha, sha, seq,
             _document({"reason": reason}))).lastrowid


def stage_attempts(conn, run_ids):
    run_ids = tuple(run_ids)
    found = {run_id: [] for run_id in run_ids}
    if not run_ids or not _has_table(conn, "stageAttempts"):
        return found
    rows = conn.execute(
        "SELECT runId, stageId, attempt, startedAt, endedAt, inputSha,"
        " outputSha, outcome, edge, agentSeconds, inputTokens, outputTokens,"
        " costUsd, detail FROM stageAttempts"
        f" WHERE runId IN ({', '.join('?' * len(run_ids))})"
        " ORDER BY startedAt, id", run_ids)
    for run_id, *row, detail in rows:
        found[run_id].append(StageAttempt(*row, json.loads(detail)))
    return found

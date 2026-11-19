from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import store.read
from holophyte.config.serve_settings import serve_config

REDACTION_VERSION = 1
PRIVATE = "[private]"
SECRET = "[secret]"
SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?"
               r"-----END [A-Z ]*PRIVATE KEY-----|.*)", re.S),
    re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"),
)
RUNS, EVENTS, SESSIONS, MANIFEST = (
    "factory_runs.jsonl", "factory_events.jsonl", "agent_sessions.jsonl",
    "manifest.json")


def iso(ms):
    if ms is None:
        return None
    moment = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Redactor:
    def __init__(self, private):
        self.private = tuple(private)

    def text(self, value):
        if value is None:
            return None
        for pattern in self.private:
            value = pattern.sub(PRIVATE, value)
        for pattern in SECRETS:
            value = pattern.sub(SECRET, value)
        return value

    def tree(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, dict):
            return {self.text(key): self.tree(item)
                    for key, item in value.items()}
        return value

    def payload(self, raw):
        if raw is None:
            return None
        try:
            return self.tree(json.loads(raw))
        except ValueError:
            return self.text(raw)


def _runs(conn, since):
    return conn.execute(
        "SELECT r.id, t.linearIdentifier, r.attempt, r.outcome, r.failureKind,"
        " r.mergeSha, r.reviewRoundCount, r.startedAt, r.endedAt"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.startedAt >= ? ORDER BY r.id", (since or 0,)).fetchall()


def _events(conn, since):
    return conn.execute(
        "SELECT runId, seq, at, kind, summary, payload FROM runEvents"
        " WHERE runId IN (SELECT id FROM runs WHERE startedAt >= ?)"
        " ORDER BY runId, seq", (since or 0,)).fetchall()


def records(conn, project, redactor, since=None):
    runs, events, sessions = [], [], []
    keys = {}
    for (run_id, key, attempt, outcome, failure_kind, merge_sha, rounds,
         started, ended) in _runs(conn, since):
        keys[run_id] = key
        runs.append({
            "run_id": run_id, "ticket_key": key, "project": project,
            "attempt": attempt, "outcome": outcome,
            "failure_kind": failure_kind, "merge_sha": merge_sha,
            "review_round_count": rounds, "started_at": iso(started),
            "ended_at": iso(ended)})
    for run_id, seq, at, kind, summary, raw in _events(conn, since):
        payload = redactor.payload(raw)
        events.append({"run_id": run_id, "seq": seq, "at": iso(at),
                       "kind": kind, "summary": redactor.text(summary),
                       "payload": payload})
        if kind == "agent_session" and isinstance(payload, dict):
            sessions.append({
                "session_id": payload.get("session_id"), "run_id": run_id,
                "ticket_key": keys[run_id], "role": payload.get("role"),
                "route": payload.get("route"), "at": iso(at)})
    return {RUNS: runs, EVENTS: events, SESSIONS: sessions}


def _write_lines(path, rows):
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False))
            stream.write("\n")


def export(target, directory, since=None, clock=time.time):
    from holophyte.leak_guard import patterns
    started = int(clock() * 1000)
    if not target.store_path.is_file():
        raise SystemExit(f"[holo2] no store at {target.store_path}")
    project = serve_config(target).name
    redactor = Redactor(patterns(target))
    conn = store.read.open_readonly(target.store_path)
    try:
        conn.execute("BEGIN")
        files = records(conn, project, redactor, since)
    finally:
        conn.close()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        _write_lines(directory / name, rows)
    manifest = {
        "project": project,
        "files": {name: len(rows) for name, rows in files.items()},
        "started_at": iso(started), "ended_at": iso(int(clock() * 1000)),
        "since": since, "redaction_version": REDACTION_VERSION}
    (directory / MANIFEST).write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(f"[holo2] exported {len(files[RUNS])} runs, {len(files[EVENTS])}"
          f" events and {len(files[SESSIONS])} agent sessions to {directory}")
    return manifest

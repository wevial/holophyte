from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import store.read
from holophyte.config.serve_settings import serve_config

REDACTION_VERSION = 1
SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.*?"
               r"-----END [A-Z ]*PRIVATE KEY-----|.*)", re.S),
    re.compile(r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
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


def _merged(spans):
    merged = []
    for start, end, secret in sorted(spans):
        if merged and start < merged[-1][1]:
            last = merged[-1]
            merged[-1] = (last[0], max(last[1], end), last[2] or secret)
        else:
            merged.append((start, end, secret))
    return merged


def _parsed(raw):
    try:
        return None if raw is None else json.loads(raw)
    except ValueError:
        return raw


class Redactor:
    def __init__(self, private):
        self.private = tuple(private)

    def _spans(self, value):
        offset = 0
        for line in value.split("\n"):
            for pattern in self.private:
                for match in pattern.finditer(line):
                    if match.end() > match.start():
                        yield offset + match.start(), offset + match.end(), False
            offset += len(line) + 1
        for pattern in SECRETS:
            for match in pattern.finditer(value):
                yield match.start(), match.end(), True

    def text(self, value):
        parts, cursor = [], 0
        for start, end, secret in _merged(self._spans(value)):
            parts += [value[cursor:start], "[secret]" if secret else "[private]"]
            cursor = end
        return "".join(parts) + value[cursor:]

    def tree(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.tree(item) for item in value]
        if isinstance(value, dict):
            return {self.text(key): self.tree(item)
                    for key, item in value.items()}
        return value


def _runs(conn, project_id, since):
    return conn.execute(
        "SELECT r.id, t.linearIdentifier, r.attempt, r.outcome, r.failureKind,"
        " r.mergeSha, r.reviewRoundCount, r.startedAt, r.endedAt"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.projectId = ? AND r.startedAt >= ? ORDER BY r.id",
        (project_id, since or 0)).fetchall()


def _events(conn, project_id, since):
    return conn.execute(
        "SELECT runId, seq, at, kind, summary, payload FROM runEvents"
        " WHERE runId IN (SELECT id FROM runs"
        " WHERE projectId = ? AND startedAt >= ?)"
        " ORDER BY runId, seq", (project_id, since or 0)).fetchall()


def records(conn, project_id, project, redactor, since):
    runs, events, sessions = [], [], []
    keys = {}
    for (run_id, key, attempt, outcome, failure_kind, merge_sha, rounds,
         started, ended) in _runs(conn, project_id, since):
        keys[run_id] = key
        runs.append({
            "run_id": run_id, "ticket_key": key, "project": project,
            "attempt": attempt, "outcome": outcome,
            "failure_kind": failure_kind, "merge_sha": merge_sha,
            "review_round_count": rounds, "started_at": iso(started),
            "ended_at": iso(ended)})
    for run_id, seq, at, kind, summary, raw in _events(conn, project_id, since):
        parsed = _parsed(raw)
        events.append({"run_id": run_id, "seq": seq, "at": iso(at),
                       "kind": kind, "summary": redactor.text(summary),
                       "payload": redactor.tree(parsed)})
        if kind == "agent_session" and isinstance(parsed, dict):
            field = {name: redactor.tree(parsed.get(name))
                     for name in ("session_id", "role", "route")}
            sessions.append({**field, "run_id": run_id,
                             "ticket_key": keys[run_id], "at": iso(at)})
    return {RUNS: runs, EVENTS: events, SESSIONS: sessions}


def _replace(path, text):
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".export-")
    mask = os.umask(0)
    os.umask(mask)
    try:
        os.fchmod(handle, 0o666 & ~mask)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise


def _lines(rows):
    return "".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
                   for row in rows)


def export(target, directory, since):
    from holophyte.admission import project_of
    from holophyte.leak_guard import patterns
    started = int(time.time() * 1000)
    if not target.store_path.is_file():
        raise SystemExit(f"[holo2] no store at {target.store_path}")
    redactor = Redactor(patterns(target))
    project = redactor.text(serve_config(target).name)
    conn = store.read.open_readonly(target.store_path)
    try:
        conn.execute("BEGIN")
        project_id = project_of(conn, target)
        if project_id is None:
            raise SystemExit(f"[holo2] {target.store_path} holds no project"
                             f" row for {target.path}")
        files = records(conn, project_id, project, redactor, since)
    finally:
        conn.close()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        _replace(directory / name, _lines(rows))
    manifest = {
        "project": project,
        "files": {name: len(rows) for name, rows in files.items()},
        "started_at": iso(started), "ended_at": iso(int(time.time() * 1000)),
        "since": since, "redaction_version": REDACTION_VERSION}
    _replace(directory / MANIFEST,
             json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    print(f"[holo2] exported {len(files[RUNS])} runs, {len(files[EVENTS])}"
          f" events and {len(files[SESSIONS])} agent sessions to {directory}")

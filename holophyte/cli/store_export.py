from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import store.read
from holophyte.config.serve_settings import serve_config

REDACTION_VERSION = 1
TOKEN = r"(?:\"(?:[^\"\\\n]|\\.)*\"?|'(?:[^'\\\n]|\\.)*'?|[\w.~+/=-]+)"
SECRETS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----(?:.*?"
               r"-----END [A-Z0-9 ]*PRIVATE KEY[A-Z ]*-----|.*)", re.S),
    re.compile(r"\b(?i:authorization)\s*[:=]\s*(?:[A-Za-z]+\s+)?" + TOKEN),
    re.compile(r"\b(?i:bearer)\s+" + TOKEN),
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


def _number(text):
    value = float(text)
    return value if math.isfinite(value) else text


def _parsed(raw):
    try:
        return None if raw is None else json.loads(
            raw, parse_float=_number, parse_constant=str)
    except ValueError:
        return raw


def _unique(key, taken):
    candidate, n = key, 1
    while candidate in taken:
        n += 1
        candidate = f"{key}#{n}"
    return candidate


class Redactor:
    def __init__(self, private):
        self.private = tuple(private)

    def _private(self, value):
        offset = 0
        for line in value.split("\n"):
            for pattern in self.private:
                for match in pattern.finditer(line):
                    if match.end() > match.start():
                        yield offset + match.start(), offset + match.end(), False
            offset += len(line) + 1

    def _spans(self, value):
        private = list(self._private(value))
        masked = list(value)
        for start, end, _ in private:
            masked[start:end] = " " * (end - start)
        yield from private
        for text in (value, "".join(masked)):
            for pattern in SECRETS:
                match = pattern.search(text)
                while match:
                    yield match.start(), match.end(), True
                    match = pattern.search(text, match.start() + 1)

    def text(self, value):
        value = value.encode("utf-8", "replace").decode("utf-8")
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
            redacted = {}
            for key, item in value.items():
                redacted[_unique(self.text(key), redacted)] = self.tree(item)
            return redacted
        return value


def _runs(conn, project_id, since):
    return conn.execute(
        "SELECT r.id, t.linearIdentifier, r.attempt, r.outcome, r.failureKind,"
        " r.mergeSha, r.reviewRoundCount, r.startedAt, r.endedAt"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.projectId = ? AND (? IS NULL OR r.startedAt >= ?)"
        " ORDER BY r.id", (project_id, since, since)).fetchall()


def _events(conn, project_id, since):
    return conn.execute(
        "SELECT runId, seq, at, kind, summary, payload FROM runEvents"
        " WHERE runId IN (SELECT id FROM runs"
        " WHERE projectId = ? AND (? IS NULL OR startedAt >= ?))"
        " ORDER BY runId, seq", (project_id, since, since)).fetchall()


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
                       "kind": redactor.text(kind), "summary": redactor.text(summary),
                       "payload": redactor.tree(parsed)})
        if kind == "agent_session" and isinstance(parsed, dict):
            field = {name: redactor.tree(parsed.get(name))
                     for name in ("session_id", "role", "route")}
            sessions.append({**field, "run_id": run_id,
                             "ticket_key": keys[run_id], "at": iso(at)})
    return {RUNS: runs, EVENTS: events, SESSIONS: sessions}


def _mode(path, mask):
    return path.stat().st_mode & 0o777 if path.is_file() else 0o666 & ~mask


def _staged(path, text, mode):
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".export-")
    try:
        os.fchmod(handle, mode)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    except BaseException:
        os.unlink(temporary)
        raise
    return temporary


def _refuse_the_store(directory, store_path):
    held = [Path(f"{path}{suffix}") for suffix in ("", "-wal", "-shm")
            for path in (store_path, os.path.realpath(store_path))]
    for name in (RUNS, EVENTS, SESSIONS, MANIFEST):
        path = directory / name
        if path.exists() and any(
                part.exists() and os.path.samefile(path, part) for part in held):
            raise SystemExit(f"[holo2] export refused: {path} is the store"
                             f" {store_path}; nothing was written")


def _replace(directory, texts, modes):
    staged = {}
    try:
        for name, text in texts.items():
            staged[name] = _staged(directory / name, text, modes[name])
        for name in list(staged):
            os.replace(staged[name], directory / name)
            del staged[name]
    finally:
        for temporary in staged.values():
            os.unlink(temporary)


def _publish(directory, texts, manifest):
    mask = os.umask(0)
    os.umask(mask)
    modes = {name: _mode(directory / name, mask) for name in (*texts, MANIFEST)}
    (directory / MANIFEST).unlink(missing_ok=True)
    _replace(directory, texts, modes)
    _replace(directory, {MANIFEST: manifest()}, modes)


@contextlib.contextmanager
def _one_export_at_a_time(directory):
    held = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(held, fcntl.LOCK_EX)
        yield
    finally:
        os.close(held)


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

    def manifest():
        return json.dumps({
            "project": project,
            "files": {name: len(rows) for name, rows in files.items()},
            "started_at": iso(started), "ended_at": iso(int(time.time() * 1000)),
            "since": since, "redaction_version": REDACTION_VERSION},
            sort_keys=True, indent=2) + "\n"
    with _one_export_at_a_time(directory):
        _refuse_the_store(directory, target.store_path)
        _publish(directory,
                 {name: _lines(rows) for name, rows in files.items()}, manifest)
    print(f"[holo2] exported {len(files[RUNS])} runs, {len(files[EVENTS])}"
          f" events and {len(files[SESSIONS])} agent sessions to {directory}")

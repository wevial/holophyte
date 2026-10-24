import json
import os
import signal
import socket
import subprocess

import store
from holophyte.loop.runs import open_store
from holophyte.scout.filing import ROW_ID_LIMIT
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_runs import no_store
from store.scouts import (
    ENDED,
    QUEUED,
    ScoutRefused,
    end_scout,
    record_scout_event,
    scout,
)

SCOUT_STOP_ACTION = "scout-stop"
VIA_DAEMON = "POST /actions/scout-stop"
REQUESTED = "scout_stop_requested"
NO_PROCESS = "no scout process was running"
BY_SIGNAL = "stopped by signal"
PS_TIMEOUT_S = 10


def refusal(body):
    value = body.get("scout")
    if type(value) is not int or value < 1:
        return "scout must be a positive integer"
    note = body.get("note")
    if not isinstance(note, str) or not note.strip():
        return "note must say why (non-blank text)"
    if body.get("author") is not None and not isinstance(body["author"], str):
        return "author must be text"
    return None


def stopped_by(author, note):
    return f"stopped by {author}: {note}"


def stop_reason(conn, scout_id):
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE scoutId = ? AND kind = ?"
        " ORDER BY seq DESC LIMIT 1", (scout_id, REQUESTED)).fetchone()
    if row is None:
        return BY_SIGNAL
    asked = json.loads(row[0])
    return stopped_by(asked["author"], asked["note"])


def runs_scout(pid, scout_id):
    ran = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    words = ran.stdout.split()
    return ran.returncode == 0 and any(
        words[at:at + 2] == ["--scout", str(scout_id)]
        for at in range(len(words) - 1))


def refused_phase(row, here):
    if row.phase in ENDED:
        return f"scout {row.id} is {row.phase}; nothing to stop"
    if row.phase != QUEUED and row.host not in (None, here):
        return (f"scout {row.id} runs on {row.host}, not on {here};"
                f" stop it from {row.host}'s daemon")
    return None


def requested(conn, scout_id, author, note, via):
    named = {"scout": scout_id, "pid": None}
    row = scout(conn, scout_id) if scout_id < ROW_ID_LIMIT else None
    if row is None:
        return 404, {"error": "no such scout", **named,
                     "detail": f"no scout {scout_id}"}
    declined = refused_phase(row, socket.gethostname())
    if declined is not None:
        return 200, {"ok": False, **named, "detail": declined}
    live = row.phase != QUEUED
    pid = row.pid if live and row.pid and runs_scout(row.pid, scout_id) else None
    record_scout_event(
        conn, scout_id, REQUESTED, f"stop asked by {author} ({via}): {note}",
        level="detail", payload=json.dumps(
            {"author": author, "note": note, "via": via, "pid": pid}))
    if pid is None:
        reason = stopped_by(author, note)
        end_scout(conn, scout_id, "stopped",
                  f"{reason}; {NO_PROCESS}" if live else reason)
        return 200, {"ok": True, **named, "detail": f"scout {scout_id} stopped"}
    return 200, {"ok": True, **named, "pid": pid,
                 "detail": f"stop sent to scout {scout_id}'s process {pid}"}


def stop(project, body, via=VIA_DAEMON):
    refused = refusal(body)
    if refused is not None:
        return 400, {"error": refused}
    if not project.store_path.exists():
        return 503, no_store(project)
    scout_id = body["scout"]
    conn = open_store(project)
    try:
        with store.transaction(conn):
            code, reply = requested(conn, scout_id, console_author(body),
                                    body["note"].strip(), via)
    except ScoutRefused as ended:
        return 200, {"ok": False, "scout": scout_id, "pid": None,
                     "detail": str(ended)}
    finally:
        conn.close()
    pid = reply.get("pid")
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return 200, {**reply, "ok": False,
                         "detail": f"scout {scout_id}'s process {pid} exited"
                                   " before the stop reached it"}
    return code, reply

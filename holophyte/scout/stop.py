import json
import os
import re
import signal
import socket
import subprocess
from pathlib import Path

import store
from holophyte.loop.runs import open_store
from holophyte.scout.answer import current_ask, stop_answer
from holophyte.scout.filing import ROW_ID_LIMIT
from holophyte.scout.follow_up import current_pass, stop_pass
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_runs import no_store
from store.scouts import (
    ANSWERING,
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


def stop_request(conn, scout_id):
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE scoutId = ? AND kind = ?"
        " ORDER BY seq DESC LIMIT 1", (scout_id, REQUESTED)).fetchone()
    return None if row is None else json.loads(row[0])


def stop_reason(conn, scout_id):
    asked = stop_request(conn, scout_id)
    if asked is None:
        return BY_SIGNAL
    return stopped_by(asked["author"], asked["note"])


def end_unrun(conn, scout_id, phase, ask, reason, author, note):
    if phase == ANSWERING:
        stop_answer(conn, scout_id, ask, reason, author, note)
        return f"scout {scout_id}'s answer stopped; it is done again"
    steered = current_pass(conn, scout_id)
    if steered is not None:
        stop_pass(conn, scout_id, steered, reason, author, note)
        return (f"scout {scout_id}'s pass {steered['pass']} stopped;"
                " it is done again")
    end_scout(conn, scout_id, "stopped", reason)
    return f"scout {scout_id} stopped"


def runs_scout(pid, scout_id, project_path):
    ran = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True, timeout=PS_TIMEOUT_S)
    named = re.search(rf"(?:^|\s)--scout {scout_id} (.+)$", ran.stdout.strip())
    return (ran.returncode == 0 and named is not None
            and Path(named.group(1)).resolve() == Path(project_path).resolve())


def refused_phase(row, here):
    if row.phase in ENDED:
        return f"scout {row.id} is {row.phase}; nothing to stop"
    if row.phase != QUEUED and row.host not in (None, here):
        return (f"scout {row.id} runs on {row.host}, not on {here};"
                f" stop it from {row.host}'s daemon")
    return None


def requested(conn, project, scout_id, author, note, via):
    named = {"scout": scout_id, "pid": None}
    row = scout(conn, scout_id) if scout_id < ROW_ID_LIMIT else None
    if row is None:
        return 404, {"error": "no such scout", **named,
                     "detail": f"no scout {scout_id}"}, None
    declined = refused_phase(row, socket.gethostname())
    if declined is not None:
        return 200, {"ok": False, **named, "detail": declined}, None
    live = row.phase != QUEUED
    running = live and row.pid and runs_scout(row.pid, scout_id, project.path)
    pid = row.pid if running else None
    ask = current_ask(conn, scout_id) if row.phase == ANSWERING else None
    steered = (current_pass(conn, scout_id) if row.phase != ANSWERING
               else None)
    record_scout_event(
        conn, scout_id, REQUESTED, f"stop asked by {author} ({via}): {note}",
        level="detail", payload=json.dumps({
            "author": author, "note": note, "via": via, "pid": pid,
            "ask": ask, "steer": steered and steered["message"]}))
    if pid is None:
        reason = stopped_by(author, note)
        said = end_unrun(conn, scout_id, row.phase, ask,
                         f"{reason}; {NO_PROCESS}" if live else reason,
                         author, note)
        return 200, {"ok": True, **named, "detail": said}, ask
    return 200, {"ok": True, **named, "pid": pid,
                 "detail": f"stop sent to scout {scout_id}'s process {pid}"}, ask


def stop(project, body, via=VIA_DAEMON):
    refused = refusal(body)
    if refused is not None:
        return 400, {"error": refused}
    if not project.store_path.exists():
        return 503, no_store(project)
    scout_id, author, note = body["scout"], console_author(body), body["note"].strip()
    conn = open_store(project)
    try:
        with store.transaction(conn):
            code, reply, ask = requested(conn, project, scout_id, author,
                                         note, via)
    except ScoutRefused as ended:
        return 200, {"ok": False, "scout": scout_id, "pid": None,
                     "detail": str(ended)}
    finally:
        conn.close()
    pid = reply.get("pid")
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return gone(project, scout_id, ask, author, note)
        except PermissionError as denied:
            return 200, {**reply, "ok": False,
                         "detail": f"scout {scout_id}'s process {pid} was not"
                                   f" signalled: {denied.strerror}"}
    return code, reply


def gone(project, scout_id, ask, author, note):
    named = {"scout": scout_id, "pid": None}
    reason = f"{stopped_by(author, note)}; {NO_PROCESS}"
    conn = open_store(project)
    try:
        with store.transaction(conn):
            phase = scout(conn, scout_id).phase
            if phase not in ENDED:
                said = end_unrun(conn, scout_id, phase, ask, reason, author,
                                 note)
                return 200, {"ok": True, **named, "detail": said}
    except ScoutRefused as moved:
        return 200, {"ok": False, **named,
                     "detail": f"scout {scout_id} was not stopped: {moved}"}
    finally:
        conn.close()
    return 200, {"ok": phase == "stopped", **named,
                 "detail": f"scout {scout_id} is {phase}; its process ended it"}

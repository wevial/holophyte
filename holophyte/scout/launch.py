import io
import json
import re
from pathlib import Path

import store
import store.read
from holophyte.admission import project_row
from holophyte.host.registry import Host, HostError, registered_at, settings
from holophyte.host.supervisor import SWEEP_EVENT
from holophyte.loop.runs import open_store
from holophyte.scout.scout_config import scout_config
from holophyte.serve.serve_findings import CONTRADICTION, stop_kind
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_runs import SQLITE_MAX_DIGITS, SQLITE_MAX_INT, no_store
from store.enums import ScoutKind
from store.scouts import ENDED, QUEUED, create_scout, record_scout_event, scout

SCOUT_ACTION = "scout"
KINDS = tuple(kind.value for kind in ScoutKind)
ROUTES = (("lead", "leads"), ("helper", "helpers"))
COUNTS = (("swarm", "swarm"), ("minutes", "budget_min"))
VIA_DAEMON = "POST /actions/scout"
RUN_NUMBER = re.compile(r"[1-9][0-9]*\Z")
SUBJECT_REFS = {
    "run": (RUN_NUMBER.match, "a positive integer"),
    "defect": (RUN_NUMBER.match, "a positive integer"),
    "ticket": (str.strip, "non-blank"),
    "pr": (lambda ref: ref.startswith(("http://", "https://")),
           "a URL starting http:// or https://"),
}


def refusal(body):
    ask = body.get("ask")
    if not isinstance(ask, str) or not ask.strip():
        return "ask must be non-blank text"
    if body.get("kind") not in KINDS:
        return f"kind must be one of {', '.join(KINDS)}"
    for field, _ in ROUTES:
        if field in body and not isinstance(body[field], str):
            return f"{field} must name a route"
    for field, _ in COUNTS:
        value = body.get(field)
        if field in body and (type(value) is not int or value < 1):
            return f"{field} must be a positive integer"
    return subject_refusal(body)


def subject_refusal(body):
    if "subject" not in body:
        return None
    subject = body["subject"]
    if not isinstance(subject, str) or ":" not in subject:
        return "subject must be KIND:REF text"
    kind, ref = subject.split(":", 1)
    if kind not in SUBJECT_REFS:
        return f"subject's kind must be one of {', '.join(SUBJECT_REFS)}"
    valid, shape = SUBJECT_REFS[kind]
    if not valid(ref):
        return f"subject's {kind} ref must be {shape}"
    return None


def held_run(conn, ref):
    if len(ref) > SQLITE_MAX_DIGITS or int(ref) > SQLITE_MAX_INT:
        return False
    return conn.execute("SELECT 1 FROM runs WHERE id = ?",
                        (int(ref),)).fetchone() is not None


def run_subject(conn, ref):
    if not held_run(conn, ref):
        return None, f"run {ref} is not in this project's store"
    return ref, None


def defect_subject(conn, ref):
    stops = conn.execute(
        "SELECT kind, summary FROM runEvents WHERE runId = ?"
        " AND kind IN (?, ?)", (int(ref), CONTRADICTION, SWEEP_EVENT)
    ).fetchall() if held_run(conn, ref) else []
    if not any(stop_kind(kind, summary) for kind, summary in stops):
        return None, f"run {ref} has no stop on its ticket's wording"
    return ref, None


def ticket_subject(conn, ref):
    ticket = store.read.ticket_by_identifier(conn, ref.strip())
    if ticket is None:
        return None, f"ticket {ref} is not in this project's store"
    return ticket.linearIdentifier, None


def pr_subject(conn, ref):
    if conn.execute("SELECT 1 FROM runs WHERE prUrl = ?",
                    (ref,)).fetchone() is None:
        return None, f"no run of this project opened {ref}"
    return ref, None


SUBJECTS = {"run": run_subject, "defect": defect_subject,
            "ticket": ticket_subject, "pr": pr_subject}


def resolved(conn, subject):
    if subject is None:
        return None, None
    kind, ref = subject.split(":", 1)
    stored, unheld = SUBJECTS[kind](conn, ref)
    return (None if unheld else (kind, stored)), unheld


def chosen(body, config):
    picked = {}
    for field, table in ROUTES:
        name = body.get(field)
        routes = getattr(config, table)
        if name is None:
            name = getattr(config, field)
        elif name not in routes:
            return None, (f"{field} {name!r} names no route in"
                          f" [scout.{table}]; the routes are"
                          f" {', '.join(sorted(routes)) or 'none'}")
        picked[field] = name
    for field, key in COUNTS:
        value = body.get(field)
        picked[field] = getattr(config, key) if value is None else value
    if picked["swarm"] > config.helper_turns:
        return None, (f"swarm {picked['swarm']} is more than helper_turns ="
                      f" {config.helper_turns}, the scout's helper total")
    return picked, None


def record(project, body, picked, config, via):
    conn = open_store(project)
    try:
        with store.transaction(conn):
            subject, unheld = resolved(conn, body.get("subject"))
            if unheld is not None:
                return None, unheld
            scout_id = create_scout(
                conn, project_row(conn, project), body["ask"], body["kind"],
                picked["minutes"], lead_route=picked["lead"],
                helper_route=picked["helper"], swarm=picked["swarm"],
                helper_turns=config.helper_turns, waves=config.waves,
                subject=subject)
            record_scout_event(
                conn, scout_id, "scout_queued",
                f"{body['kind']} scout queued by {console_author(body)} ({via})",
                level="detail",
                payload=json.dumps({"author": console_author(body), "via": via,
                                    "subject": body.get("subject")}))
    finally:
        conn.close()
    return scout_id, None


def entry_name(entry):
    return entry.name or str(entry.path)


def start(project, scout_id):
    from holophyte.scout.scout_sweep import start_queued
    out = io.StringIO()
    try:
        host = Host.locate()
        limit = settings(host).scout_max_parallel
        entry = registered_at(host, project)
        if entry is None:
            return False, (f"{host.path} does not register {project.path},"
                           " so no host sweep starts it")
        live, started = start_queued(host.projects(), limit, out,
                                     key=entry_name, home_dir=host.home)
    except (HostError, OSError) as bad:
        return False, str(bad).removeprefix("[holo2] ")
    if (Path(project.path).resolve(), scout_id) in started:
        return True, f"scout {scout_id} started"
    if live >= limit:
        return False, f"{live} of {limit} scout slots in use"
    said = [line.removeprefix("[holo2] ")
            for line in out.getvalue().splitlines()
            if line.startswith((f"[holo2] {entry_name(entry)}: ",
                                "[holo2] no scout starts"))]
    return False, said[-1] if said else "not started this pass"


def phase_of(project, scout_id):
    conn = open_store(project)
    try:
        row = scout(conn, scout_id)
    finally:
        conn.close()
    return row.phase, row.reason


def launch(project, body, via=VIA_DAEMON):
    refused = refusal(body)
    if refused is not None:
        return 400, {"error": refused}
    if not project.store_path.exists():
        return 503, no_store(project)
    config = scout_config(project)
    picked, declined = chosen(body, config)
    if declined is not None:
        return 200, {"ok": False, "detail": declined}
    scout_id, unheld = record(project, body, picked, config, via)
    if unheld is not None:
        return 200, {"ok": False, "detail": unheld}
    ran, said = start(project, scout_id)
    phase, reason = phase_of(project, scout_id)
    if phase == QUEUED:
        return 200, {"ok": True, "scout": scout_id, "queued": True,
                     "detail": f"scout {scout_id} queued: {said}"}
    if phase in ENDED:
        return 200, {"ok": False, "scout": scout_id, "queued": False,
                     "detail": f"scout {scout_id} {phase}: {reason}"}
    return 200, {"ok": True, "scout": scout_id, "queued": False,
                 "detail": said if ran else f"scout {scout_id} started"}

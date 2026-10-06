from __future__ import annotations

import re

import store
from holophyte.loop.runs import open_store
from holophyte.pr.merge_ready import readiness
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_runs import locate_run, no_store

MERGE_ACTION = "merge"
RUN_MERGE_PATH = re.compile(r"^/runs/([^/]+)/merge$")


def run_merge(project, segment):
    failed, run = locate_run(project, segment)
    if failed is not None:
        return failed
    return 200, readiness(project, run.id).answer()


def approval_note(body, ready):
    note = body.get("note")
    names = ", ".join(name for name, *_ in ready.facts)
    return (f"{console_author(body)} via the console: merge at head"
            f" {ready.head_sha}; {names} held"
            + (f"; {note.strip()}" if isinstance(note, str) and note.strip()
               else ""))


def merge_action(project, body):
    run_id = body.get("run")
    if type(run_id) is not int or not 0 < run_id < 2**63:
        return 400, {"error": "run must be a positive integer"}
    if not project.store_path.exists():
        return 503, no_store(project)
    ready = readiness(project, run_id)
    answer = ready.answer()
    refused = {"action": MERGE_ACTION, "ok": False, "run": run_id,
               "reason": answer["reason"], "detail": answer["detail"],
               "facts": answer["facts"]}
    if not answer["ready"]:
        return 200, refused
    conn = open_store(project)
    try:
        store.approve(conn, ready.park.ticket_id, approval_note(body, ready))
    except store.ApproveRefused as moved:
        return 200, {**refused, "reason": "not_parked", "detail": str(moved)}
    finally:
        conn.close()
    return 200, {"action": MERGE_ACTION, "ok": True, "run": run_id,
                 "ticket": answer["ticket"], "head_sha": ready.head_sha,
                 "detail": f"approved at {ready.head_sha}; the loop's next"
                           f" claim merges the candidate on {answer['pr_url']}"}

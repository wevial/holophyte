from __future__ import annotations

import re

import store
from holophyte.pr.merge_ready import REVIEW_BYPASSABLE, readiness
from holophyte.serve.serve_actions import action_store, written_on
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
    bypassed = ready.failing
    names = ", ".join(name for name, ok, *_ in ready.facts if ok)
    return (f"{console_author(body)} via the console: merge at head"
            f" {ready.head_sha}; {names} held"
            + (f"; bypassing the required review: {bypassed[2]}"
               if bypassed is not None else "")
            + (f"; {note.strip()}" if isinstance(note, str) and note.strip()
               else ""))


def merge_action(project, body):
    run_id = body.get("run")
    if type(run_id) is not int or not 0 < run_id < 2**63:
        return 400, {"error": "run must be a positive integer"}
    bypass = body.get("bypass_review", False)
    if type(bypass) is not bool:
        return 400, {"error": "bypass_review must be a boolean"}
    if not project.store_path.exists():
        return 503, no_store(project)
    ready = readiness(project, run_id)
    answer = ready.answer()
    refused = {"action": MERGE_ACTION, "ok": False, "run": run_id,
               "reason": answer["reason"], "detail": answer["detail"],
               "facts": answer["facts"]}
    bypassing = bypass and ready.reason == REVIEW_BYPASSABLE
    if not answer["ready"] and not bypassing:
        return 200, refused
    conn = action_store(project)
    try:
        store.approve(conn, ready.park.ticket_id, approval_note(body, ready),
                      run_id=run_id)
        recorded = written_on(conn, MERGE_ACTION)
    except store.ApproveRefused as moved:
        return 200, {**refused, "reason": "not_parked", "detail": str(moved)}
    finally:
        conn.close()
    bypassed = ", bypassing the required review" if bypassing else ""
    return 200, {"action": MERGE_ACTION, "ok": True, "run": run_id,
                 "recorded": recorded,
                 "ticket": answer["ticket"], "head_sha": ready.head_sha,
                 "detail": f"approved at {ready.head_sha}{bypassed}; the loop's"
                           f" next claim merges the candidate on {answer['pr_url']}"}

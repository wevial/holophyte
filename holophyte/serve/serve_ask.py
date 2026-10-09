from __future__ import annotations

import re

import store.read
from holophyte.serve.serve_actions import action_store, written_on
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_runs import locate_run, no_store
from store import console_asks

ASK_ACTION = "ask"
RUN_ASKS_PATH = re.compile(r"^/runs/([^/]+)/asks$")


def run_asks(project, segment):
    failed, run = locate_run(project, segment)
    if failed is not None:
        return failed
    conn = store.read.open_readonly(project.store_path)
    try:
        asks = console_asks.asks(conn, run.id)
    finally:
        conn.close()
    return 200, {"run": run.id, "ticket": run.linearIdentifier,
                 "pr_url": run.prUrl, "asks": asks}


def ask_action(project, body):
    run_id = body.get("run")
    if type(run_id) is not int or not 0 < run_id < 2**63:
        return 400, {"error": "run must be a positive integer"}
    if not project.store_path.exists():
        return 503, no_store(project)
    conn = action_store(project)
    try:
        run = conn.execute(
            "SELECT t.linearIdentifier, r.prUrl FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId WHERE r.id = ?",
            (run_id,)).fetchone()
        if run is None:
            return 404, {"error": "no such run", "run": run_id}
        refused = {"action": ASK_ACTION, "ok": False, "run": run_id}
        try:
            event_id = console_asks.ask(conn, run_id, body.get("question"),
                                        console_author(body))
            recorded = written_on(conn, ASK_ACTION)
        except console_asks.AskRefused as refusal:
            return 200, {**refused, "reason": refusal.reason,
                         "detail": str(refusal)}
        except store.ApproveRefused as moved:
            return 200, {**refused, "reason": "not_parked", "detail": str(moved)}
    finally:
        conn.close()
    ticket, pr_url = run
    return 200, {"action": ASK_ACTION, "ok": True, "run": run_id,
                 "ticket": ticket, "event_id": event_id, "recorded": recorded,
                 "detail": f"console ask event {event_id} recorded; the loop's"
                           f" next claim answers it on {pr_url}"}

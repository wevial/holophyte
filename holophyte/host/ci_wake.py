import json
from time import time

import store
import store.read
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.pr import pr_status
from holophyte.pr.pr_ready import held_for_frontend, record_hold

FINISHED = ("success", "failure")
EXPIRED = object()


def wake_reason(target, conn, ticket, pull, status, latest_s):
    if ticket.parkKind == "route":
        return _route_retry(target, conn, ticket)
    if ticket.parkKind != "ci":
        return None
    merge = merge_config(target)
    now = time()
    checks = status.checks
    if ticket.prSeenChecks in ("pending", None) and checks in FINISHED:
        return f"checks on {pull.url} finished {checks}"
    if checks == "success" and latest_s is not None \
            and now - latest_s >= merge.pr_quiet_sec:
        return (f"{pull.url} is green and quiet for"
                f" {merge.pr_quiet_sec}s ([merge] pr_quiet_sec)")
    if checks == "pending" and ticket.askedMs is not None \
            and now * 1000 - ticket.askedMs > merge.check_wait_sec * 1000:
        _expire(conn, ticket, pull, f"pending checks exceeded"
                f" {merge.check_wait_sec}s on the pull request",
                held_for_frontend(target, status))
        return EXPIRED
    return None


def _route_retry(target, conn, ticket):
    retry_ms = merge_config(target).route_retry_sec * 1000
    parked_ms, = conn.execute("SELECT lastHeartbeat FROM runs WHERE id = ?",
                              (ticket.runId,)).fetchone()
    if time() * 1000 - (parked_ms or 0) >= retry_ms:
        return "retrying the adjudicator after a route outage"
    return None


def _expire(conn, ticket, pull, why, held):
    from holophyte.babysit.babysitter import open_threads_question
    with store.transaction(conn):
        now = store.read.ticket_by_id(conn, ticket.id)
        parked = conn.execute(
            "SELECT candidateSha FROM runs WHERE id = ? AND parkKind = 'ci' AND phase ="
            " 'awaiting_merge_approval' AND endedAt IS NULL",
            (ticket.runId,)).fetchone()
        if now is None or now.status != "blocked_on_operator" \
                or now.activeRunId is not None \
                or now.lastRunId != ticket.runId or parked is None:
            return
        store.record_event(conn, ticket.runId, "ci_expired", why)
        if held:
            record_hold(conn, ticket.runId, pull, parked[0])
        store.set_question(conn, ticket.id,
                           open_threads_question(pull, why, ()),
                           park_kind="pull_request")
    print(f"[holo2] {ticket.linearIdentifier}: {why}; the run waits on a"
          " human")


def parked_approval(conn, run_id):
    return conn.execute(
        "SELECT e.payload, r.candidateSha FROM runEvents e JOIN runs r"
        " ON r.id = e.runId WHERE e.runId = ? AND e.kind = 'approval_parked'"
        " ORDER BY e.id DESC LIMIT 1", (run_id,)).fetchone()


def approval_requests(target, conn, run_id, pull):
    if target is None or parked_approval(conn, run_id) is None:
        return None
    try:
        return pr_status.unanswered_requests(target, pull)
    except InfraFailure:
        return None


def carry_approval(conn, run_id, status, requests=None):
    parked = parked_approval(conn, run_id)
    if parked is None:
        return
    sha, candidate = json.loads(parked[0])["sha"], parked[1]
    open_threads = (status.threads if status.open_threads is None
                    else status.open_threads)
    if candidate != sha:
        why = (f"the candidate was re-pointed from {sha[:12]} to"
               f" {(candidate or '?')[:12]}")
    elif status.head_sha is None:
        why = "the pull request's head could not be read"
    elif status.head_sha != sha:
        why = f"the head moved from {sha[:12]} to {status.head_sha[:12]}"
    elif open_threads or open_threads is None or status.open_threads_floor:
        why = "a review thread is open or its count is unknown"
    elif requests is None:
        why = "the conversation requests could not be read"
    elif requests:
        why = f"{requests} conversation or review request(s) wait for an answer"
    elif status.review == "changes_requested":
        why = "a review requested changes"
    else:
        store.record_event(conn, run_id, "approval_carried",
                           f"the approval of {sha} carries through the park")
        return
    store.record_event(conn, run_id, "approval_dropped",
                       f"the approval of {sha} is not carried: {why}; the run"
                       " parks for a fresh approval")

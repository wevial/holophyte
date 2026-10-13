from time import time

import store
import store.read
from holophyte.config.config_tables import merge_config
from holophyte.pr.pr_ready import held_for_frontend, record_hold

FINISHED = ("success", "failure")
EXPIRED = object()


def wake_reason(target, conn, ticket, pull, status, latest_s):
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


def carry_approval(conn, run_id, status):
    parked = conn.execute(
        "SELECT r.candidateSha FROM runs r WHERE r.id = ? AND EXISTS (SELECT 1"
        " FROM runEvents e WHERE e.runId = r.id AND e.kind = 'approval_parked')",
        (run_id,)).fetchone()
    if parked is None:
        return
    sha = parked[0]
    open_threads = (status.threads if status.open_threads is None
                    else status.open_threads)
    if status.head_sha not in (None, sha):
        why = f"the head moved from {sha[:12]} to {status.head_sha[:12]}"
    elif open_threads or open_threads is None or status.open_threads_floor:
        why = "a review thread is open or its count is unknown"
    elif status.review == "changes_requested":
        why = "a review requested changes"
    else:
        store.record_event(conn, run_id, "approval_carried",
                           f"the approval of {sha} carries through the park")
        return
    store.record_event(conn, run_id, "approval_dropped",
                       f"the approval of {sha} is not carried: {why}; the run"
                       " parks for a fresh approval")

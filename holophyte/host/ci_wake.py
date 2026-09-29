"""When a run parked `ci` goes back to a worker, or to a human instead."""
from time import time

import store
import store.read
from holophyte.config.config_tables import merge_config

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
                f" {merge.check_wait_sec}s on the pull request")
        return EXPIRED
    return None


def _expire(conn, ticket, pull, why):
    from holophyte.babysit.babysitter import open_threads_question
    with store.transaction(conn):
        now = store.read.ticket_by_id(conn, ticket.id)
        parked = conn.execute(
            "SELECT 1 FROM runs WHERE id = ? AND parkKind = 'ci' AND phase ="
            " 'awaiting_merge_approval' AND endedAt IS NULL",
            (ticket.runId,)).fetchone()
        if now is None or now.status != "blocked_on_operator" \
                or now.activeRunId is not None \
                or now.lastRunId != ticket.runId or parked is None:
            return
        store.record_event(conn, ticket.runId, "ci_expired", why)
        store.set_question(conn, ticket.id,
                           open_threads_question(pull, why, ()),
                           park_kind="pull_request")
    print(f"[holo2] {ticket.linearIdentifier}: {why}; the run waits on a"
          " human")

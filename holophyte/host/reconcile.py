import json
from datetime import datetime, timezone
from time import time

import store
import store.read
import store.tickets
from holophyte import deadline
from holophyte.board.projection import (
    ledger,
    mirror_push,
    post_ledger_comment,
    refresh_board_states,
    release_lease_label,
)
from holophyte.config.config_tables import merge_config
from holophyte.config.project import worktree_path
from holophyte.host import ci_wake
from holophyte.loop.gates import sh
from holophyte.pr import pr_activity, pr_status
from holophyte.review.findings import refresh_findings
from holophyte.story import story_claim

RECONCILED_STATUS = {"completed": "merged", "canceled": "abandoned"}
RECONCILE_TRIGGER = {"completed": "linear_completed",
                     "canceled": "linear_cancelled"}


def _reconcile_at_startup(target, conn, project, provider):
    from holophyte.admission import held_line
    if held_line(conn, project):
        return
    # GitHub first: a board Done must not walk a merged ticket past its parked run.
    _reconcile_pull_requests(target, conn, project, provider)
    _reconcile_mirror(conn, project, provider, target)


def _reconcile_mirror(conn, project, provider, target=None):
    refresh_board_states(conn, project, provider)
    tickets = []
    for ticket in store.read.open_tickets(conn, project):
        if ticket.activeRunId is not None:
            continue
        if ticket.status == "blocked_on_operator" \
                and _parked_pull_request(conn, ticket.id) is not None:
            deadline.check(f"{ticket.linearIdentifier}'s cancel check")
            if _close_canceled(target, conn, provider, ticket.id):
                continue
            # Only GitHub's answer carries the merge sha the parked run closes with.
            print(f"[holo2] reconcile left {ticket.linearIdentifier} to its"
                  " pull request: the run parked on it is GitHub's to close")
            continue
        tickets.append(ticket)
    if not tickets:
        return
    deadline.check("the board's closed identifiers")
    try:
        closed = provider.closed_identifiers([t.linearIdentifier for t in tickets])
    except Exception as e:
        print(f"[holo2] reconcile skipped: the board could not be asked which"
              f" mirrored tickets it has closed ({e})")
        return
    for ticket in tickets:
        state = closed.get(ticket.linearIdentifier)
        if state not in RECONCILED_STATUS or story_claim.held_open(
                conn, ticket.id, ticket.linearIdentifier, state):
            continue
        to_status = RECONCILED_STATUS[state]
        line = (f"[holo2] reconciled {ticket.linearIdentifier}:"
                f" {ticket.status} -> {to_status} (Linear {state})")
        with store.transaction(conn):
            # Re-read under the lock: the row may have moved while the board was asked.
            now = store.read.ticket_by_id(conn, ticket.id)
            if now is None or now.status != ticket.status \
                    or now.activeRunId is not None:
                print(f"[holo2] reconcile left {ticket.linearIdentifier}"
                      f" alone: it moved while the board was asked")
                continue
            if now.lastRunId is not None:
                store.record_intervention(
                    conn, now.lastRunId, "reconcile",
                    f"Linear holds {ticket.linearIdentifier} {state};"
                    f" mirror walked {ticket.status} -> {to_status}",
                    source="supervisor", trigger=RECONCILE_TRIGGER[state])
            else:
                line += "; no run to record the intervention against"
            story_claim.walk_closed(conn, ticket.id, ticket.linearIdentifier,
                                    to_status)
        print(line)
        if to_status == "abandoned" and target is not None:
            _retire_abandoned(target, conn, now)


def _close_canceled(target, conn, provider, ticket_id):
    # Asked again first: a merge since this pass's read must land with its sha.
    verdict = _ask_before_cancel(target, conn, provider, ticket_id)
    if verdict != "cancel":
        return verdict == "landed"
    with store.transaction(conn):
        ticket = store.read.ticket_by_id(conn, ticket_id)
        if ticket is None or ticket.boardState != "Canceled" \
                or ticket.status != "blocked_on_operator" \
                or ticket.activeRunId is not None:
            return False
        identifier, run_id = ticket.linearIdentifier, ticket.lastRunId
        url = _parked_pull_request(conn, ticket_id)
        parked = _parked_phase(conn, run_id) is not None
        if parked:
            store.record_intervention(
                conn, run_id, "close_out",
                f"{identifier} canceled on the board; run {run_id} ended"
                f" abandoned and {url} left open",
                source="supervisor", trigger="linear_cancelled")
            store.release(conn, run_id, "abandoned",
                          f"canceled on the board; pull request {url} left"
                          " open")
        else:
            store.record_intervention(
                conn, run_id, "reconcile",
                f"Linear holds {identifier} canceled; mirror walked"
                " blocked_on_operator -> abandoned",
                source="supervisor", trigger="linear_cancelled")
        story_claim.walk_closed(conn, ticket_id, identifier, "abandoned")
    if parked:
        if target is not None:
            from holophyte.board.projection import release_lease_label
            release_lease_label(target, conn, ticket_id, provider, run_id)
        print(f"[holo2] reconciled {identifier}: canceled on the board; run"
              f" {run_id} ended abandoned, {url} left open on GitHub")
    else:
        print(f"[holo2] reconciled {identifier}: blocked_on_operator ->"
              " abandoned (canceled on the board)")
    return True


def _ask_before_cancel(target, conn, provider, ticket_id):
    ticket = next((t for t in store.read.blocked_tickets(conn)
                   if t.id == ticket_id), None)
    if ticket is None or ticket.boardState != "Canceled" \
            or _parked_phase(conn, ticket.runId) is None:
        return "cancel"
    pull = pr_status.parse_pr_url(ticket.prUrl)
    if pull is None:
        return "cancel"
    if target is None or _budget_low():
        return "wait"
    try:
        status = pr_status.pull_status(target, pull)
    except Exception as e:  # noqa: BLE001 - any transport failure
        print(f"[holo2] {ticket.linearIdentifier}: {pull.url} could not be"
              f" read before its cancel ({e}); the run stays parked")
        return "wait"
    GITHUB_BUDGET.remember(status)
    if status.merged:
        _land_github_merge(target, conn, provider, ticket, pull, status)
        return "landed"
    if status.closed:
        _note_closed_pr(target, conn, provider, ticket, pull, status)
    return "cancel"


def _retire_abandoned(target, conn, ticket):
    from holophyte.loop.claim import retire_worktree

    run = store.read.run_detail(conn, ticket.lastRunId)
    if run is None or not run.branch:
        return
    reason = retire_worktree(target, run.branch)
    if reason:
        line = (f"[holo2] {ticket.linearIdentifier}: kept worktree"
                f" {worktree_path(target, run.branch)}: {reason}")
        print(line)
        store.record_event(conn, run.id, "worktree_retirement_refused", line)


PR_CLOSED_QUESTION = "rejected: "


def _reconcile_pull_requests(target, conn, project, provider,
                             failed_asked=None):
    from holophyte.admission import held_line
    sent = set()
    if held_line(conn, project) or _budget_low():
        return sent
    poll_ms = merge_config(target).pr_poll_sec * 1000
    for ticket in store.read.blocked_tickets(conn, project):
        if not ticket.prUrl or ticket.runId is None:
            continue
        pull = pr_status.parse_pr_url(ticket.prUrl)
        if pull is None or _parked_phase(conn, ticket.runId) is None:
            continue
        deadline.check(f"{ticket.linearIdentifier}'s pull request read")
        try:
            status = pr_status.pull_status(target, pull)
        except Exception as e:  # noqa: BLE001 - any transport failure
            print(f"[holo2] {ticket.linearIdentifier}: {pull.url} could not"
                  f" be read ({e}); the run stays parked")
            continue
        GITHUB_BUDGET.remember(status)
        low = _budget_low()
        if status.merged:
            _land_github_merge(target, conn, provider, ticket, pull, status)
        elif status.closed:
            _note_closed_pr(target, conn, provider, ticket, pull, status)
        # A babysit round is many reads and writes: not on a budget already low.
        elif not low:
            reason = ci_wake.wake_reason(target, conn, ticket, pull, status,
                                         _iso_epoch(_seen(status)[0]))
            issue = None if reason is ci_wake.EXPIRED else _rebabysit(
                conn, ticket, pull, status, poll_ms, reason)
            if issue is not None:
                sent.add(issue)
        if low:
            return sent
    if failed_asked is None:
        failed_asked = _FAILED_ASKED.setdefault(str(target.store_path), {})
    _close_failed_pull_requests(target, conn, project, provider, poll_ms,
                                sent, failed_asked)
    return sent


CLOSABLE_OUTCOMES = ("rejected", "failed", "abandoned", "killed")
FAILED_PR_OUTCOMES = ("failed", "abandoned", "killed")

# This process's throttle; the host sweep, a new process each run, passes its own.
_FAILED_ASKED = {}


class CloseRefused(Exception):
    pass


def close_out_landed(target, conn, ticket_id, message, provider, run_id=None):
    with store.transaction(conn):
        ticket = store.read.ticket_by_id(conn, ticket_id)
        last = _closable_run(conn, ticket)
        if run_id is not None and last != run_id:
            raise CloseRefused("the last run moved while GitHub was asked")
        store.record_intervention(conn, last, "close_out", message)
        ledger_text = conn.execute(
            "SELECT text FROM ledger WHERE runId = ? ORDER BY id DESC LIMIT 1",
            (last,)).fetchone()[0]
        store.set_question(conn, ticket_id, None)
        store.walk_ticket(conn, ticket_id, "merged")
        store.clear_merge_sha(conn, last)
    release_lease_label(target, conn, ticket_id, provider, last)
    mirror_push(conn, ticket_id, provider)
    post_ledger_comment(ticket.linearIssueId, ledger_text, provider)
    return last


def _closable_run(conn, ticket):
    if ticket.status == "merged":
        raise CloseRefused("already merged")
    live = conn.execute(
        "SELECT id FROM runs WHERE ticketId = ? AND endedAt IS NULL",
        (ticket.id,)).fetchone()
    if ticket.activeRunId is not None or live is not None:
        raise CloseRefused("has a live run")
    run = conn.execute("SELECT outcome, endedAt FROM runs WHERE id = ?",
                       (ticket.lastRunId,)).fetchone()
    if run is None or run[1] is None or run[0] not in CLOSABLE_OUTCOMES:
        raise CloseRefused("last run must have ended rejected, failed,"
                           " abandoned or killed")
    return ticket.lastRunId


def _failed_pull_requests(conn, project):
    marks = ", ".join("?" * len(FAILED_PR_OUTCOMES))
    return conn.execute(
        "SELECT t.id, t.linearIdentifier, t.linearIssueId, r.id, r.prUrl"
        " FROM tickets t"
        " JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.projectId = ? AND t.status NOT IN ('merged', 'abandoned')"
        " AND t.activeRunId IS NULL AND r.endedAt IS NOT NULL"
        f" AND r.outcome IN ({marks}) AND r.prUrl IS NOT NULL ORDER BY t.id",
        (project, *FAILED_PR_OUTCOMES)).fetchall()


def _close_failed_pull_requests(target, conn, project, provider, poll_ms,
                                sent, asked):
    for ticket_id, identifier, issue, run_id, url in _failed_pull_requests(
            conn, project):
        if issue in sent:
            continue
        pull = pr_status.parse_pr_url(url)
        now_ms = time() * 1000
        last = asked.get(run_id)
        if pull is None or (last is not None and now_ms - last < poll_ms):
            continue
        deadline.check(f"{identifier}'s failed run pull request read")
        asked[run_id] = now_ms
        try:
            status = pr_status.pull_status(target, pull)
        except deadline.CallRefused as refused:
            # Never sent, so never read: the throttle keeps the last read made.
            if last is None:
                asked.pop(run_id, None)
            else:
                asked[run_id] = last
            print(f"[holo2] {identifier}: {pull.url} not read ({refused});"
                  " the ticket stays open")
            continue
        except Exception as e:  # noqa: BLE001 - any transport failure
            print(f"[holo2] {identifier}: {pull.url} could not be read ({e});"
                  " the ticket stays open")
            continue
        GITHUB_BUDGET.remember(status)
        if status.merged:
            _close_merged_after_failure(target, conn, provider, ticket_id,
                                        identifier, run_id, pull, status)
        if _budget_low():
            return


def _close_merged_after_failure(target, conn, provider, ticket_id,
                                identifier, run_id, pull, status):
    who = status.merged_by or "someone"
    short = status.merge_sha[:12] if status.merge_sha else "an unrecorded sha"
    message = (f"Closed: {pull.url} merged on GitHub by {who} as {short}"
               " after the run ended; no factory merge occurred.")
    try:
        close_out_landed(target, conn, ticket_id, message, provider, run_id)
    except CloseRefused as refused:
        print(f"[holo2] {identifier}: {pull.url} is merged on GitHub but the"
              f" ticket cannot be closed out ({refused}); left alone")
        return
    print(f"[holo2] {identifier}: {pull.url} was merged on GitHub by {who}"
          f" as {short}; run {run_id} kept its outcome and the ticket is"
          " closed out as merged")


# 5000 points an hour, one per read: the floor keeps the babysitter's rounds funded.
RATE_FLOOR = 500


class GitHubBudget:
    def __init__(self):
        self.remaining = None
        self.reset_at = None

    def remember(self, status):
        if status.rate_remaining is not None:
            self.remaining = status.rate_remaining
            self.reset_at = status.rate_reset

    def low(self, now=None):
        if self.remaining is None or self.remaining >= RATE_FLOOR:
            return False
        reset = _iso_epoch(self.reset_at)
        if reset is None or (time() if now is None else now) >= reset:
            self.remaining = self.reset_at = None
            return False
        return True


GITHUB_BUDGET = GitHubBudget()


def _iso_epoch(text):
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _budget_low():
    if not GITHUB_BUDGET.low():
        return False
    print(f"[holo2] GitHub's GraphQL budget is down to"
          f" {GITHUB_BUDGET.remaining} points; no parked pull request is read"
          f" until it resets at {GITHUB_BUDGET.reset_at}")
    return True


def _seen(status):
    at = max([status.updated_at or "", *(item[1] for item in status.activity)])
    return (at or None, status.threads, status.checks, status.review,
            status.title)


def _rebabysit(conn, ticket, pull, status, poll_ms, reason=None):
    identifier, run_id = ticket.linearIdentifier, ticket.runId
    row = conn.execute("SELECT prSeenAt, prSeenThreads, lastHeartbeat,"
                       " (SELECT linearIssueId FROM tickets WHERE id = ?)"
                       " FROM runs WHERE id = ?",
                       (ticket.id, run_id)).fetchone()
    if row is None or status.updated_at is None:
        return None
    seen_at, seen_threads, parked_ms, issue = row
    mark = _seen(status)
    if seen_at is None and reason is None:
        store.record_pr_seen(conn, run_id, mark, parked_only=True)
        pr_activity.record_commits(conn, run_id, status)
        return None
    arrived = (pr_activity.arrived(conn, run_id, status, seen_at)
               if seen_at is not None else [])
    # Only new authored content wakes a park: a timestamp bump never pays a pass.
    if not arrived and reason is None:
        store.record_pr_seen(conn, run_id, mark, parked_only=True,
                             facts_only=True)
        pr_activity.break_empty_wakes(conn, ticket)
        return None
    why = f"{pull.url} has new review activity" if arrived else reason
    waited_ms = int(time() * 1000) - (parked_ms or 0)
    if waited_ms < poll_ms:
        if reason is None:
            store.record_pr_seen(conn, run_id, mark, parked_only=True,
                                 facts_only=True)
        print(f"[holo2] {identifier}: {why}; the next babysit round waits"
              f" {-(-(poll_ms - waited_ms) // 1000)}s ([merge] pr_poll_sec)")
        return None
    threads = "?" if status.threads is None else status.threads
    note = (f"new review activity on {pull.url}: "
            f"{', '.join(sorted({item[0] for item in arrived}))}; updated"
            f" {status.updated_at} (last seen {seen_at}), {threads} review"
            f" threads (last seen {seen_threads})") if arrived else reason
    try:
        with store.transaction(conn):
            store.record_pr_seen(conn, run_id, mark)
            pr_activity.record_commits(conn, run_id, status)
            if arrived:
                store.record_event(conn, run_id, "pr_wake", json.dumps(arrived))
            if reason:
                store.record_event(conn, run_id, "ci_wake", reason)
            store.babysit(conn, ticket.id, note, source="supervisor")
    except store.ApproveRefused as refused:
        print(f"[holo2] {identifier}: {why} but the ticket moved while GitHub"
              f" was asked ({refused}); left alone")
        return None
    detail = (f" (updated {status.updated_at}, {threads} review threads)"
              if arrived else "")
    print(f"[holo2] {identifier}: {why}{detail}; run {run_id} sent back to"
          " the babysitter")
    return issue


def _parked_phase(conn, run_id):
    row = conn.execute("SELECT branch, phase FROM runs WHERE id = ?",
                       (run_id,)).fetchone()
    if row is None or row[1] != "awaiting_merge_approval":
        return None
    return row


def _parked_pull_request(conn, ticket_id):
    row = conn.execute(
        "SELECT r.prUrl FROM tickets t JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.id = ? AND r.phase IN ('awaiting_merge_approval', 'rejected')"
        " AND r.prUrl IS NOT NULL", (ticket_id,)).fetchone()
    return None if row is None else row[0]


def _land_github_merge(target, conn, provider, ticket, pull, status):
    identifier, run_id = ticket.linearIdentifier, ticket.runId
    who = status.merged_by or "someone"
    sha = status.merge_sha
    short = sha[:12] if sha else "an unrecorded sha"
    with store.transaction(conn):
        now = store.read.ticket_by_id(conn, ticket.id)
        parked = _parked_phase(conn, run_id)
        if now is None or now.status != "blocked_on_operator" \
                or now.activeRunId is not None or now.lastRunId != run_id \
                or parked is None:
            print(f"[holo2] {identifier}: {pull.url} is merged on GitHub but"
                  " the ticket moved while it was asked; left alone")
            return
        branch = parked[0]
        store.record_intervention(
            conn, run_id, "approve",
            f"{pull.url} merged on GitHub by {who} as {short}; the run is"
            " closed out as merged", source="human", trigger="manual")
        store.release(conn, run_id, "merged", merge_sha=sha)
        store.set_question(conn, ticket.id, None)
        store.tickets.walk_ticket(conn, ticket.id, "merged")
    mirror_push(conn, ticket.id, provider)
    ledger(conn, run_id, identifier, "merge",
           f"MERGED through {pull.url} as {sha} by {who} on GitHub (branch"
           f" {branch} deleted locally; local main not moved).", provider)
    if branch:
        try:
            sh(["git", "worktree", "remove", "--force",
                str(worktree_path(target, branch))], target.path)
            sh(["git", "branch", "-D", branch], target.path)
        except RuntimeError as e:
            print(f"[holo2] post-merge cleanup left debris: {e}")
    refresh_findings(target, conn)
    print(f"[holo2] {identifier}: {pull.url} was merged on GitHub by {who}"
          f" as {short}; run {run_id} closed out as merged")


def _note_closed_pr(target, conn, provider, ticket, pull, status):
    with store.transaction(conn):
        current = store.read.ticket_by_id(conn, ticket.id)
        if current is None or current.status != "blocked_on_operator" \
                or current.lastRunId != ticket.runId \
                or current.activeRunId is not None \
                or _parked_phase(conn, ticket.runId) is None:
            return
        branch, sha = conn.execute(
            "SELECT branch, candidateSha FROM runs WHERE id = ?",
            (ticket.runId,)).fetchone()
        _reject_pr(conn, ticket.runId, pull, status.closed_by, branch, sha)
    from holophyte.board.projection import release_lease_label
    release_lease_label(target, conn, ticket.id, provider, ticket.runId)


def _reject_pr(conn, run_id, pull, who, branch, sha):
    who = who or "unknown"
    question = f"{PR_CLOSED_QUESTION}{pull.url} closed by {who}"
    reason = (f"closed on GitHub by {who} without merging;"
              f" branch {branch} preserved at {sha}")
    with store.transaction(conn):
        store.record_event(conn, run_id, "pull_request", reason)
        store.release(conn, run_id, "rejected", reason)
        ticket_id = store.read.run_snapshot(conn, run_id).ticketId
        store.walk_ticket(conn, ticket_id, "blocked_on_operator")
        store.set_question(conn, ticket_id, question,
                           park_kind="pull_request_closed")
        store.set_pull_request(conn, run_id, pull.url, sha)
    print(f"[holo2] {question}; {reason}")


def _pr_seen(target, pull, conn=None, run_id=None):
    try:
        status = pr_status.pull_status(target, pull)
    except Exception as e:  # noqa: BLE001 - any transport failure
        print(f"[holo2] {pull.url} could not be read after the pass ({e});"
              " the park records no activity mark")
        return None
    GITHUB_BUDGET.remember(status)
    if conn is not None and run_id is not None:
        pr_activity.record_pass(conn, run_id, status)
        pr_activity.record_commits(conn, run_id, status)
    return _seen(status)

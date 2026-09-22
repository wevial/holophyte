"""The startup reconciles and the GitHub read budget they spend from.

`_reconcile_at_startup()` runs the two repairs before the first claim, and
the serial pass and the scheduler tick run the first again each time around
(KO-359): `_reconcile_pull_requests()` asks GitHub about every parked run's
pull request -- landing what a person merged (`_land_github_merge()`),
naming what was closed without merging (`_note_closed_pr()`), and sending
what moved past the mark the park recorded back to the babysitter
(`_rebabysit()`) -- and `_reconcile_mirror()` walks the tickets Linear has
since closed to their terminal status. The supervisor's pass calls the same
pull-request reconcile for the projects no live loop is working (KO-372),
and `_park_on_pr()`'s activity mark is `_pr_seen()` here.

`GitHubBudget` is the one GraphQL budget the process's token has; under
`RATE_FLOOR` points the reads stop until the reset GitHub named.

Moved verbatim out of `holophyte/loop.py` (KO-387) -- `_pr_seen()` out of
`holophyte/pullrequest.py` -- and the loop imports back the names its
remaining call sites use.
"""
import json
from datetime import datetime, timezone
from time import time

import store
import store.read
import store.tickets
from holophyte import pr_activity, pr_status
from holophyte.board import ledger, mirror_push, refresh_board_states
from holophyte.config_tables import merge_config
from holophyte.findings import refresh_findings
from holophyte.gates import sh
from holophyte.target import worktree_path

# The mirror's status for each closed Linear state type: a ticket finished
# elsewhere is `merged`, one cancelled is `abandoned`.
RECONCILED_STATUS = {"completed": "merged", "canceled": "abandoned"}
RECONCILE_TRIGGER = {"completed": "linear_completed",
                     "canceled": "linear_cancelled"}


def _reconcile_at_startup(target, conn, project, provider):
    """The two startup reconciles, GitHub before the board (KO-359 review).

    A person who merged a parked pull request on GitHub may have moved its
    ticket to Done on Linear as well. Asked first, the mirror reconcile
    would see Done, walk the ticket `merged` itself and take it out of the
    pull request reconcile's `blocked_on_operator` read: the run stayed
    parked with no outcome and no `mergeSha`, and Shipped never showed it.
    So the parked pull requests are read first and a merged one ships its
    run; the mirror repair then finds that ticket already `merged` and
    walks only the rest. Order alone is not enough: had GitHub failed on
    that first read, the mirror repair would still have seen Done and
    walked the ticket `merged` around its parked run, which no later pass
    could reach -- the pull request reconcile reads `blocked_on_operator`
    tickets only. So the mirror repair also leaves every ticket whose
    newest run is parked on a pull request to this reconcile, whatever
    the board says, and the next pass asks GitHub again.
    """
    from holophyte.admission import held_line
    if held_line(conn, project):
        return
    _reconcile_pull_requests(target, conn, project, provider)
    _reconcile_mirror(conn, project, provider, target)


def _reconcile_mirror(conn, project, provider, target=None):
    """Walk the mirrored tickets Linear has since closed to their terminal
    status, one printed line each; nothing is written to Linear. Board state
    names refresh first for all open mirrors, including live and parked runs.

    The mirror is written when the loop claims a ticket and hears nothing
    when Linear later closes it elsewhere -- a ticket another target
    finished, or one the operator cancelled -- so it sat on the board as
    `ready` or `needs_spec` for good (KO-217, KO-137 and KO-138 on the
    daemon's board). Startup only, after the read-only sweep and the pull
    request reconcile (`_reconcile_at_startup()`): the five open statuses
    are read through the store for this project only (the provider knows
    one team, and another project's tickets are that project's loop to
    reconcile), a ticket with an active run is left to
    that run, and the provider is asked about the rest in one call. A
    closed one is walked along §3 edges (`walk_ticket`) with a `reconcile`
    intervention row on its most recent run first, in the same
    transaction -- record before acting. The row is re-read under that
    transaction's lock and must still be where the open read saw it, with
    no run: another process on the same store can claim or move a ticket
    while the provider is being asked, and a verdict on the stale read
    would mark a ticket merged under a live run. A ticket that never ran
    has no run to carry the row (`interventions.runId` is NOT NULL), so
    its printed line is its only record and says so. A ticket parked on a
    pull request is left to the pull request reconcile whatever the board
    says (KO-359 review): a Done there means a person merged the pull
    request, and only GitHub's answer closes the parked run out with its
    merge commit's sha -- so it stays `blocked_on_operator` until GitHub
    can be asked, rather than walked `merged` around a run no later pass
    would reach. A provider that
    cannot answer -- no network, no key -- skips the reconcile in one line
    and the loop goes on as before: this is a repair of the mirror, not a
    gate on the work.
    """
    refresh_board_states(conn, project, provider)
    tickets = []
    for ticket in store.read.open_tickets(conn, project):
        if ticket.activeRunId is not None:
            continue
        if ticket.status == "blocked_on_operator" \
                and _parked_pull_request(conn, ticket.id) is not None:
            # GitHub's verdict, not the board's: a Done here is a person
            # who merged the pull request, and `_reconcile_pull_requests()`
            # closes the run out with the merge commit's sha when GitHub
            # can be asked. Walking the ticket `merged` around a parked run
            # would strand that run (KO-359 review).
            print(f"[holo2] reconcile left {ticket.linearIdentifier} to its"
                  " pull request: the run parked on it is GitHub's to close")
            continue
        tickets.append(ticket)
    if not tickets:
        return
    try:
        closed = provider.closed_identifiers([t.linearIdentifier for t in tickets])
    except Exception as e:  # any transport failure: the board could not be asked
        print(f"[holo2] reconcile skipped: the board could not be asked which"
              f" mirrored tickets it has closed ({e})")
        return
    for ticket in tickets:
        state = closed.get(ticket.linearIdentifier)
        if state not in RECONCILED_STATUS:
            continue
        to_status = RECONCILED_STATUS[state]
        line = (f"[holo2] reconciled {ticket.linearIdentifier}:"
                f" {ticket.status} -> {to_status} (Linear {state})")
        with store.transaction(conn):
            now = store.read.ticket_by_id(conn, ticket.id)
            if now is None or now.status != ticket.status \
                    or now.activeRunId is not None:
                # Moved or claimed while the board was being asked: the
                # verdict was formed on a row that no longer holds.
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
            store.tickets.walk_ticket(conn, ticket.id, to_status)
        print(line)
        if to_status == "abandoned" and target is not None:
            _retire_abandoned(target, conn, now)


def _retire_abandoned(target, conn, ticket):
    from holophyte.claim import retire_worktree

    run = store.read.run_detail(conn, ticket.lastRunId)
    if run is None or not run.branch:
        return
    reason = retire_worktree(target, run.branch)
    if reason:
        line = (f"[holo2] {ticket.linearIdentifier}: kept worktree"
                f" {worktree_path(target, run.branch)}: {reason}")
        print(line)
        store.record_event(conn, run.id, "worktree_retirement_refused", line)


# How the ticket's question begins once its pull request was closed on
# GitHub without merging: the run ends rejected, and the skip line reads
# this rather than the `--approve` that would merge nothing.
PR_CLOSED_QUESTION = "rejected: "


def _reconcile_pull_requests(target, conn, project, provider):
    """Ask GitHub about every pull request this project's parked runs wait
    on, and land the ones a person merged there (KO-359).

    A run parked on its pull request waits for `--approve`; the operator
    merges the pull request by hand after a coworker's review instead, and
    the run sat parked, the ticket In Progress, Shipped without it. This
    runs at loop startup, before the mirror reconcile so a ticket the
    merger also moved to Done still ships its run, and at the top of every
    later pass -- each serial claim, each scheduler tick -- over the
    project's `blocked_on_operator` tickets whose newest run holds a
    `prUrl` and is still parked in
    `awaiting_merge_approval`. One `pr_status.pull_status()` read per ticket. A
    merged pull request is that approval: `_land_github_merge()` ends the
    run merged with the merge commit's sha and walks the ticket to
    `merged`, Done on the board. One closed without merging ends the run
    rejected and makes the question `rejected: URL closed by LOGIN`
    (`_note_closed_pr()`); an open one changes nothing. A GitHub error is
    one printed line for that ticket and the pass goes on to the next, as
    the mirror reconcile skips a board that cannot be asked: this lands
    work already landed, it does not gate the work in the queue.

    Since KO-362 the same read also carries the pull request's
    `updatedAt` and review-thread count, held against what the last
    babysit pass recorded on the run (`runs.prSeenAt`,
    `runs.prSeenThreads`): an open pull request that moved past them has
    review activity nobody has answered, and `_rebabysit()` sends the
    run back to the babysitter exactly as `--babysit KO-n` does, at most
    once per `[merge] pr_poll_sec` per pull request. The read's
    `rateLimit` is remembered in `GITHUB_BUDGET`: under `RATE_FLOOR`
    points, the tick reads no pull request at all and prints one line
    naming the reset. Returns the Linear ids of the tickets sent back,
    so the serial loop can claim them again this pass.
    """
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
        elif not low:
            # A babysit round is many reads and writes: not on a budget
            # that is already low.
            issue = _rebabysit(conn, ticket, pull, status, poll_ms)
            if issue is not None:
                sent.add(issue)
        if low:
            return sent
    return sent


# The GraphQL budget below which the tick stops reading parked pull requests
# until the reset GitHub named (KO-362). The budget is 5000 points an hour
# per token and each read here is one point; the babysitter's own rounds are
# the spend worth protecting, so the floor is well above one tick's reads.
RATE_FLOOR = 500


class GitHubBudget:
    """What the last parked pull request read said of the token's GraphQL
    budget: `remaining` points and the `reset_at` GitHub named, or None
    for either when nothing has been read yet or the answer carried no
    `rateLimit`. One per process: the token is the process's."""

    def __init__(self):
        self.remaining = None
        self.reset_at = None

    def remember(self, status):
        if status.rate_remaining is not None:
            self.remaining = status.rate_remaining
            self.reset_at = status.rate_reset

    def low(self, now=None):
        """Under `RATE_FLOOR` with the reset still ahead. A reset that has
        passed, or one GitHub did not name, forgets the reading: the next
        read learns the budget again."""
        if self.remaining is None or self.remaining >= RATE_FLOOR:
            return False
        reset = _iso_epoch(self.reset_at)
        if reset is None or (time() if now is None else now) >= reset:
            self.remaining = self.reset_at = None
            return False
        return True


GITHUB_BUDGET = GitHubBudget()


def _iso_epoch(text):
    """`text`, GitHub's ISO 8601 timestamp, as epoch seconds; None for
    anything else."""
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
    """Whether the tick's pull request reads stop here, with the one line
    that says so when they do."""
    if not GITHUB_BUDGET.low():
        return False
    print(f"[holo2] GitHub's GraphQL budget is down to"
          f" {GITHUB_BUDGET.remaining} points; no parked pull request is read"
          f" until it resets at {GITHUB_BUDGET.reset_at}")
    return True


def _seen(status):
    """`store.record_pr_seen()`'s tuple from one `PullStatus`."""
    at = max([status.updated_at or "", *(item[1] for item in status.activity)])
    return (at or None, status.threads, status.checks, status.review)


def _rebabysit(conn, ticket, pull, status, poll_ms):
    """Send the run parked on `pull` back to the babysitter when the pull
    request has review activity the last pass did not see; the ticket's
    Linear id when it was sent, None otherwise (KO-362).

    Only newly authored content beyond the park's mark can wake it. A
    timestamp bump alone refreshes facts; it never dispatches a paid pass.
    The wake records the content in the same transaction as the intervention.
    """
    identifier, run_id = ticket.linearIdentifier, ticket.runId
    row = conn.execute("SELECT prSeenAt, prSeenThreads, lastHeartbeat,"
                       " (SELECT linearIssueId FROM tickets WHERE id = ?)"
                       " FROM runs WHERE id = ?",
                       (ticket.id, run_id)).fetchone()
    if row is None or status.updated_at is None:
        return None
    seen_at, seen_threads, parked_ms, issue = row
    mark = _seen(status)
    if seen_at is None:
        store.record_pr_seen(conn, run_id, mark, parked_only=True)
        pr_activity.record_commits(conn, run_id, status)
        return None
    arrived = pr_activity.arrived(conn, run_id, status, seen_at)
    if not arrived:
        store.record_pr_seen(conn, run_id, mark, parked_only=True,
                             facts_only=True)
        pr_activity.break_empty_wakes(conn, ticket)
        return None
    waited_ms = int(time() * 1000) - (parked_ms or 0)
    if waited_ms < poll_ms:
        store.record_pr_seen(conn, run_id, mark, parked_only=True,
                             facts_only=True)
        print(f"[holo2] {identifier}: {pull.url} has new review activity;"
              f" the next babysit round waits"
              f" {-(-(poll_ms - waited_ms) // 1000)}s ([merge] pr_poll_sec)")
        return None
    threads = "?" if status.threads is None else status.threads
    note = (f"new review activity on {pull.url}: "
            f"{', '.join(sorted({item[0] for item in arrived}))}; updated"
            f" {status.updated_at} (last seen {seen_at}), {threads} review"
            f" threads (last seen {seen_threads})")
    try:
        with store.transaction(conn):
            store.record_pr_seen(conn, run_id, mark)
            pr_activity.record_commits(conn, run_id, status)
            store.record_event(conn, run_id, "pr_wake", json.dumps(arrived))
            store.babysit(conn, ticket.id, note, source="supervisor")
    except store.ApproveRefused as refused:
        print(f"[holo2] {identifier}: {pull.url} has new review activity but"
              f" the ticket moved while GitHub was asked ({refused}); left"
              " alone")
        return None
    print(f"[holo2] {identifier}: {pull.url} has new review activity"
          f" (updated {status.updated_at}, {threads} review threads); run"
          f" {run_id} sent back to the babysitter")
    return issue


def _parked_phase(conn, run_id):
    """The run's `(branch, phase)` if it is parked awaiting merge approval,
    None otherwise: the reconcile acts on that run alone."""
    row = conn.execute("SELECT branch, phase FROM runs WHERE id = ?",
                       (run_id,)).fetchone()
    if row is None or row[1] != "awaiting_merge_approval":
        return None
    return row


def _parked_pull_request(conn, ticket_id):
    """The PR whose parked or rejected run owns the mirror status."""
    row = conn.execute(
        "SELECT r.prUrl FROM tickets t JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.id = ? AND r.phase IN ('awaiting_merge_approval', 'rejected')"
        " AND r.prUrl IS NOT NULL", (ticket_id,)).fetchone()
    return None if row is None else row[0]


def _land_github_merge(target, conn, provider, ticket, pull, status):
    """Close out the run parked on `pull` as merged: a person merged it on
    GitHub, and that is the `--approve` the park was waiting for."""
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
    """Reject only the same parked candidate that the GitHub read saw."""
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
    from holophyte.board import release_lease_label
    release_lease_label(target, conn, ticket.id, provider, ticket.runId)


def _reject_pr(conn, run_id, pull, who, branch, sha):
    """Record the human decision and preserve the candidate and board state."""
    who = who or "unknown"
    question = f"{PR_CLOSED_QUESTION}{pull.url} closed by {who}"
    reason = (f"closed on GitHub by {who} without merging;"
              f" branch {branch} preserved at {sha}")
    with store.transaction(conn):
        store.record_event(conn, run_id, "pull_request", reason)
        store.release(conn, run_id, "rejected", reason)
        ticket_id = store.read.run_snapshot(conn, run_id).ticketId
        store.walk_ticket(conn, ticket_id, "blocked_on_operator")
        store.set_question(conn, ticket_id, question)
        store.set_pull_request(conn, run_id, pull.url, sha)
        conn.execute("UPDATE runs SET parkKind = 'pull_request_closed' WHERE id = ?",
                     (run_id,))
    print(f"[holo2] {question}; {reason}")


def _pr_seen(target, pull, conn=None, run_id=None):
    """`(updatedAt, thread count, checks, review)` as the pull request
    reads now -- `store.record_pr_seen()`'s tuple -- for the park to
    record after the pass's own writes; None when GitHub could not be
    asked, which the park records as nothing seen."""
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

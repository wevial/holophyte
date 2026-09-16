"""Project the store's ticket state and run narrative onto Linear.

Mirrors validate ticket contracts, publish state and lease labels, and warn
on best-effort board failures. Escalation parks repeatedly failing tickets;
close-out releases their runs and refreshes the rendered findings window.
`ledger()` stores full entries before posting cleaned, capped board copies.
`file_ticket()` validates operator-supplied tickets before and after filing.
"""
import contextlib
import fcntl
import os
import re
import socket
import sys
from pathlib import Path

import store
import store.read
import store.tickets
import ticket_template
from holophyte.findings import refresh_findings
from holophyte.report import host_label
from holophyte.runs import warn_on_run

# The prefix of the board lease label (KO-351): `holo:HOST`, `holo:` and the
# writer's `[report] host_label`, so the ready column says which writer holds
# a ticket where the store lease, private to one writer's store, cannot.
LEASE_LABEL_PREFIX = "holo:"


BOARD_COMMENT_LIMIT = 12000
_COMMENT_BANNERS = (
    r"Reading additional input from stdin", r"(?:\*\*)?OpenAI Codex v",
    r"workdir: ", r"model: ", r"provider: ", r"approval: ", r"sandbox: ",
    r"reasoning effort: ", r"reasoning summaries: ",
    r"(?:\*\*)?session id: ", r"tokens used",
)


def comment_body(text, limit=BOARD_COMMENT_LIMIT):
    """Keep at most `limit` cleaned characters plus a notice counting the cut.
    The store retains the original text, including banners and omitted prose."""
    text = re.sub(
        r"(?m)^<!-- devin-review-badge-begin -->[^\n]*\n"
        r"[\s\S]*?^<!-- devin-review-badge-end -->[^\n]*(?:\n|$)",
        "", text,
    )
    text = re.sub(
        r"(?m)^(?:" + "|".join(_COMMENT_BANNERS) + r")[^\n]*(?:\n|$)",
        "", text,
    )
    text = re.sub(r"\n(?:[ \t]*\n){3,}", "\n\n\n", text)
    if len(text) <= limit:
        return text
    return (text[:limit] + f"\n[... {len(text) - limit} characters cut; "
            "the full round is on the run in the store]")


def lease_host(target):
    """The host half of this writer's lease label: `[report] host_label`,
    or the hostname when the target sets no label -- a writer without a
    label still has to hold its tickets against another writer, and the
    hostname is the name the store already records for its runs."""
    return host_label(target, socket.gethostname())


def lease_label(target):
    """This writer's board lease label, `holo:HOST`: the one label its
    claims write and its close-outs, requeues and backed-off claims take
    off. One per writer, not per run: the store, which knows which of this
    writer's runs is live, is what keeps a late removal off a fresh claim's
    label (`release_lease_label()`), and `lease_turn()` is what keeps a
    fresh claim out of the gap between that look and the removal."""
    return LEASE_LABEL_PREFIX + lease_host(target)


def lease_turn_path(target):
    """The lease turn for `target`, beside its store in the state directory
    -- never in the repository, where a task's `git add -A` could commit it."""
    return target.holo_dir / "lease.lock"


@contextlib.contextmanager
def lease_turn(target):
    """Hold `target`'s turn at the board lease label for the block.

    A blocking flock on a permanent file beside the store, created once and
    never unlinked (unlinking a flock file is what lets two holders exist),
    so every loop and every close-out on this store locks the same inode.
    Two things take it: a claim, for the span of `store.claim()` and the
    label write that follows, and a close-out's `release_lease_label()`,
    for the span of its look at the store and the removal. It exists
    because that look and that removal are two operations with a network
    call between them, and a sibling loop's claim landing in the gap --
    lease taken, label written -- was stripped off the board by the
    removal that followed, leaving the ticket free for another writer to
    claim while this store's fresh run was live (the review of KO-351).
    Under the turn no claim of this store can move between the look and
    the removal, so a live run the store names is always seen before its
    label is touched.

    Not the store's write lock, on purpose: that one is held for the
    microseconds of a transaction, never across a network call, and a
    loop working another ticket is not stalled by this one -- only
    claims and lease releases queue here, each for one board round trip.
    """
    path = lease_turn_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # drops the flock


def lease_turn_held(target):
    """Whether somebody holds `target`'s lease turn right now: a claim or
    a close-out of this store is between its look and its write, which is
    a loop (or a close-out) live on the store whatever its heartbeats say.
    A non-blocking try at the same flock, given straight back; a missing
    file is nobody's turn, and is not created here."""
    path = lease_turn_path(target)
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # drops the flock, when it was taken
    return False


def lease_holders(labels):
    """The hosts whose `holo:` labels are in `labels`, in board order; []
    without one (a board that does not label, or a task dict older than
    the key)."""
    return [str(label)[len(LEASE_LABEL_PREFIX):] for label in labels or []
            if str(label).startswith(LEASE_LABEL_PREFIX)]


def foreign_lease_holders(labels, host):
    """The hosts other than `host` holding a `holo:` lease in `labels`, in
    board order."""
    return [holder for holder in lease_holders(labels) if holder != host]


def drop_lease_label(conn, ticket_id, provider, issue_id, label):
    """Take exactly `label` off `issue_id`, once; never raise. A board that
    refuses leaves a `warning` row naming the label another writer will
    refuse the ticket on, and this writer's next claim treats as stale."""
    try:
        provider.unlabel_issue(issue_id, label)
    except Exception as e:  # noqa: BLE001 - best-effort board write
        warn(conn, ticket_id, f"the lease label {label} could not be removed"
                              f" from {issue_id} ({e}); another writer will"
                              " refuse the ticket until it is")


def release_lease_label(target, conn, ticket_id, provider, run_id):
    """Take this writer's lease label off the ticket's issue for run
    `run_id`'s close-out; never raise.

    The board half of every close-out that gives the store lease back -- a
    merge, a failure, a park, a sweep, a requeue -- so a label never
    outlives the lease it mirrors. The label names the writer, not the
    run, so the store decides whether it is still this run's to remove: a
    close-out that runs late, after a fresh claim of this store has
    re-asserted the same label under a new live run, leaves it on, since
    the ticket's `activeRunId` names that other run.

    That look and the removal run under `lease_turn()`, as the claim's
    lease-and-label does (see `close_out_failure()` for why the write
    lock is not held across a network call): without the turn a sibling
    loop could claim the ticket and write the same label between the
    look and the removal, taking the fresh run's lease off the board.
    Under the turn a claim either lands before the look, which then sees
    its run and leaves the label alone, or waits out the removal.
    Best-effort like `mirror_push()`; a storeless or boardless caller
    has nothing to release.
    """
    if conn is None or provider is None:
        return
    with lease_turn(target):
        ticket = store.read.ticket_by_id(conn, ticket_id)
        if ticket is None or ticket.activeRunId not in (None, run_id):
            return
        drop_lease_label(conn, ticket_id, provider, ticket.linearIssueId,
                         lease_label(target))


def mirror_key(task):
    """The `linearIssueId` a task's mirror is keyed by.

    The canonical issue UUID when the provider has one, and the human label
    otherwise. Written once here because two callers now have to agree on it:
    `mirror_task()` mirrors under this id, and the failure-pattern check below
    has to find that same row *before* anything is claimed.
    """
    return task.get("issue_id") or task["id"]


def store_status(conn, ticket_id):
    """The store's status column for `ticket_id`, for a printed decision."""
    return store.read.ticket_by_id(conn, ticket_id).status


def task_contract(task):
    """A provider task's contract as `(title, criteria, commands)`.

    The one mapping from the provider's shape to the store's, used by both
    sides of the drift check: `mirror_task()` mirrors the ticket through it, so
    the snapshot `store.claim()` freezes off that row is this contract, and
    `merge_drift()` snapshots the live ticket the same way. Two hand-rolled
    mappings would eventually disagree about, say, a ticket carrying no verify
    command, and the disagreement would read as drift on a ticket nobody
    touched. Positional, in the order `store.contract_snapshot()` takes them.
    """
    return (task["title"],
            list(task.get("criteria") or ()),
            [task["verify"]] if task.get("verify") else [])


def body_problem(task, repo=None):
    """The first template violation in the offered ticket's body, or None.

    The claim-time contract gate. `ticket_template.validate()` is what a
    ticket is held to before it enters the queue, and until now nothing on
    the claim path asked it: KO-165 was claimed with literal angle-bracket
    placeholders in its title, summary and first criterion and no What line,
    the title-only implementer skipped the in-scope work, review passed it,
    and the merge shadowed fifteen runs of history. The validator refuses
    that body; this is the call that puts it in the way.

    Advisories are scope guidance, not violations, so `blocking()` filters
    them out and an advisory-only body is claimed as before. The first
    blocker is returned, not the list: the loop prints one line per
    refusal and `ticket_template.py` gives the owner the full list.

    A task with no body at all (`None`, not `""`) is not judged: a provider
    that hands no body has nothing to validate; an empty description is
    the emptiest invalid body there is.

    `repo` is the target repository's path; with it the validator also
    refuses a body naming a path that repository gitignores, which the
    reviewer's export of the candidate can never contain.
    """
    body = task.get("body")
    if body is None:
        return None
    problems = ticket_template.blocking(
        ticket_template.validate(ticket_template.parse(body), repo=repo))
    return problems[0] if problems else None


def merge_drift(conn, run_id, provider, issue_id):
    """The contract fields that moved between the claim and now; () if none.

    The merge gate's question: the run was implemented, reviewed and verified
    against the ticket as it stood at the claim, so a body a human edited
    while the run was working means the candidate answers a contract that no
    longer exists. Asked here rather than continuously because this is the
    last moment the answer can still change anything — before it, an edit is
    something a fix round could absorb; after it, the branch is in main.

    Best-effort in one direction only: a provider that cannot re-read a
    ticket, a Linear that is down, a deleted issue, and a run claimed
    before the snapshot column existed all return `()` — not "nothing
    changed" but "this gate has no evidence", and refusing a verified
    merge on missing evidence would turn every Linear outage into a
    stuck queue. A failed read is a warning on the run so the silence is
    recorded; drift itself is the caller's to act on.
    """
    if conn is None or run_id is None or provider is None:
        return ()
    fetch = getattr(provider, "fetch_task", None)
    if fetch is None:
        return ()  # a provider with no re-read; nothing to compare against
    claimed = store.run_contract(conn, run_id)
    if claimed is None:
        return ()  # claimed before the snapshot existed
    try:
        live = fetch(issue_id)
    except Exception as e:
        warn_on_run(conn, run_id, f"could not re-read {issue_id} for the "
                                  f"merge-time drift check ({e}); merging on "
                                  "the contract frozen at the claim")
        return ()
    if not live:
        warn_on_run(conn, run_id, f"{issue_id} could not be found for the "
                                  "merge-time drift check; merging on the "
                                  "contract frozen at the claim")
        return ()
    return store.contract_drift(
        claimed, store.contract_snapshot(*task_contract(live)))


def mirror_task(conn, project, task, specced=True):
    """Mirror the offered ticket's live body into the store; return its id.

    The first half of a claim, split from the lease so the loop can ask the
    store about the ticket *as it is now* before it opens a run. The mirror is
    an upsert with no lease of its own, so a ticket that turns out to be
    unpickable has cost nothing but a refreshed row — which is the row the
    next offer is judged on. The lease itself is `store.claim()`, taken by
    `main()` once the fresh row has said yes; that is what stops two loops
    from working one project at once.

    Where the mirror lands is the store's routing rule (§2) applied to what
    the provider parsed: a ticket carrying both acceptance criteria and a
    verify command is `ready`, and one missing either is `needs_spec` and not
    pickable. The loop still picks in Linear, so what that decides here is
    whether the ticket can legally enter `in_flight` — an under-specced mirror
    cannot, and the board simply keeps saying whatever a human last set.

    The two ids the mirror stores are different ids: `linearIssueId` is the
    canonical issue UUID the provider hands over as `issue_id`, and is what
    the mirror is keyed and re-found by, while `linearIdentifier` is the
    human "KO-123" label. Storing the label in both would key the mirror on
    the mutable one — a later UUID-carrying writer would mirror the same
    issue a second time under its real id. A provider with no UUID gets a
    mirror keyed on the identifier it does have.

    No `depends_on`, on purpose: the provider does not parse a dependency
    list, so the store's copy is the only one and `mirror_ticket()` keeps
    it when the caller says nothing — `[]` would clear a blocked ticket's
    dependencies in the very row the pickability gate reads next.

    `specced=False` mirrors the ticket with its criteria and verify command
    withheld — the same row a criteria-less body produces: `needs_spec`,
    which `pickable()` refuses. That is how a body the template validator
    rejects is parked, whatever its lists say; the rows keep no word for
    "malformed" and the printed refusal names the problem.

    `blocked_on_deps` is also the empty pass's park for a ticket the board
    stopped listing (KO-425): the listing naming it again ends the wait,
    so a specced mirror walks the row back to `ready` on this shared path
    — the admit step and the scheduler's queue mirror both — and
    `pickable()` re-parks one still blocked. `blocked_on_operator` is a
    human's and never moved.
    """
    title, criteria, commands = task_contract(task)
    if not specced:
        criteria, commands = [], []
    ticket_id = store.tickets.mirror_ticket(
        conn,
        project,
        linear_issue_id=mirror_key(task),
        linear_identifier=task["id"],
        title=title,
        acceptance_criteria=criteria,
        verification_commands=commands,
        time_box_ms=task["budget_min"] * 60 * 1000,
        # The body the loop read, so the store serves the contract the run
        # worked from (KO-328); a task with none (`body_problem()` treats
        # that as nothing to validate) mirrors as empty.
        body=task.get("body") or "",
    )
    if criteria and commands:
        with store.transaction(conn):
            ticket = store.read.ticket_by_id(conn, ticket_id)
            if ticket.status == "blocked_on_deps":
                store.tickets.walk_ticket(conn, ticket_id, "ready")
                if not store.tickets.pickable(conn, ticket_id):
                    store.tickets.walk_ticket(conn, ticket_id,
                                              "blocked_on_deps")
    return ticket_id


def release_run(conn, run_id, merged, reason=None, outcome_class="work",
                merge_sha=None):
    """Give the lease back when the loop is done with a run, merged or not.

    Called from the loop's `finally`, because the failure paths are the ones
    that matter: a run that dies holding the lease blocks every later claim on
    the project, and a preserved branch is meant to wait for a human without
    also freezing the queue.

    A failure reason names the phase the run stopped in, read back from the
    store rather than remembered here: on a crash the loop's own idea of where
    it was died with the exception, while the phase `set_phase()` last wrote
    is exactly where the run got to. A caller that knows better says so with
    `reason` — the supervisor sweep does, because "stopped in phase working"
    is true of a swept run and says nothing about why it was swept.

    `merge_sha` is the merge commit a merged run landed on main as, stamped
    on the row in the same transaction that ends the run; None when the
    caller has none to give.
    """
    if merged:
        store.release(conn, run_id, "merged", merge_sha=merge_sha)
        return
    # No preservation claim in the default: the paths that delete or keep a
    # branch say so themselves in the reason they pass, and stamping
    # "preserved" on a reason-less failure lied on every deletion path
    # (KO-146 incident, run 10).
    store.release(conn, run_id, "failed", reason or
                  f"run stopped in phase {store.run_phase(conn, run_id)}",
                  outcome_class=outcome_class)


# --- Linear as the notice board ----------------------------------------------
# State-model §1: Holophyte owns the in-flight substate and Linear is where it
# gets posted, so ticket status travels one way — store to Linear, never read
# back — and `mirror_push()` is the loop's only writer of a workflow state.
# That is what stops loop logic from depending on which column a human dragged
# a card into: the drag is overwritten by the next push, and nothing branches
# on it.
#
# The table is module-level config rather than five literals at the call sites
# because the thing likeliest to change is the mapping, not the pushing —
# per-project mappings and §9's team-vs-project question (does a project or a
# team own these state names?) both land here. Neither is answered here.
MIRROR_STATES = {
    "ready": "Todo",
    "in_flight": "In Progress",
    "merged": "Done",
    "abandoned": "Canceled",
    # No Linear state means "waiting on an operator", so a blocked ticket goes
    # back to the column a human picks work out of, until there is a state
    # that says it properly. Telling the operator what is being waited on is a
    # comment, and status comments are not this projection's business.
    "blocked_on_operator": "Todo",
}
# `needs_spec` and `blocked_on_deps` are deliberately unmapped: neither is a
# statement about the board — an unspecced ticket is wherever its author left
# it, and a dependency-blocked one is the resolver's business — so the
# projection leaves those tickets' Linear state alone rather than inventing a
# column for them.


def warn(conn, ticket_id, summary):
    """Record a warning on the ticket's run and print it; never raise.

    Best-effort work that failed is still something the run has to account
    for, so it goes in the same event stream as the phase changes, as a
    `warning` row a reader can pick out of the narrative — written against
    the ticket's active run, or its last one once released, because that
    is the stream a reader of this ticket is already reading.
    """
    run_id = None
    if conn is not None:
        ticket = store.read.ticket_by_id(conn, ticket_id)
        # A ticket with no run has nothing to hang the row on; the printed
        # line is then the whole record.
        if ticket is not None:
            run_id = (ticket.activeRunId if ticket.activeRunId is not None
                      else ticket.lastRunId)
    warn_on_run(conn, run_id, summary)


def mirror_push(conn, ticket_id, provider):
    """Project the ticket's stored status onto its Linear state; return it.

    Last-write-wins and one-way: the store's `tickets.status` is the truth,
    `MIRROR_STATES` says what that truth looks like on the board, and nothing
    is read back. Returns the state pushed, or None when there was nothing to
    push — an unmapped status, or a push that failed.

    Failure is a warning, not an error. Linear being unreachable must not
    fail a run that has already merged, so a push that raises leaves the
    board stale, the store right, and a `warning` row in the run's stream.
    That is the failure §1 asks for: the notice board may be behind, the
    loop may not be wrong.

    A `conn` of None makes this a no-op, like the rest of the store seam: a
    storeless `run_task()` holds no status to project.
    """
    if conn is None:
        return None
    ticket = store.read.ticket_by_id(conn, ticket_id)
    if ticket is None:
        raise ValueError(f"no ticket {ticket_id}")
    issue_id, identifier = ticket.linearIssueId, ticket.linearIdentifier
    status = ticket.status
    state = MIRROR_STATES.get(status)
    if state is None:
        return None
    try:
        provider.set_state(issue_id, state)
    except Exception as e:
        warn(conn, ticket_id, f"Linear mirror push failed for {identifier}: "
                              f"{status} -> {state} ({e}); the store keeps"
                              " the status and the board stays stale")
        return None
    return state


def mirror_status(conn, ticket_id, status, provider):
    """Move the ticket to `status` in the store, then push the move to Linear.

    The pair the loop calls at a boundary that changes ticket status, in that
    order: the store is written first because it is the truth, and Linear is
    told afterwards because it is a copy.

    Returns whether the store took the move, not what was pushed, because the
    two failures are not the same kind of thing. A push that does not land
    leaves a stale board and a `mirror_push()` warning, and the caller carries
    on regardless; a move the §3 diagram does not draw means the ticket is not
    where the caller thought it was, and only the caller knows whether its
    next step still makes sense. So the refusal is warned about, nothing is
    pushed — the board keeps showing the status the ticket actually has — and
    False says the status change did not happen.

    A storeless caller gets False for the same reason `mirror_push()` gets
    None: there is no status here to move or to project.
    """
    if conn is None:
        return False
    try:
        store.tickets.transition(conn, ticket_id, status)
    except store.tickets.IllegalTransition as e:
        warn(conn, ticket_id, f"ticket status left where it was: {e}")
        return False
    mirror_push(conn, ticket_id, provider)
    return True


# --- failure-pattern escalation ----------------------------------------------
# A rollback catches one failure; nothing here caught a *pattern*. A ticket the
# loop cannot finish stays non-terminal on the board, so the provider offers it
# again on the next pass, and the pass after that, forever — the loop has no
# memory of having already tried. The store does: one `runs` row per attempt,
# each stamped with the outcome it ended on. So the escalation is a count over
# rows that already exist, asked at the two moments that can act on it — when a
# run closes out, and before the next one is claimed.
#
# The threshold is a module constant rather than project config on purpose:
# per-project policy is a real question (a flaky integration suite deserves a
# higher bar than a typo fix) and this is not the ticket that answers it. The
# seam is here, with one name to change.
MAX_FAILED_RUNS = 2


def failure_history(conn, ticket_id):
    """The ticket's failed runs since a human last intervened, oldest first.

    One query for both halves of the escalation, as `(attempt, reason)` rows:
    its length is what trips the threshold and its reasons are what the human
    is told. Reading them together is what stops the comment from listing a
    different set of runs from the one that blocked the ticket.

    Bounded on the left by the newest `source='human'` interventions row on
    any of the ticket's runs. A recorded human action (a resume, an operator
    close-out) is a human taking the ticket back: the failures before it are
    that human's accepted history, not evidence to keep re-parking on — so
    one unblock buys a fresh MAX_FAILED_RUNS rather than exactly one attempt
    forever. A board drag writes no interventions row and so — deliberately,
    per the escalation's original rule (69fe923) — forgives nothing; a
    `source='supervisor'` row grants no amnesty either (a deliberate
    boundary, not a description of existing rows).

    Two bounds, because timestamps alone cannot draw this line: failures
    count by `endedAt` against the newest human row's `at`, and a failed
    run carrying a human `close_out` row of its own is excluded *by
    identity* — the canonical repair records the close-out first and
    releases the run a clock-read later, so `endedAt`-vs-`at` ordering is
    jitter, and a hand-dispositioned run must not be the strike that
    re-parks the ticket on its next failure.

    Only `outcomeClass = 'work'` rows count. An `infra` failure — a claim
    race, a reviewer container that would not start — is the factory
    failing, not the ticket, and is neither a strike nor a line in the
    comment; `--report` still lists it.
    """
    since = store.read.latest_human_intervention_at(conn, ticket_id)
    return [(run.attempt, run.outcomeReason) for run
            in store.read.failed_attempts_since(conn, ticket_id, since)]


def escalation_comment(history):
    """The Linear comment a blocked ticket gets: one line per failed run.

    The status alone says the factory gave up without saying what it kept
    hitting, and the reasons are already written — `release()` stamps each run
    with the phase it stopped in. So the comment is a rendering, not a new
    account of the failures, and a run that ended with nothing recorded says
    so rather than being left off the list.
    """
    lines = [f"**Blocked after {len(history)} failed runs.** Counted since"
             " the last recorded human intervention, if any; attempt numbers"
             " are lifetime. The factory will not claim this ticket again"
             " until a human moves it out of this state. What each counted"
             " attempt ended on:", ""]
    lines += [f"- attempt {attempt}: {reason or 'no reason recorded'}"
              for attempt, reason in history]
    return "\n".join(lines)


STRIKE_QUESTION_TAIL = (
    " runs failed on this ticket since the last recorded human intervention"
    " and the factory stopped claiming it; a human decides what happens next.")


def strike_question(count):
    """The question the escalation parks a ticket on: the count, then a
    fixed sentence, fixed so `is_strike_question()` can tell the loop's
    own park from a module's (a merge conflict, `merge?`, an open pull
    request) when both leave the ticket `blocked_on_operator` with
    enough counted failures behind it (KO-345)."""
    return f"{count}{STRIKE_QUESTION_TAIL}"


def is_strike_question(question):
    """Whether `question` is the escalation's, as `strike_question()` wrote
    it, rather than a park reason of a module's own."""
    return bool(question) and question.endswith(STRIKE_QUESTION_TAIL)


def escalate(conn, ticket_id, provider):
    """Park a ticket whose failed runs have reached `MAX_FAILED_RUNS`.

    Returns whether the ticket *is* blocked when this call returns, not
    whether this call is what blocked it — the two differ on every pass
    after the first, and the claim path reads the first answer, so a
    ticket already blocked keeps refusing claims instead of being worked
    again the moment the escalation stops being news.

    Only an `in_flight` ticket is escalated, the same rule as the edge the
    store draws. A ticket sitting anywhere else is not the loop's to park:
    `merged` work the board keeps re-offering collects failed claims too,
    and blocking it would lie about finished work — the stale-board
    re-push in `main()` is what that case wants instead.

    The block is the ordinary status move plus one comment, in that order and
    with the same discipline as `mirror_push()`: the store is the truth and is
    written first, Linear is a copy and is told after, and a comment that does
    not land is a warning on the run rather than a failure of the escalation.
    """
    if conn is None:
        return False
    ticket = store.read.ticket_by_id(conn, ticket_id)
    if ticket is None:
        return False
    status, issue_id = ticket.status, ticket.linearIssueId
    identifier = ticket.linearIdentifier
    if status == "blocked_on_operator":
        return True  # already parked; the answer, not a second escalation
    if status != "in_flight":
        return False
    history = failure_history(conn, ticket_id)
    if len(history) < MAX_FAILED_RUNS:
        return False
    if not block_ticket(conn, ticket_id, provider,
                        strike_question(len(history))):
        return False
    try:
        provider.comment(issue_id, comment_body(escalation_comment(history)))
    except Exception as e:
        warn(conn, ticket_id, f"failure history comment failed for "
                              f"{identifier} ({e}); the store keeps the block"
                              " and Linear is not told why")
    print(f"[holo2] {identifier} blocked after {len(history)} failed runs")
    return True


def block_ticket(conn, ticket_id, provider, question):
    """Park `ticket_id` as `blocked_on_operator`, asking `question`.

    The status move through `mirror_status()`, then the question into the
    column the schema reserves for it, so a supervisor or `/attention`
    reading the store can see what is waited on without asking Linear.
    Returns whether the store took the move; a refused move writes none.

    The one way a ticket is parked, whoever parks it: the escalation after
    one failure too many, and the merge gate under `[merge] approve =
    "human"`, whose question is `merge?`.
    """
    if not mirror_status(conn, ticket_id, "blocked_on_operator", provider):
        return False
    conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                 (question, ticket_id))
    conn.commit()
    return True


def close_out_failure(target, conn, run_id, ticket_id, reason=None, provider=None,
                      confirm=None, outcome_class="work", refresh=True):
    """End a failed run the one way the factory ends failed runs.

    Three writes in a fixed order, and the order is the point. The failure
    record goes first, inside `release()`'s transaction, which stamps the
    outcome and only then clears the ticket's lease: a crash between them
    leaves a failed-looking run still holding a lease, which a human or a
    later release can free, rather than a free lease under a run that
    still looks alive — the double-claim hazard, and the one asymmetry
    worth ordering for. Then the escalation, a count over the row just
    written, so a failure is escalated on the pass that recorded it. Then
    the window, regenerated last so the entry that ends the run is in it.

    Factored out of the loop's `finally` because the supervisor sweep fails
    runs too, and a run failed by the sweep has to close out identically to
    one the loop failed itself — same outcome, same lease, same escalation
    counter, same rendered entry. The only thing the sweep supplies of its own
    is the `reason`, and the only thing it does differently is that the
    process being failed is not the caller.

    Which is what `confirm` is for. A caller failing somebody else's run
    decided that from a read, and between that read and this write the run's
    own process may have heartbeated, changed phase or finished — so the
    decision is re-reached here instead, inside the transaction that writes
    the failure and under the write lock that keeps the run's process out of
    it. Returning false abandons the close-out with none of it written, and
    this answers false in turn; the callback may also record what it is about
    to do -- or that it declined to -- because a note of a failure that then
    did not happen is worse than no note, and a decline nobody wrote down is
    indistinguishable from a sweep that never came. The loop's own `finally`
    passes nothing: a process failing itself cannot race itself, and there is
    no verdict of its own to re-reach.

    Only the release is under that lock. The escalation may call Linear,
    and a supervisor holding the write lock across a network call would
    stall every live loop until it answers — so it stays outside, where
    a failed push leaves a stale board, not a half-closed run.

    `outcome_class` is the row's `outcomeClass`: `work` unless the failure
    was an `InfraFailure`, in which case the escalation that follows does
    not count it.

    `refresh=False` leaves the window to the caller: a pool worker renders
    it under the merge lock, where the file is not written into the
    checkout beside a sibling's merge (the review of KO-343). The release
    and the escalation are the same either way.
    """
    with store.transaction(conn):
        if confirm is not None and not confirm():
            return False
        release_run(conn, run_id, False, reason, outcome_class)
    escalate(conn, ticket_id, provider)
    # The board lease goes with the store lease, in the same close-out
    # (KO-351); outside the lock for the reason the escalation is.
    release_lease_label(target, conn, ticket_id, provider, run_id)
    if refresh:
        refresh_findings(target, conn)
    return True


def ledger(conn, run_id, task_id, kind, text, provider):
    """Store the full narrative entry before posting its cleaned board copy.

    `kind` is a `store.LEDGER_KINDS` value. Board failures leave the row intact;
    missing store or board connections are reported without failing the run.
    FINDINGS.md is rendered from store rows at close-out, never appended here.
    """
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if conn is not None and run_id is not None:
        store.record_ledger(conn, run_id, kind, text)
    else:
        print("[holo2] no store to record the ledger entry in")
    if provider is None:
        print("[holo2] no board to archive to; record kept in the store")
        return
    try:
        provider.comment(task_id, f"**{ts}**\n\n{comment_body(text)}")
    except Exception as e:
        print(f"[holo2] board comment failed ({e}); record kept in the store")


# --- Operator command: filing a ticket from a file --------------------------

# The priorities `--file-ticket` may create an issue with, each the word for
# one of Linear's integers (urgent 1, high 2, medium 3, low 4); absent, the
# issue is created with none, as before the flag existed.
FILE_TICKET_PRIORITIES = {"urgent": 1, "high": 2, "medium": 3, "low": 4}

def _ticket_problems(text, repo):
    """The blocking template violations of `text`, checked against `repo`."""
    return ticket_template.blocking(
        ticket_template.validate(ticket_template.parse(text), repo=repo))


def file_ticket(target, path, state, board, out=None, priority=None,
                update=None):
    """`--file-ticket`'s whole body: validate `path`, create the issue in the
    target's `board`, relate it, read it back and validate that.

    `priority` is one of `FILE_TICKET_PRIORITIES`' words or None; the word
    is mapped to Linear's integer for the create call and printed as given
    at the end of the filed line, and None sends no priority at all.

    Returns 0 with the filed line printed, 1 with the first problem printed
    and nothing created when the file fails validation, and 2 with the
    identifier *and* the first problem printed when the body Linear stored
    fails it: the ticket exists then, and the operator has to fix it there,
    so its identifier is printed before the problem and is never lost.

    The re-read is the point of the command. Every ticket the loop refused
    as `needs_spec` this week was valid on disk and broken in transfer -- a
    client rewriting bold and autolinks -- so the file is validated twice,
    once as written and once as Linear gives it back, and only the second
    pass says the transfer was clean.

    `update` is an identifier (`KO-n`) or None. Given, no issue is created:
    that issue's title, description and estimate are replaced from the file
    (state and priority stay as they are), and the same read-back validates
    what Linear stored, with the same exits. Every contract revision on
    2026-09-03 was a hand patch through a client that rewrote the body, and
    two of them left a ticket the loop skipped as `needs_spec`; this is the
    file going to the board checked in both directions, as filing is.

    A dependency is part of the contract, so once the body is stored the
    blockers the file names and the board does not yet hold are recorded,
    the way filing records them, and named on the printed line with a `+`.
    A blocker the board holds and the file no longer names is left in place
    and named too: the gate follows the file upward, and a narrowed contract
    is the operator's deliberate removal, never a side effect. KO-279 gained
    a `Depends on:` by update on 2026-09-07 and was claimed before that
    blocker had run, because the update changed the text and not the gate.

    `board` is the target's `[board]` pair (`project_id`, `team`), resolved
    by `cli()` before the file is read: a target with no board exits there,
    naming the key. The module is imported here rather than at the top, the
    way `provider.LinearProvider` does it: the loop's other paths through
    this module never file a ticket.
    """
    import linear_provider

    out = out or sys.stdout
    text = Path(path).read_text()
    ticket = ticket_template.parse(text)
    problems = _ticket_problems(text, target.path)
    if problems:
        print(f"[holo2] {path}: {problems[0]}", file=out)
        return 1
    if update is not None:
        issue_id = linear_provider.update_issue(update, ticket.title, text,
                                                ticket.estimate_min)
        # Read after the body is stored: a refused update adds nothing, and
        # the difference is taken against the board as it stands then.
        held = linear_provider.blockers_of(update)
        identifier = update
        named = ticket.depends_on or []
        added = [b for b in named if b not in held]
        for blocker in added:
            linear_provider.add_blocker(issue_id, blocker)
        parts = []
        if added:
            parts.append("blocked by " + ", ".join(f"+{b}" for b in added))
        extra = [b for b in held if b not in named]
        if extra:
            parts.append("board also holds " + ", ".join(extra))
        line = f"[holo2] updated {identifier}: {ticket.title}"
        if parts:
            line += f" ({'; '.join(parts)})"
        print(line, file=out)
    else:
        issue = linear_provider.create_issue(
            board.project_id, board.team, ticket.title, text,
            ticket.estimate_min, state,
            priority=FILE_TICKET_PRIORITIES[priority] if priority else None)
        for blocker in ticket.depends_on or []:
            linear_provider.add_blocker(issue["id"], blocker)
        identifier = issue["identifier"]
        detail = f"{state}, {ticket.estimate_min} min"
        if ticket.depends_on:
            detail += f", blocked by {', '.join(ticket.depends_on)}"
        if priority:
            detail += f", {priority}"
        print(f"[holo2] filed {identifier}: {ticket.title} ({detail})",
              file=out)
    stored = _ticket_problems(
        linear_provider.fetch_description(identifier), target.path)
    if stored:
        print(f"[holo2] {identifier}: as stored by Linear, {stored[0]}",
              file=out)
        return 2
    return 0

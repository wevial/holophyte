"""store.operate: the operator API -- the writes that end and resume runs.

Moved verbatim out of `store/__init__.py` (KO-393): `release()` and the
`TERMINAL_PHASES`/`ENDED_PHASES`/`OUTCOME_CLASSES` constants it owns, the
escalation-ladder commands `requeue()`/`approve()`/`babysit()`/`repoint()`
with their refusals and the `_release_parked()` transaction `approve()` and
`babysit()` share, `GATE_CONFLICT_REASON`/`is_gate_conflict()` and
`repoint()`'s `FULL_SHA`, the §5 resume machinery (`RESUMABLE_*`/
`PARKED_PHASES`, the `RUN_PHASE_TRANSITIONS` graph, `ResumeRefused`/
`GuidanceNotAccepted`, `resume()`) and `record_intervention()` with the
`INTERVENTION_*` unions it validates against. The `runEvents` writers,
`park()` and `record_pr_seen()` are run lifecycle, not operator API, and
stay home; `PHASES`, `_append_event`, `set_phase()` and `record_ledger()`
are shared with the run API and are imported back from the package, while
`walk_ticket` comes straight from `store.tickets` -- the package binds it
only after this module is imported. The package re-exports every name, so
`store.release()` keeps working.
"""
from __future__ import annotations

import re
import time

from . import PHASES, _append_event, record_ledger, set_phase
from .schema import _transaction
from .tickets import walk_ticket

# The `runs.phase` a run ends in for each `runs.outcome`, so `release()` cannot
# leave a finished run parked in the phase it was working in. `killed` is its
# own phase in §4; the other two failure outcomes share `failed`.
TERMINAL_PHASES = {
    "merged": "done",
    "killed": "killed",
    "abandoned": "failed",
    "failed": "failed",
}

# The phases those outcomes leave behind. A run with `endedAt` stamped and
# sitting in one of them is over, and that pair is what `release()` refuses to
# end a second time. A resumed run is not in the set: `resume()` moves a failed
# run back to a work phase and clears its ending, so the run it hands back
# moves and is releasable again.
ENDED_PHASES = frozenset(TERMINAL_PHASES.values())

# `runs.outcomeClass`: what a failure is evidence about. Mirrors the CHECK.
OUTCOME_CLASSES = frozenset({"work", "infra"})


def release(conn, run_id, outcome, reason=None, now=None,
            outcome_class="work", merge_sha=None):
    """End run `run_id` with `outcome` and give the ticket's lease back.

    The mirror of `claim()`, and the reason a crashed loop does not brick the
    ticket: `tickets.activeRunId` is the lease, so a run that ends without
    clearing it blocks every later claim on that ticket forever. Callers
    therefore release on failure paths too, not only on the happy one.

    One `BEGIN IMMEDIATE`, for the same read-then-write reason `claim()` takes
    it: stamp `endedAt`/`outcome`/`outcomeReason` and the terminal phase
    `TERMINAL_PHASES` gives for the outcome, then clear the ticket's
    `activeRunId`, moving its pointer to `lastRunId` so the finished run is
    still reachable from the ticket.

    Through `_transaction()` rather than a `BEGIN` of its own, so a caller
    that has already opened one joins instead of raising. A process failing
    *itself* has nothing to join for -- it is the only writer of its own run
    -- but a process failing somebody else's run has to re-read the state it
    decided on and clear the lease under one lock, or the run heartbeats in
    between and the lease is handed to a second worker while the first is
    still writing. That is the supervisor sweep, and this is where its
    re-check has to be able to sit.

    The run's telemetry is finalized in the same transaction: `endedAt` is
    the other end of the elapsed time `startedAt` opened, and
    `reviewRoundCount` is counted off the run's own `reviewRounds` rows. Both
    are written once, here, so a finished run carries how long it took and how
    many rounds it needed without a reader having to re-derive either.

    The terminal phase moves through `set_phase()`, so ending a run stamps a
    heartbeat and appends the transition to the run's event stream like any
    other phase change. A failure outcome also parks the phase the run stopped
    in as its `resumePhase`, which is the only moment that phase is still
    known: `runs.phase` reads `failed` from here on, and §5 resumes a failed
    run into the phase it left.

    Releasing a run that has already ended — `endedAt` stamped and parked in
    one of `ENDED_PHASES` — does nothing at all. Since this call writes phase
    as well as outcome, an unguarded second release would be destructive
    rather than harmless: it would re-end a `merged`/`done` run as
    `failed`/`failed`, and a repeat of a failed release would read that run's
    own `failed` phase back as the phase it stopped in and so wipe the
    `resumePhase` §5 resumes into. Terminal state and the resume point are
    written once, by the release that ended the run. Both lease clears stay
    scoped to *this* run id for the same reason, so no release can drop a
    lease a newer run has since taken.

    An unknown `run_id` is a caller bug and raises `ValueError`. `now` is
    epoch milliseconds for `endedAt`, defaulting to the clock.

    `outcome_class` is `runs.outcomeClass`: `work` unless the caller knows
    the failure was the factory's own (`infra`), in which case the row is
    kept out of the escalation count. An unknown class raises before any
    write, the same as an unknown outcome.

    `merge_sha` is `runs.mergeSha`: the merge commit a `merged` run landed
    on main as, written here because this is the transaction that makes the
    run merged and the loop is the only caller that still knows the sha. It
    is the one fact FINDINGS cannot recover from the other columns. Only a
    `merged` outcome may carry one; any other outcome with a sha is a caller
    bug and raises before any write.
    """
    if outcome not in TERMINAL_PHASES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if outcome_class not in OUTCOME_CLASSES:
        raise ValueError(f"unknown outcome class {outcome_class!r}")
    if merge_sha is not None and outcome != "merged":
        raise ValueError(
            f"merge_sha {merge_sha!r} on a run released as {outcome!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT ticketId, endedAt, phase FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ticket_id, ended_at, phase = row
        if ended_at is not None and phase in ENDED_PHASES:
            # Already over. Returning leaves the block having written nothing
            # rather than re-stamping an ending over the real one; an owned
            # transaction commits empty, and a joined one is the caller's to
            # end either way.
            return
        # Through `set_phase()` like every other phase move, so the run's last
        # transition is in its event stream too: a merged run whose log stops
        # at `merging` reads as a run that never finished.
        stopped_in = set_phase(conn, run_id, TERMINAL_PHASES[outcome],
                               note=f"run ended, outcome {outcome}", now=now)
        # §5's "it re-enters the phase it left", recorded here because
        # `release()` is the last caller that still knows what that phase was:
        # after this write the run says `failed` and nothing else remembers
        # where the work had got to. Only the four phases §5 calls mechanically
        # resumable are worth recording — a run that failed while `claimed` or
        # mid-merge has no work phase to go back to, and `resume()` reads the
        # NULL as §4's edge back to `working`.
        resume_phase = (stopped_in
                        if TERMINAL_PHASES[outcome] == "failed"
                        and stopped_in in RESUMABLE_WORK_PHASES
                        else None)
        # `reviewRoundCount` is stamped here, from the rows themselves, for
        # the same reason the phase is: this is the close-out, so this is the
        # moment the count is final. Counting the run's own `reviewRounds`
        # rather than trusting a caller's tally keeps the column from
        # disagreeing with the rounds it summarizes.
        conn.execute(
            "UPDATE runs SET endedAt = ?, outcome = ?, outcomeReason = ?,"
            " outcomeClass = ?, resumePhase = ?, mergeSha = ?,"
            " reviewRoundCount = (SELECT COUNT(*) FROM reviewRounds"
            "                     WHERE runId = ?)"
            " WHERE id = ?",
            (now, outcome, reason, outcome_class, resume_phase, merge_sha,
             run_id, run_id),
        )
        conn.execute(
            "UPDATE tickets SET activeRunId = NULL, lastRunId = ?"
            " WHERE id = ? AND activeRunId = ?",
            (run_id, ticket_id, run_id),
        )


class RequeueRefused(Exception):
    """A requeue `requeue()` will not do; nothing was written.

    The ticket does not exist, still has a live run, is neither `in_flight`
    nor parked on a merge-gate conflict, or its last run did not end
    `failed` -- each is the same answer to the operator: this is not a
    failed ticket waiting to go back in the queue, so the message names
    which and the command line exits on it.
    """


# The outcome reason the merge gate fails a run with when merging `main`
# into the branch conflicts (KO-342): `is_gate_conflict()` recognises it,
# and `requeue()` admits a `blocked_on_operator` ticket on that ground
# alone (KO-365). The loop composes the reason from this prefix so the two
# cannot drift apart.
GATE_CONFLICT_REASON = "merging main into "


def is_gate_conflict(reason):
    """Whether a run's `outcomeReason` is the merge gate's conflict park."""
    return (reason or "").startswith(GATE_CONFLICT_REASON) \
        and " conflicted on: " in reason


def requeue(conn, ticket_id, note, now=None):
    """Put a failed ticket back in the queue; return the run it failed in.

    The escalation ladder's rung-3 pair (`record_intervention()` then
    `walk_ticket()`) as one rung-1 call: a run that fails leaves its ticket
    `in_flight` with no active run, which `pickable()` refuses until an
    operator moves it, and on 2026-09-03 that was five REPL sessions. Both
    writes land in one `_transaction()`, so the ticket is never `ready`
    without the `requeue` row that says why -- and the row carries `note`,
    the operator's reason, rather than the mislabeled `close_out` those
    sessions wrote.

    One more admission (KO-365): a ticket parked `blocked_on_operator`
    because the merge gate's merge of `main` into the branch conflicted
    (`is_gate_conflict()` on the newest run's reason). The run failed and
    the branch was preserved; the operator resolves the merge on the branch
    and this is the way back -- `--repoint` refuses a failed run. The block
    is cleared with the same row and walk. Any other `blocked_on_operator`
    park (a pull request, `merge?`, a strike-out) keeps the refusal.

    Refuses, with `RequeueRefused` and no write, anything else: an unknown
    ticket, one with an active run, one not `in_flight` (already `ready`,
    say), or one whose last run ended some other way (merged) or never
    ended. Touches no board state: the loop mirrors the Linear status when
    it claims.
    """
    with _transaction(conn):
        row = conn.execute(
            "SELECT linearIdentifier, status, activeRunId, lastRunId"
            " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise RequeueRefused(f"ticket {ticket_id} does not exist")
        identifier, status, active_run_id, last_run_id = row
        if active_run_id is not None:
            raise RequeueRefused(
                f"{identifier}: run {active_run_id} is still live;"
                " a requeue is for a ticket whose run has ended")
        run = (conn.execute("SELECT outcome, outcomeReason FROM runs"
                            " WHERE id = ?", (last_run_id,)).fetchone()
               if last_run_id is not None else None)
        parked_on_conflict = status == "blocked_on_operator" \
            and run is not None and run[0] == "failed" \
            and is_gate_conflict(run[1])
        if status != "in_flight" and not parked_on_conflict:
            raise RequeueRefused(
                f"{identifier} is {status}, not in_flight; nothing to requeue")
        if run is None:
            raise RequeueRefused(
                f"{identifier} has no ended run to requeue after")
        outcome = run[0]
        if outcome != "failed":
            raise RequeueRefused(
                f"{identifier}: run {last_run_id} ended {outcome},"
                " not failed; nothing to requeue")
        record_intervention(conn, last_run_id, "requeue", note, now=now)
        if parked_on_conflict:
            conn.execute("UPDATE tickets SET blockedQuestion = NULL"
                         " WHERE id = ?", (ticket_id,))
        walk_ticket(conn, ticket_id, "ready")
    return last_run_id


class ApproveRefused(Exception):
    """An approval `approve()` will not do; nothing was written.

    The ticket does not exist, has a live run, or its newest run is not
    parked in `awaiting_merge_approval` -- each is the same answer to the
    operator: there is no candidate waiting for "merge?" here, so the
    message names the ticket's status and its run's phase, and the command
    line exits on it.
    """


# The phase an approved candidate's next run resumes into: written as the
# parked run's `resumePhase` by `approve()`, read back by the loop's claim
# path, which goes straight to the gate (`RUN_PHASE_TRANSITIONS['claimed']`).
APPROVED_RESUME_PHASE = "merge_gate"


def approve(conn, ticket_id, note, now=None):
    """Release a ticket parked for merge approval; return the parked run's id.

    The operator's answer to `merge?` under `[merge] approve = "human"`, as
    one transaction: an `interventions` row with action `approve` carrying
    `note`, the parked run ended -- outcome `abandoned`, because it neither
    merged nor failed and the next run is what merges its candidate -- with
    `resumePhase` set to `APPROVED_RESUME_PHASE`, and the ticket walked to
    `ready`. The loop's next claim reads that `resumePhase` off the ticket's
    newest run and, its worktree still standing, skips implementation and
    review and takes the candidate straight to the merge gate. Under
    `[merge] mode = "pr"` the candidate lands through its pull request: the
    resumed run shepherds the PR and, once its checks are green and its
    threads resolved, merges it through the API -- the approval is the
    human's "merge" whatever `[merge] approve` says.

    Ended rather than left parked: a run is one attempt, and the attempt
    that merges is the next one, so leaving this row open in
    `awaiting_merge_approval` would keep `/attention`-style readers pointing
    at a decision already made. `abandoned` is not `failed`: the escalation
    count reads `outcome = 'failed'` only, so an approval is never a strike.

    Refuses, with `ApproveRefused` and no write, anything that is not a
    parked ticket: an unknown ticket, one with a live run, one whose status
    is not `blocked_on_operator` (walked on by hand while its run still sat
    parked, say), or one whose newest run is in any phase but
    `awaiting_merge_approval` (ready with no run yet, failed, merged). The
    refusal names the ticket's status and, past that, the run's phase.
    Touches no board state: the loop mirrors the Linear status when it
    claims.
    """
    return _release_parked(
        conn, ticket_id, "approve", note,
        "approved for merge; the next claim resumes the candidate"
        " at the merge gate", now)


def babysit(conn, ticket_id, note, now=None, source="human"):
    """Send a ticket parked on its pull request back to the babysitter; return
    the parked run's id.

    `approve()`'s twin for `[merge] mode = "pr"`, and the same transaction
    with the action `babysit` on the `interventions` row: the parked run is
    ended `abandoned` with its resume point at the merge gate and the ticket
    walked to `ready`, so the loop's next claim resumes the candidate --
    and, the run carrying a pull request, babysits it again: reads the
    threads that arrived since the park, verdicts them, fixes and replies,
    waits for the checks. What it is not is an approval: a PR that comes up
    ready to merge under `[merge] approve = "human"` parks again for the
    human's "merge" rather than landing on the operator's "look again".
    The refusals are `approve()`'s, as `ApproveRefused`, plus one of its
    own: a run parked with no pull request (`runs.prUrl` NULL -- parked
    under `[merge] mode = "local"`) has no threads to look at again, and
    releasing it would send the candidate down the local gate, where a
    release is a merge; that is `approve()`'s to do, so the babysitter
    refuses it with nothing written. `source` is who sent it back:
    `"human"` for `--babysit`, `"supervisor"` when the loop's own tick
    saw new review activity on the pull request (KO-362), so the
    intervention row and the ledger say which.
    """
    return _release_parked(
        conn, ticket_id, "babysit", note,
        "sent back to the babysitter; the next claim resumes the candidate"
        " on its pull request", now, require_pr=True, source=source)


def _release_parked(conn, ticket_id, action, note, reason, now,
                    require_pr=False, source="human"):
    """The transaction `approve()` and `babysit()` share: the intervention
    row with `action`, the parked run ended `abandoned` for `reason` with
    its resume point at the merge gate, the ticket walked to `ready`.
    `require_pr` refuses, before the first write, a parked run that has no
    `prUrl`."""
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT linearIdentifier, status, activeRunId, lastRunId"
            " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise ApproveRefused(f"ticket {ticket_id} does not exist")
        identifier, status, active_run_id, last_run_id = row
        if active_run_id is not None:
            raise ApproveRefused(
                f"{identifier} is {status} with run {active_run_id} still"
                " live; an approval is for a run parked awaiting merge"
                " approval")
        if status != "blocked_on_operator":
            raise ApproveRefused(
                f"{identifier} is {status}, not blocked_on_operator; nothing"
                " is parked awaiting merge approval")
        run = (conn.execute("SELECT phase, prUrl FROM runs WHERE id = ?",
                            (last_run_id,)).fetchone()
               if last_run_id is not None else None)
        if run is None:
            raise ApproveRefused(
                f"{identifier} is {status} and has no run; nothing is"
                " parked awaiting merge approval")
        phase, pr_url = run
        if phase != "awaiting_merge_approval":
            raise ApproveRefused(
                f"{identifier} is {status} and its newest run {last_run_id}"
                f" is {phase}, not awaiting_merge_approval; nothing to"
                " approve")
        if require_pr and pr_url is None:
            raise ApproveRefused(
                f"{identifier} is parked with no pull request (run"
                f" {last_run_id} was parked under [merge] mode = \"local\");"
                " there are no threads to shepherd, and a release here would"
                " merge the candidate -- that is --approve's to say")
        record_intervention(conn, last_run_id, action, note, now=now,
                            source=source)
        release(conn, last_run_id, "abandoned", reason, now=now)
        # `release()` records a resume point for failed runs only; this one
        # is the operator's, written once the ending is stamped.
        conn.execute("UPDATE runs SET resumePhase = ? WHERE id = ?",
                     (APPROVED_RESUME_PHASE, last_run_id))
        walk_ticket(conn, ticket_id, "ready")
    return last_run_id


class RepointRefused(Exception):
    """A re-point `repoint()` will not do; nothing was written.

    The ticket does not exist, has a live run, its newest run is already
    approved or is not parked in `awaiting_merge_approval`, or the sha is
    not a full commit id --
    each is the same answer to the operator: there is no parked candidate
    here to move, or nothing a merge gate could hold a branch to, so the
    message names the ticket and the reason, and the command line exits
    on it.
    """


# The shape of the one thing `repoint()` will record as a candidate: a full
# 40-hex commit id, the form `git rev-parse HEAD` prints and the form the
# park records. Either case is accepted (git does), and lowercased before
# it is stored so `_candidate_drift()`'s equality test against the
# lowercase form git prints holds. An abbreviated sha would pass that test
# never, and a branch name would pass it only by accident.
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


def repoint(conn, ticket_id, sha, note, now=None):
    """Move a parked candidate to `sha`; return `(run_id, old_sha)`.

    The approve path holds a parked run's branch to the sha its park
    recorded and fails, tree untouched, when the tip differs -- right for a
    commit slipped in after the park, wrong for the one legitimate case: the
    operator rebuilt the branch as the same commits on a rewritten `main`
    (2026-09-05, three parked candidates after the unpushed history was
    filtered). Before this the only way to re-point was raw SQL on
    `runs.candidateSha`. This is that write as a recorded intervention, all
    in one `_transaction()`: an `interventions` row with action `repoint`
    carrying `note` as its `guidance`, a narrative `runEvents` row naming
    the old and new shas, then `candidateSha` set to `sha`. `--approve` is
    unchanged: the gate still holds the branch to the recorded sha, now the
    rebuilt one.

    Refuses, with `RepointRefused` and no write, anything that is not a
    parked, not-yet-approved ticket with a well-formed sha: an unknown
    ticket, one with a live run, one whose newest run carries a
    `resumePhase` (approved: its release is already in flight, so the
    refusal says to requeue instead), one whose newest run is in any phase
    but `awaiting_merge_approval` (ready with no run yet, failed, merged),
    or a `sha` that is not 40 hex characters (either case; it is stored
    lowercased, the form git prints). The refusal names the ticket and the
    reason. Touches no branch: rebasing the branch itself is the operator's
    git work, before this call.

    Two holds, both required. `park()` leaves `resumePhase` NULL and
    `approve()` writes `merge_gate` there as it ends the run, so a run the
    loop produced is refused by its phase alone; the `resumePhase` check
    is the contract's own precondition, and it is what catches a row walked
    by hand into a parked phase with an approval already recorded on it.
    """
    if not isinstance(sha, str) or not FULL_SHA.match(sha):
        raise RepointRefused(
            f"ticket {ticket_id}: {sha!r} is not a full 40-hex commit id;"
            " a re-point names the exact commit the gate will hold the"
            " branch to")
    sha = sha.lower()
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT linearIdentifier, status, activeRunId, lastRunId"
            " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise RepointRefused(f"ticket {ticket_id} does not exist")
        identifier, status, active_run_id, last_run_id = row
        if active_run_id is not None:
            raise RepointRefused(
                f"{identifier} is {status} with run {active_run_id} still"
                " live; a re-point is for a run parked awaiting merge"
                " approval")
        run = (conn.execute("SELECT phase, candidateSha, resumePhase FROM"
                            " runs WHERE id = ?", (last_run_id,)).fetchone()
               if last_run_id is not None else None)
        if run is None:
            raise RepointRefused(
                f"{identifier} is {status} and has no run; nothing is"
                " parked awaiting merge approval")
        phase, old_sha, resume_phase = run
        if resume_phase is not None:
            raise RepointRefused(
                f"{identifier} is {status} and its newest run {last_run_id}"
                f" is already approved (resumes at {resume_phase}); its"
                " release is in flight, so requeue instead of re-pointing")
        if phase != "awaiting_merge_approval":
            raise RepointRefused(
                f"{identifier} is {status} and its newest run {last_run_id}"
                f" is {phase}, not awaiting_merge_approval; nothing to"
                " re-point")
        record_intervention(conn, last_run_id, "repoint", note,
                            guidance=note, now=now)
        _append_event(conn, last_run_id, "narrative", "repoint",
                      f"candidate re-pointed from {old_sha} to {sha}: {note}",
                      now)
        conn.execute("UPDATE runs SET candidateSha = ? WHERE id = ?",
                     (sha, last_run_id))
    return last_run_id, old_sha


# §5's resumable set, transcribed: "mechanically resumable — `failed`, or any
# of working/verifying/reviewing/addressing where lastHeartbeat is older than
# staleThresholdMs", plus `blocked_on_operator`, the one phase that takes an
# answer. Staleness is deliberately not re-derived here: §5 says resume is
# always safe to attempt, and whether a live run *should* be resumed is the
# supervisor's judgement, not a fact this mutation can improve on.
#
# Everything else is refused. `claimed` has nothing to resume into, and
# `merge_gate`, `awaiting_merge_approval`, `merging`, `squashing`, `done` and
# `killed` are either mid-merge or over: none of them is a phase the §4
# diagram draws a resume edge out of.
#
# The set is split because the working half is also what `release()` records
# as a failed run's `resumePhase`: `failed` and `blocked_on_operator` are
# phases a run is parked *in*, not phases work was interrupted in, so neither
# is a phase to send a resumed run back to.
RESUMABLE_WORK_PHASES = frozenset(
    {"working", "verifying", "reviewing", "addressing"}
)
RESUMABLE_PHASES = RESUMABLE_WORK_PHASES | {"failed", "blocked_on_operator"}
# The phases a run is parked *in*, alive and waiting for a person: the loop
# wrote a question (or, under `[merge] approve = "human"`, an approved
# candidate), gave the lease back and went home. Neither has a heartbeat by
# design, so the supervisor sweep leaves both alone; `park()` is the write
# that puts a run in `awaiting_merge_approval`, and the ticket that releases
# it (`--approve`) is what moves it on.
PARKED_PHASES = frozenset({"blocked_on_operator", "awaiting_merge_approval"})

# §4's run graph as an edge table, keyed like `TICKET_TRANSITIONS` so both
# state machines render through `render_state_graph()` the same way. The
# edges are the ones the loop writes — `factory.py`'s `set_phase()` calls,
# `release()`'s move into the terminal phase for each outcome, and `resume()`'s
# way back out of a parked run — not a `set_phase()` gate: that function moves
# a run between any two phases on purpose, so the table is the loop's map and
# the wiring tests hold the walked streams against it. Every phase in `PHASES`
# is a key so a declared phase always renders as a node; `squashing` is
# declared but has no edge because this loop never enters it (the merge is
# --no-ff). `awaiting_merge_approval` is entered from `merge_gate` under
# `[merge] approve = "human"` by `park()`; the run stays open there, so the
# only edges out are the ones a release writes for a run that did not merge.
RUN_PHASE_TRANSITIONS = {
    # `claimed -> merge_gate` is the approved candidate's run: `--approve`
    # ended the parked run with `resumePhase = 'merge_gate'`, and the claim
    # that follows reuses its worktree and branch and goes straight to the
    # gate -- nothing to implement or review, the candidate already was.
    "claimed": frozenset({"working", "merge_gate", "failed", "killed"}),
    "working": frozenset({"verifying", "failed", "killed"}),
    "verifying": frozenset({"reviewing", "failed", "killed"}),
    "reviewing": frozenset({"addressing", "merge_gate", "failed", "killed"}),
    "addressing": frozenset({"verifying", "failed", "killed"}),
    "merge_gate": frozenset({"merging", "awaiting_merge_approval", "failed",
                             "killed"}),
    # `awaiting_merge_approval -> done` is the pull request a person merged
    # on GitHub while the run waited for `--approve`: the loop's reconcile
    # ends the parked run merged with that merge commit (KO-359). The
    # operator's own `--approve` still ends it `failed` (abandoned) and lets
    # the next run merge.
    "awaiting_merge_approval": frozenset({"done", "failed", "killed"}),
    "merging": frozenset({"done", "failed", "killed"}),
    "squashing": frozenset(),
    "done": frozenset(),
    # `resume()`: a failed run re-enters its `resumePhase`, or `working`
    # when none was recorded; a `blocked_on_operator` run always re-enters
    # `working`.
    "failed": RESUMABLE_WORK_PHASES,
    "blocked_on_operator": frozenset({"working"}),
    "killed": frozenset(),
}
assert set(RUN_PHASE_TRANSITIONS) == set(PHASES)


class ResumeRefused(Exception):
    """A resume the state model does not allow; nothing was written.

    Raised for a run that does not exist and for one in a phase §5 gives no
    resume for — both are the same answer to the caller: this run is not
    going to start moving again because you asked.
    """


class GuidanceNotAccepted(ResumeRefused):
    """Human text was offered to a run that never asked for it.

    §5's enforced invariant, and the reason this is a subclass rather than a
    return value: guidance landing on a `working` run is the mid-run steering
    injection the whole phase model exists to prevent, so it is a validation
    error and the run is left exactly as it was.
    """


def resume(conn, run_id, guidance=None, source="human", now=None):
    """Resume `run_id`, optionally with `guidance`; return the phase re-entered.

    State-model §5. Two rules, and the first one is the point of the ticket:

    * **Guidance requires `blocked_on_operator`.** A non-None `guidance` on a
      run in any other phase raises `GuidanceNotAccepted` before anything is
      written. Mid-run injection is what makes supervisors unpredictable, so
      the supervisor's `redirect` has to park the run with a question and wait
      for the answer to come back through this one door. The converse is not a
      rule: a bare resume of a blocked run is allowed, an operator saying
      "never mind, carry on".
    * **A bare resume re-enters the phase the run left.** For `failed` that is
      `runs.resumePhase`, recorded by whoever failed it, falling back to
      `working` — the only edge §4 draws out of `failed` — when nothing was
      recorded. A run parked in `blocked_on_operator` always re-enters
      `working` (§4 again: `blocked_on_operator --> working : guidance
      provided`); an answered question resumes as work whatever the run was
      doing when it stopped to ask. And a stale `working`/`verifying`/
      `reviewing`/`addressing` run re-enters the phase it is already in, which
      is that same rule with nothing to move.

    `resumePhase` is cleared on the way out, so a later failure that records
    nothing cannot resume into a phase left over from an earlier one.

    Every accepted resume writes an `interventions` row — §2 keeps those out
    of `runEvents` precisely because they are queryable decisions, and a
    resume is one whether or not a human typed anything. `source` says who
    resumed (`human` or `supervisor`); the trigger is `manual` because §6's
    triggers name why a run was *stopped* and none of them names a resume.

    Resuming a `failed` run clears the ending `release()` stamped --
    `endedAt`, `outcome`, `outcomeReason`, `outcomeClass` back to its default
    -- because the run is live again and everything that reads `endedAt`
    reads it as "over": `set_phase()` refuses a stamped run (KO-213),
    `heartbeat()` leaves one alone, the sweep skips it and the FINDINGS
    window lists it. A resumed run that kept its stamp could not move, and
    the stretch of work it does is closed out by the release that ends it,
    which writes a fresh ending over nothing. A resumed run's
    `lastHeartbeat` stays where the worker left it: heartbeats are written
    by whoever is doing the work, and stamping one here would claim liveness
    this call has no evidence for. `now` is epoch milliseconds for the
    intervention's `at`, defaulting to the clock.

    Runs in one `_transaction()`, like the other writers, so a resume arriving
    as the effect of a Linear webhook commits with its delivery id.
    """
    # An empty string is not an answer, and it is falsy, so a caller that let
    # one through would have its "no guidance" and its "guidance" paths
    # silently agree here while §5 says they are different calls.
    if guidance is not None and (
        not isinstance(guidance, str) or not guidance.strip()
    ):
        raise ValueError(f"guidance must be non-empty text or None, got {guidance!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT phase, resumePhase FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ResumeRefused(f"run {run_id} does not exist")
        phase, resume_phase = row
        # The guidance gate is asked first: on a `done` run offered guidance
        # both rules are broken, and the one worth naming is the injection.
        if guidance is not None and phase != "blocked_on_operator":
            raise GuidanceNotAccepted(
                f"run {run_id} is in phase {phase}, not blocked_on_operator:"
                " guidance is only accepted by a run that asked for it"
            )
        if phase not in RESUMABLE_PHASES:
            raise ResumeRefused(
                f"run {run_id}: phase {phase} is not one state-model §5 resumes"
            )
        if phase == "failed" and resume_phase is not None:
            target = resume_phase
        elif phase in ("failed", "blocked_on_operator"):
            target = "working"
        else:
            target = phase
        conn.execute(
            "UPDATE runs SET phase = ?, resumePhase = NULL WHERE id = ?",
            (target, run_id),
        )
        if phase in ENDED_PHASES:
            conn.execute(
                "UPDATE runs SET endedAt = NULL, outcome = NULL,"
                " outcomeReason = NULL, outcomeClass = 'work' WHERE id = ?",
                (run_id,),
            )
        conn.execute(
            'INSERT INTO interventions'
            ' (runId, source, "trigger", "action", guidance, at)'
            " VALUES (?, ?, 'manual', 'resume', ?, ?)",
            (run_id, source, guidance, now),
        )
    return target


# §2's intervention unions, transcribed from `_INTERVENTIONS_DDL` so a caller
# can validate before the INSERT answers with a constraint name instead of
# the value that was wrong. The schema test holds these against the database.
INTERVENTION_SOURCES = ("supervisor", "human")
INTERVENTION_TRIGGERS = ("time_box", "off_criteria", "looping",
                         "review_stuck", "linear_cancelled", "linear_completed",
                         "manual")
INTERVENTION_ACTIONS = ("redirect", "kill", "extend_time_box", "resume",
                        "close_out", "requeue", "approve", "repoint",
                        "babysit", "reconcile", "restart_supervisor",
                        "launch_loop", "config_edit")


def record_intervention(conn, run_id, action, note, source="human",
                        trigger="manual", question=None, guidance=None,
                        now=None):
    """Record one operator/supervisor decision on `run_id`; return its id.

    §2 keeps interventions out of runEvents because they are queryable
    decisions — but the only writer was `resume()`, so every other human
    action was either unrecorded or falsely recorded as a resume (the KO-146
    incident's four mislabeled rows). This is the general writer: the row and
    a narrative runEvent carrying `note` land in one `_transaction()`, so the
    record-before-acting discipline is one call — and a caller that wants the
    record atomic with the change it describes opens `transaction()` around
    both, which this joins.

    `question` is required for a redirect (§2 pairs the two; a redirect row
    with nothing asked would be semantically invalid with no way to repair
    it) and `guidance` carries a human's answer where one exists. `note` is
    the narrative and deliberately lands in the event stream, not the row:
    the columns keep their §2 meanings instead of doubling as a notes field.
    """
    if action not in INTERVENTION_ACTIONS:
        raise ValueError(f"unknown intervention action {action!r}")
    if source not in INTERVENTION_SOURCES:
        raise ValueError(f"unknown intervention source {source!r}")
    if trigger not in INTERVENTION_TRIGGERS:
        raise ValueError(f"unknown intervention trigger {trigger!r}")
    if not isinstance(note, str) or not note.strip():
        raise ValueError(f"note must be non-empty text, got {note!r}")
    if action == "redirect" and (
            not isinstance(question, str) or not question.strip()):
        raise ValueError("a redirect records the question it asked;"
                         f" got {question!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute("SELECT 1 FROM runs WHERE id = ?",
                        (run_id,)).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            'INSERT INTO interventions'
            ' (runId, source, "trigger", "action", question, guidance, at)'
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, source, trigger, action, question, guidance, now))
        _append_event(conn, run_id, "narrative", "intervention",
                      f"{source} {action}: {note}", now)
        # The narrative's copy, in the same transaction: an operator's step
        # is a ledger entry like a round or a merge, so a reader of the
        # run's story sees it where it happened. A human is the operator;
        # the supervisor is the loop's own machinery.
        record_ledger(conn, run_id, "intervention",
                      f"{source} {action}: {note}",
                      source="operator" if source == "human" else "loop",
                      now=now)
    return cursor.lastrowid

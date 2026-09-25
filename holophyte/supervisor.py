"""The supervisor: the stale-run sweep, its report, the lock and the loop.

The second process the factory runs, and the one that must keep working
while the loop is down. `sweep()` reads the live runs under one transaction,
counts strikes and returns a `Sweep` of `Trip`s; `act_on_trip()` fails a
tripped run through the loop's own `close_out_failure()` once
`still_tripped()` agrees the verdict survives; `sweep_lines()` and its
halves render a pass and `sweep_report()` is `--sweep`'s whole body. The
project form of the supervisor loop -- `supervise()`, one `supervise_pass()`
per interval, held to one process per target by `acquire_supervisor_lock()`
and its helpers, refused with `SupervisorHeld` -- is `PROJECT --supervise`'s
for a project the host registry does not list; the host sweep
(`holophyte.sweep_host`) runs the same steps once per registered store.
`supervisor_liveness_line()` is `--report`'s line about the watcher, read
from the heartbeat rows either writes. Neither form re-executes itself: a
process runs the code it started with, and a newer store ends the project
form for its service manager to start again on the new code. Beyond the
standard library it imports `store` and `store.read` for the rows,
`open_store` from `holophyte.runs`, `close_out_failure` from
`holophyte.board`, `sweep_config` from `holophyte.config_tables`, and
`host_label`, `format_age`, `REPORT_GAP` from `holophyte.report`; nothing
from `factory`.

Sixth slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import collections
import contextlib
import functools
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from time import time

import holophyte
import store
import store.read
from holophyte import deadline
from holophyte.board import (
    body_problem,
    close_out_failure,
    lease_turn_held,
    mirror_key,
    mirror_task,
    refresh_board_states,
)
from holophyte.board_sync import observe_board
from holophyte.board_sync import owed as tickets_owed
from holophyte.claim_store import store_mode
from holophyte.config import budget_scale, serve_config
from holophyte.config_tables import BOARD_ASK_SEC, sweep_config
from holophyte.reexec import LOOP_UNIT, start_loop
from holophyte.report import format_age, host_label
from holophyte.runs import MAX_ROUNDS, open_store
from holophyte.supervisor_lock import (
    acquire_supervisor_lock,
    release_supervisor_lock,
    supervisor_lock_path,
)
from holophyte.sweep_report import merge_lock_lines, sweep_lines
from store.working import agent_work

# --- the supervisor's stale-run sweep -----------------------------------------
# The loop watches itself only while it is alive. A run whose process crashed,
# hung, or was killed leaves a row in a work phase, a heartbeat that stopped
# and a ticket lease nobody will ever give back -- and nothing noticed.
#
# The sweep is the noticing: it reads runs, counts strikes and reports what
# tripped. `--sweep` on its own stops there, which is what makes it safe to
# point at a loop that is still working. `--sweep --act` goes on to do
# something about each trip, and what it does is the loop's own failure
# close-out (`close_out_failure()`) run from outside the run: the run is
# failed, its leases are given back, the failure counts towards the ticket's
# escalation threshold, and the window is regenerated. Nothing is killed and
# nothing is deleted -- the branch and worktree wait for a human exactly as
# they do after a failure the loop noticed itself. Detection without action
# still needs somebody watching; action is what makes an overnight run safe,
# because a hung run becomes a clean failure the next invocation can route
# around instead of a zombie holding the lease forever.

# The phases the review-stuck check applies in: the ones a run is in between
# a review round ending and the next one starting. Anywhere else the rounds on
# file are history the run has moved past, not a review it is still inside.
REVIEW_PHASES = ("reviewing", "addressing")


# The phases a run can be swept in: everything the store's enum has, less the
# three a finished run sits in and the two a run is parked in
# (`store.PARKED_PHASES`). Derived from `store.PHASES` rather than listed, so
# a phase added there is swept by default -- the safe direction for a check
# whose failure mode is a hung run nobody looks at.
#
# The parked phases are excluded because a parked run is *supposed* to have
# no heartbeat: the loop wrote the question (`blocked_on_operator`), or
# parked an approved candidate for a person to say merge
# (`awaiting_merge_approval`), released the process and went home, and the
# run waits for a human for however long that takes. Sweeping it would report
# every parked run as dead within five minutes, and 2/5 would then fail the
# states the design keeps open for an operator's answer.
SWEEPABLE_PHASES = tuple(
    phase for phase in store.PHASES
    if phase not in store.ENDED_PHASES and phase not in store.PARKED_PHASES)

# The mechanical conditions a run can trip. `time_box` is spelled as the
# `interventions.trigger` value of the same name, so the name an operator
# reads here is the name that vocabulary already uses.
STALE_HEARTBEAT = "stale_heartbeat"
TIME_BOX = "time_box"
REVIEW_STUCK = "review_stuck"

# The `runEvents.kind` an acted-on trip is recorded under, so the condition
# that failed a run is in the run's own stream and not only in its outcome
# reason: a reader following the narrative sees the supervisor arrive.
SWEEP_EVENT = "supervisor_sweep"

# One tripped run, as the sweep reports it: which run, whose ticket, what it
# was doing, which condition, and the numbers that condition was decided on.
# `evidence` is prose for an operator, not a parseable field -- what a reader
# needs to agree with the verdict without opening the database.
#
# `heartbeat` is not for the report. It is the `lastHeartbeat` the verdict was
# reached on, carried so `still_tripped()` can ask whether the run has shown
# any sign of life since -- a verdict is only actionable against the state it
# was made from.
#
# `host` is the machine the run was claimed on, from `runs.host`, and None
# for a row older than that column. It is carried for the report only: the
# sweep does not branch on it.
Trip = collections.namedtuple(
    "Trip",
    ("run_id", "ticket", "phase", "condition", "evidence", "heartbeat",
     "host"), defaults=(None,))
# What one pass found: how many live runs it looked at, the trips among them,
# whether it acted on them, and the runs it is watching -- silent, at a strike
# below the trip threshold. The count is carried because "nothing tripped" is
# only reassuring next to the number of runs that were checked -- silence and
# health look identical without it -- and `acted` because a report of tripped
# runs reads completely differently depending on whether they were left alone
# or failed. `watched` is carried because a first strike printed as "all
# healthy" hides exactly the evidence the next invocation acts on.
# `restarts` is the loop-level condition, apart from the per-run ones: each is
# a `(sha, age_ms)` for a self-merge re-exec no loop activity has followed
# past the grace window, carried once -- the sweep that found it stamped it
# reported -- so the line is printed by the pass that recorded it and no
# other.
Sweep = collections.namedtuple("Sweep",
                               ("swept", "trips", "acted", "watched",
                                "outcomes", "restarts", "locks"),
                               defaults=((), ()))
# What acting on one trip came to. `acted` is whether the run was failed;
# `phase` is the run's phase as the re-check found it, which for a decline is
# the status the summary names -- the run finished, moved on or answered --
# and for an act is the phase it was failed in. `acted` is the outcome, not
# the flag `sweep()` was called with: the two parted in holophyte-bugs.md #1,
# where a summary read the flag and reported a failure the re-check had
# refused to write.
Outcome = collections.namedtuple("Outcome", ("trip", "acted", "phase"))


def review_overlap(conn, run_id):
    """How much `run_id`'s latest two finished review rounds share, or None.

    `(earlier_round, later_round, overlap)` from `store.findings_overlap()`
    over the two most recent rounds with an `endedAt` -- a round still being
    reviewed has no findings to compare yet. None when there are fewer than
    two such rounds, or when either has no semantic findings. Evidence-only
    rows do not count: two empty rounds score 1.0 by the measure's definition
    (equal sets), but treating an approval as a repeated review would trip
    a run whose review went well.

    The findings are the store's own JSON, written by
    `store.record_review_round()` after it validated them, so a row that
    fails to compare here is a corrupted store rather than a reviewer's bad
    day -- and the `ValueError` is left to surface as one.
    """
    rounds = store.read.newest_ended_rounds(conn, run_id)
    if len(rounds) < 2:
        return None
    later, earlier = rounds[0].round, rounds[1].round
    earlier_findings = json.loads(rounds[1].findings)
    later_findings = json.loads(rounds[0].findings)
    if any(store.findings_fingerprint(findings) == store.EMPTY_FINGERPRINT
           for findings in (earlier_findings, later_findings)):
        return None
    return earlier, later, store.findings_overlap(earlier_findings,
                                                  later_findings)


def still_tripped(target, conn, trip, knobs=None):
    """Recheck a trip under the acting transaction's write lock.

    An ended run or changed phase is acquitted. A fresh heartbeat clears only
    staleness: overrunning or stuck work can still be alive. Recompute agent
    work and its allowance because settlement or completed review rounds may
    change time-box evidence between observation and action. Recompute review
    overlap against the original knobs for the same reason."""
    knobs = sweep_config(target) if knobs is None else knobs
    run = store.read.run_snapshot(conn, trip.run_id)
    if run is None:
        return False
    if run.endedAt is not None or run.phase != trip.phase:
        return False
    if trip.condition == STALE_HEARTBEAT:
        return run.lastHeartbeat == trip.heartbeat
    if trip.condition == TIME_BOX:
        spent = agent_work(run)
        return (spent is not None and bool(run.timeBoxMs)
                and spent > time_box_allowance(
                    run.timeBoxMs * budget_scale(target), run.reviewRoundCount,
                    run.reviewRoundCap or MAX_ROUNDS, knobs.budget_grace,
                    knobs.run_cap))
    if trip.condition == REVIEW_STUCK:
        overlap = review_overlap(conn, trip.run_id)
        return (overlap is not None
                and overlap[2] >= knobs.review_overlap_threshold)
    return True


def act_on_trip(target, conn, trip, provider=None, knobs=None):
    """Fail one tripped run, if it is still tripped; return an `Outcome`.

    The whole of what acting means. `close_out_failure()` is the loop's own,
    unchanged and not re-implemented here, so a swept failure is the same kind
    of row as any other failure: the same outcome, the same released leases,
    the same contribution to the ticket's escalation count, the same rendered
    entry. Only two things are the sweep's own -- the reason, which names the
    condition instead of the phase, and the event, which puts the supervisor's
    arrival in the run's narrative where the reason alone would leave the
    stream ending at whatever the dead process last managed to say.

    Both go in under `close_out_failure()`'s `confirm`, which is to say inside
    the transaction that writes the failure, and only once `still_tripped()`
    has agreed the verdict survives. The classification pass had to commit
    before this ran -- failing a run may call Linear, and the store's write
    lock must not be held across a network call -- and committing let the
    run's process back in to heartbeat or finish. Re-checking there and
    failing there is what keeps the two from separating: a run cannot prove
    itself alive in the gap between being confirmed dead and having its lease
    handed to the next worker, because under one `BEGIN IMMEDIATE` there is no
    gap for it to do so in.

    A decline is recorded under the same event kind, distinguishing a sweep
    that stood down from one that never arrived. `acted` records what happened,
    not the flag the sweep was called with.

    Nothing is signalled, killed or deleted. Freeing the lease and recording
    the failure is enough to unblock the queue, and a supervisor that also
    tried to kill things would need to be right about which process it was
    killing. A wedged run that is not writing to the store but is still
    working on disk therefore remains out of scope, and is the reason the
    strike rule is two sightings rather than one.
    """
    knobs = sweep_config(target) if knobs is None else knobs
    ticket_id = store.read.run_snapshot(conn, trip.run_id).ticketId
    seen = {"phase": None}

    def confirm(target):
        # Read under the same lock the verdict is re-reached under, so the
        # phase the outcome names is the one the decision was made on.
        run = store.read.run_snapshot(conn, trip.run_id)
        seen["phase"] = run.phase if run is not None else None
        if not still_tripped(target, conn, trip, knobs):
            if run is not None:
                store.record_event(
                    conn, trip.run_id, SWEEP_EVENT,
                    f"supervisor sweep: {trip.condition} ({trip.evidence})"
                    f" no longer held at re-check; run is now {run.phase};"
                    " no action")
            return False
        store.record_event(
            conn, trip.run_id, SWEEP_EVENT,
            f"supervisor sweep: {trip.condition} ({trip.evidence});"
            " failing the run and releasing its leases")
        from store.working import settle_work

        settle_work(conn, trip.run_id)
        return True

    acted = close_out_failure(
        target, conn, trip.run_id, ticket_id,
        f"swept by the supervisor in phase {trip.phase}: {trip.condition}"
        f" ({trip.evidence}); branch and worktree preserved for a human",
        # `close_out_failure()` calls its `confirm` with no arguments, so
        # the target is bound here, where the dependency is visible,
        # rather than captured from this scope.
        provider, functools.partial(confirm, target), failure_kind="swept")
    return Outcome(trip, acted, seen["phase"])


def time_box_allowance(time_box, rounds, cap, grace, run_cap):
    """The elapsed time a run may reach before its box is blown, in the
    box's unit.

    `time_box × (1 + min(rounds, cap)) × grace`: the loop gives every
    implementer turn -- the first, and the fix after each review round --
    the ticket's whole budget as its own cap (`loop._timed`), so a run's
    allowance is one box per turn it has actually had. `rounds` is the review
    rounds recorded for the run so far and `cap` the run's review cap, which
    bounds the turns a run can earn: a round past the cap is not a turn the
    loop would give. A run with no round yet is judged exactly as before this
    was counted (KO-340: runs 160 and 161 swept mid-fix at the single box).

    Bounded above by `run_cap` boxes (KO-416): the per-turn formula grows
    with the rounds a run earns by failing review, which is exactly the run
    that most needs a ceiling, so the allowance is the smaller of the two.
    Pure, so the arithmetic is witnessed without a store.
    """
    return min(time_box * (1 + min(rounds, cap)) * grace,
               time_box * run_cap)


def sweep(target, conn, now, act=False, provider=None, knobs=None):
    """Check every live run for a tripped condition; return a `Sweep`.

    `now` is epoch milliseconds and is a parameter, not a clock read: every
    threshold here is an age, and a test that has to sleep to make a run look
    stale is a test that is slow and flaky in exchange for nothing.

    Each swept run is first *sighted*: silent or not, the observation is
    counted in the store, because two consecutive silent sightings are what a
    stale-heartbeat trip is made of and a run seen alive has to clear the
    count it had. The heartbeat goes with the verdict, so a run that answered
    between two sweeps and fell quiet again starts its tally over even though
    no sweep caught it awake. Without `act`, that bookkeeping is the only
    write this makes -- no phase moves, no lease is freed, no ticket is
    touched -- which is what makes a bare `--sweep` safe against a working
    loop. With `act`, every trip is then put to `act_on_trip()`, and what
    each came to -- failed, or declined because the run had moved -- is
    carried as the sweep's `outcomes`, one per trip in the same order.

    A run reports at most one trip, and the conditions are asked in the order
    of what they explain. A stale heartbeat comes first: a dead worker
    explains an overrun and a stalled review both, while neither says
    anything about whether a process is still running. A blown time box
    comes before a stuck review because it is the older and broader budget,
    and a stuck review -- the latest two finished rounds of a run in
    `REVIEW_PHASES` sharing the overlap threshold or more of their
    findings -- is the narrowest: a live run inside its budget whose fix
    round left the reviewer's complaints standing.

    Sightings have a minimum spacing: a silent run whose strike on file is
    younger than the stale threshold is not struck again — the tally it has
    is used as it stands. Two sweeps seconds apart (an operator relaunching,
    a launch straight after a --sweep) are one observation of one silence,
    and counting them separately would let two launches in a minute
    manufacture the second strike the two-strike rule exists to demand of
    two separate silences.

    The whole pass is one `store.transaction()`, because the loop it watches
    is a different process writing the very columns this reads. Classifying
    from a snapshot and then striking in a second transaction leaves a gap in
    which the run heartbeats or finishes, and the strike lands on a state that
    no longer holds -- a live run one sighting nearer a trip it does not
    deserve, or a run that ended a millisecond ago reported as dead. Under one
    `BEGIN IMMEDIATE` the read the verdict is made from and the write it is
    recorded in are the same instant, and a heartbeat arriving mid-sweep waits
    and lands cleanly on the next one. The block is arithmetic over the live
    runs and nothing else, so the loop is held up for no longer than that --
    which is also why acting happens after it has committed rather than
    inside it: failing a run releases leases, escalates a ticket and may call
    Linear, and holding the store's write lock across a network call would
    stall every live loop for as long as the provider takes to answer.

    One condition is the loop's rather than a run's: a self-merge re-exec
    (`loopRestarts`) that no claim, heartbeat or exit note has followed
    within `restart_grace_ms` is a loop that did not come back. It is asked
    in the same transaction, and the store hands each such restart over once
    -- stamping it reported as it does -- so the pass that prints the line is
    the pass that recorded the condition, and the next pass is quiet about it.
    Nothing is relaunched.

    `knobs` is the target's `SweepConfig`; the default is `sweep_config()`,
    the `[supervisor]` table over the module constants.
    """
    knobs = sweep_config(target) if knobs is None else knobs
    stale_ms, strikes_needed = knobs.heartbeat_stale_ms, knobs.stale_strikes
    grace, overlap_threshold = knobs.budget_grace, knobs.review_overlap_threshold
    run_cap = knobs.run_cap
    # The box a run is counted against is the one the loop armed:
    # `budget_min` scaled by `[agents] budget_scale` (`loop._timed()`), so
    # a slower harness's turn is not swept as over its box.
    scale = budget_scale(target)
    trips, watched = [], []
    with store.transaction(conn):
        restarts = tuple(
            (sha, age) for _id, _project, sha, age
            in store.unreturned_loop_restarts(conn, knobs.restart_grace_ms, now))
        swept = store.read.live_runs(conn, SWEEPABLE_PHASES)
        for run in swept:
            run_id, ticket, phase, host = (run.id, run.linearIdentifier,
                                           run.phase, run.host)
            heartbeat, time_box = (run.lastHeartbeat,
                                            run.timeBoxMs
                                            and run.timeBoxMs * scale)
            silent = now - heartbeat
            stale = silent > stale_ms
            on_file = store.read.strike(conn, run_id)
            if (stale and on_file is not None and heartbeat <= on_file.lastSeen
                    and now - on_file.lastSeen < stale_ms):
                # A sighting within one stale-threshold of the last is the
                # same sample: two launches seconds apart must not
                # manufacture the second strike the two-strike rule exists
                # to require of two separate silences. The tally stands.
                strikes = on_file.strikes
            else:
                strikes = store.record_strike(
                    conn, run_id, stale, heartbeat, now)
            elapsed = agent_work(run, now)
            rounds, cap = run.reviewRoundCount, run.reviewRoundCap or MAX_ROUNDS
            turns = 1 + min(rounds, cap)
            if strikes >= strikes_needed:
                trips.append(Trip(
                    run_id, ticket, phase, STALE_HEARTBEAT,
                    f"silent for {silent / 60000:.1f} min"
                    f" over {strikes} consecutive sweeps", heartbeat, host))
            elif time_box and elapsed is not None and elapsed > time_box_allowance(
                    time_box, rounds, cap, grace, run_cap):
                trips.append(Trip(
                    run_id, ticket, phase, TIME_BOX,
                    f"{elapsed / 60000:.1f} min of agent work against a"
                    f" {time_box / 60000:.0f} min box × {turns}"
                    f" {'turn' if turns == 1 else 'turns'} ({grace}x grace,"
                    f" {run_cap}x run cap)",
                    heartbeat, host))
            elif (phase in REVIEW_PHASES
                    and (overlap := review_overlap(conn, run_id)) is not None
                    and overlap[2] >= overlap_threshold):
                earlier, later, shared = overlap
                trips.append(Trip(
                    run_id, ticket, phase, REVIEW_STUCK,
                    f"rounds {earlier} and {later} share {shared:.2f} of"
                    f" their findings ({overlap_threshold} threshold)",
                    heartbeat, host))
            elif strikes:
                # Silent, but one sighting short of a trip: not evidence yet,
                # and not "all healthy" either. Carried for rendering so the
                # operator sees what the next sweep can act on.
                watched.append(
                    f"run {run_id} ({ticket}, {phase}): silent"
                    f" {silent / 60000:.1f} min, strike {strikes} of"
                    f" {strikes_needed} on {host_label(target, host)}")
    outcomes = []
    if act:
        outcomes = [act_on_trip(target, conn, trip, provider, knobs)
                    for trip in trips]
    return Sweep(len(swept), trips, act, tuple(watched), tuple(outcomes),
                 restarts, tuple(merge_lock_lines(target, conn, act)))


# --- the supervisor loop --------------------------------------------------------
# A sweep only helps if something runs it. `--supervise` is the something: one
# process per target that runs the acting sweep, sleeps, and runs it again
# until a signal tells it to stop -- the smallest thing that makes "the
# factory runs overnight and the supervisor watches" true.
#
# The interval between two acting sweeps is `SUPERVISE_INTERVAL_SEC`, or the
# target's `[supervisor] sweep_interval_sec`, read with the other thresholds
# by `sweep_config()`.
# The signals a supervisor stops on. Both mean the same thing here -- finish
# the pass in hand, give the lock back, exit clean -- because an operator's
# Ctrl-C and a service manager's stop are the same request.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# The pid the host sweep beats under: every run is a new process, so its
# `supervisorHeartbeats` row is `(0, since)`, one per store. Never handed to
# `pid_alive()`: `os.kill(0, 0)` signals the caller's own process group and
# always answers alive.
HOST_SWEEP_PID = 0


def supervise_pass(target, pid, started_at, now=None, provider=None, out=None,
                   memory=None):
    """One pass: an acting sweep of the target's store, then a heartbeat.

    The store is opened and closed here rather than held by the loop: a
    connection kept open across a minute's sleep is a reader the WAL cannot
    checkpoint past, and the loop it watches would pay for the supervisor's
    idleness. The heartbeat goes in after the sweep, stamped with the same
    instant, so a reader of `supervisorHeartbeats` who finds a fresh beat
    knows the sweep it vouches for actually ran.

    Prints what `--sweep` would when there is something to say -- a trip or a
    run one strike from one -- and nothing on a healthy pass: a watcher that
    prints "all healthy" once a minute all night has buried the one line
    that mattered by morning. `memory` is the `ReconcileMemory` the caller
    keeps across passes.
    """
    out = out or sys.stdout
    now = int(time() * 1000) if now is None else now
    conn = open_store(target)
    try:
        seen = sweep(target, conn, now, act=True, provider=provider)
        if seen.trips or seen.watched or seen.restarts:
            print("\n".join(sweep_lines(seen, target)), file=out)
        reconcile_parked_pull_requests(target, conn, now, provider, out,
                                       memory=memory)
        store.record_supervisor_heartbeat(conn, pid, started_at, now)
    finally:
        conn.close()
    return seen


def loop_is_live(conn, project, now, stale_ms):
    """Whether a loop is working `project` right now: a run of the project
    in a work phase whose heartbeat is younger than the stale threshold.
    The loop beats through every stage of a run and holds the project's
    lease for as long as one is live, so a fresh beat is the loop; a run
    with no fresh beat is the sweep's business, not evidence of one."""
    phases = ", ".join("?" * len(SWEEPABLE_PHASES))
    return conn.execute(
        f"SELECT 1 FROM runs WHERE projectId = ? AND endedAt IS NULL"
        f" AND phase IN ({phases}) AND lastHeartbeat > ? LIMIT 1",
        (project, *SWEEPABLE_PHASES, now - stale_ms)).fetchone() is not None


def _linear_budget():
    """Load the shared Linear cooldown even before this process has asked
    the board. A test may stub the provider module without a budget."""
    module = sys.modules.get("linear_provider")
    if module is None:
        import linear_provider as module
    return getattr(module, "__dict__", {}).get("LINEAR_BUDGET")


def linear_budget_low(now=None, out=None):
    """Whether a Linear board read waits for the complexity budget's reset
    -- and the one line that says so when it does, printed to `out` once
    per reset the budget names rather than once per pass that waits
    (KO-434). The supervisor's board fallback and the loop's asks --
    `_mirror_queue()`'s idle relisting and the serial pass's claim --
    both skip under this rule."""
    budget = _linear_budget()
    if budget is None or not budget.low(now):
        return False
    line = budget.notice(now)
    if line is not None:
        print(f"[holo2] {line}", file=out or sys.stdout)
    return True


def board_ready(conn, project, provider, out, now=None, board_ask_ms=None):
    """Count ready board issues owed a loop when the store has no ready work.

    No mirror row, or `ready` with no active run, is owed a start (KO-411).
    Only edited `needs_spec`/`blocked_on_deps` rows are re-mirrored through
    the loop's validation; a fresh `ready` row is owed a start (KO-472).
    Parked, running and terminal statuses remain untouched (KO-420).

    No provider or a failed listing returns zero. The low Linear budget
    guard and pre-listing `boardAskedAt` stamp bound reads (KO-434).
    Open mirrors also refresh board state by identifier, including tickets
    absent from the ready listing because the operator shelved them.
    """
    from holophyte.admission import held_line
    if provider is None or held_line(conn, project):
        return 0
    now = int(time() * 1000) if now is None else now
    if linear_budget_low(now, out):
        return 0
    deadline.check("the board's ready listing")
    if board_ask_ms is None:
        board_ask_ms = BOARD_ASK_SEC * 1000
    if project is not None:
        row = conn.execute(
            "SELECT boardAskedAt FROM projects WHERE id = ?",
            (project,)).fetchone()
        asked_at = row[0] if row is not None else None
        if asked_at is not None and now - asked_at < board_ask_ms:
            return 0
        with store.transaction(conn):
            store.stamp_board_ask(conn, project, now)
    try:
        issues = provider.ready_issues()
    except Exception as e:  # noqa: BLE001 - never a strike, never the pass
        print(f"[holo2] the board could not be asked for its ready tickets"
              f" ({e}); the next pass asks again", file=out)
        return 0
    refresh_board_states(conn, project, provider)
    return sum(_board_issue_owed(conn, project, issue, out) for issue in issues)


def _board_issue_owed(conn, project, issue, out):
    """Refresh only edited, unowned refusals on the existing board ask."""
    with store.transaction(conn):
        row = conn.execute(
            "SELECT status, activeRunId, mirroredAt FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (mirror_key(issue), project)).fetchone()
        if row is None:
            return True
        status, active_run, mirrored_at = row
        updated_at = issue.get("updatedAt")
        if (status in ("needs_spec", "blocked_on_deps")
                and updated_at is not None and updated_at > mirrored_at):
            repo = conn.execute("SELECT repoPath FROM projects WHERE id = ?",
                                (project,)).fetchone()[0]
            ticket_id = mirror_task(conn, project, issue,
                                    specced=body_problem(issue, repo) is None)
            fresh = store.read.ticket_by_id(conn, ticket_id)
            print(f"[holo2] re-mirrored {issue['id']}: {status} -> {fresh.status}",
                  file=out)
            status, active_run = fresh.status, fresh.activeRunId
        return status == "ready" and active_run is None


# What a watcher remembers between passes that no store column holds:
# `mirror_asked`, when it last asked the board which of a store's mirrored
# tickets it has closed, by project id (KO-723), `failed_asked`, when it
# last read each failed run's pull request, by run id (KO-722), and
# `states_asked`, when it last asked a store-mode board its open tickets'
# states, by project id (KO-739). One per store: the project form keeps its
# own for its life, the host sweep loads each store's from `sweep.json` and
# writes it back after each project.
ReconcileMemory = collections.namedtuple(
    "ReconcileMemory", ("mirror_asked", "failed_asked", "states_asked"))


def fresh_memory():
    return ReconcileMemory({}, {}, {})


def reconcile_board_closes(conn, project, provider, target, now, out,
                           board_ask_ms, asked):
    """Walk the tickets the board closed while no loop ran (KO-723): the
    loop's own `_reconcile_mirror()`, which otherwise runs only at a loop's
    startup, so a ticket the maintainer landed outside its pull request or
    cancelled stayed open in the store until some loop started. Asked at
    most once per `board_ask_ms` per project, only when the project has an
    open ticket with no active run, and not while the Linear budget is low;
    `asked` is the store's `ReconcileMemory.mirror_asked`. Anything it
    raises is one printed line -- never a strike, never the pass."""
    from holophyte.reconcile import _reconcile_mirror

    if provider is None:
        return
    asked_at = asked.get(project)
    if asked_at is not None and now - asked_at < board_ask_ms:
        return
    if not any(t.activeRunId is None
               for t in store.read.open_tickets(conn, project)):
        return
    if linear_budget_low(now, out):
        return
    deadline.check("the board's closed tickets")
    previous = asked.get(project)
    asked[project] = now
    try:
        with contextlib.redirect_stdout(out):
            _reconcile_mirror(conn, project, provider, target)
    except Exception as e:  # noqa: BLE001 - never a strike, never the pass
        print(f"[holo2] closed board tickets could not be reconciled"
              f" ({e}); a later pass asks again", file=out)
    finally:
        # A sweep deadline cut the ask short, perhaps before the board was
        # asked what it closed: the next pass asks again, unthrottled.
        if deadline.spent() and previous is None:
            del asked[project]
        elif deadline.spent():
            asked[project] = previous


def reconcile_parked_pull_requests(target, conn, now, provider=None, out=None,
                                   knobs=None, memory=None):
    """Land the pull requests a person merged while no loop was running
    (KO-372): the loop's own `_reconcile_pull_requests()`, called from the
    supervisor's pass for every project of the store no live loop is
    working.

    A run parked on its pull request is closed out when the pull request
    is merged on GitHub, but the loop only asks at its startup and once a
    tick, and a pull-request target's loop exits as soon as the board has
    no ready ticket: a merge after that sat in the store, the ticket
    `blocked_on_operator` and the console saying so, until somebody
    relaunched by hand. The supervisor is always up, so its pass asks
    instead -- the same function, one call site, printing the same lines
    to the supervisor's `out` -- and skips a project whose loop is live
    (`loop_is_live()`), because that loop's tick is already asking. The
    reconcile's own rate budget and poll interval bound the cost exactly
    as in the loop. A GitHub error is the reconcile's one printed line per
    ticket; anything else it raises is printed here and the pass goes on
    to its heartbeat, so nothing about GitHub ever counts as a strike or
    ends a pass. The board's closes follow the same way: the mirror
    reconcile runs too, throttled per project (`reconcile_board_closes()`).

    Ready store tickets are owed a loop. A mirror miss asks the board, with
    its poll throttle and Linear budget gate, excluding already parked or
    leased tickets. A fresh heartbeat or held lease turn prevents a launch.
    Startup route failures persist on the project: future deadlines skip
    starts silently, and due retries probe before starting (KO-466).
    Successful starts record launch_loop; systemctl failures leave an event.
    The board fallback asks only for a team that already has a row: the
    sweep writes no `projects` row, so a store without one is left alone.

    `memory` is the store's `ReconcileMemory`, a fresh one when omitted.
    Returns the project ids reconciled.
    """
    # In the function, not at the top: `holophyte.loop` imports this module.
    from holophyte.reconcile import _reconcile_pull_requests

    out = out or sys.stdout
    knobs = sweep_config(target) if knobs is None else knobs
    memory = fresh_memory() if memory is None else memory
    asked = []
    owed = []
    live = False
    for (project,) in conn.execute(
            "SELECT id FROM projects WHERE admission = 'enabled' ORDER BY id"):
        # A store-mode board is asked about live tickets too (KO-739).
        if provider is not None and not linear_budget_low(now, out):
            observe_board(target, conn, project, provider, now, out,
                          memory.states_asked, knobs.board_ask_ms)
        if loop_is_live(conn, project, now, knobs.heartbeat_stale_ms):
            live = True
            continue
        try:
            with contextlib.redirect_stdout(out):
                _reconcile_pull_requests(target, conn, project, provider,
                                         failed_asked=memory.failed_asked)
        except Exception as e:  # noqa: BLE001 - never a strike, never the pass
            print(f"[holo2] parked pull requests could not be reconciled"
                  f" ({e}); the next pass asks again", file=out)
        else:
            asked.append(project)
        reconcile_board_closes(conn, project, provider, target, now, out,
                               knobs.board_ask_ms, memory.mirror_asked)
        # Owed however it got there -- this pass's send-back, an
        # operator's --requeue or --babysit, a ticket filed while the
        # loop was down -- and asked even when GitHub could not be: the
        # ready rows are the mirror's, not the reconcile's. In store mode
        # the store's queue, synced here while no loop is live.
        owed.extend(tickets_owed(target, conn, project, provider, now, out,
                                 knobs))
    # The mirror is a cache of the board, and a ticket that became ready
    # while no loop ran has no row in it: an empty mirror falls through
    # to the board itself (KO-411). A live loop asks the board on its own
    # tick, so the read is only for a target with no loop at all; the
    # answer's surviving issues carry no mirror row and no run, (None,
    # None) apiece. The board's mirror rows live under the provider's
    # team -- the key `ensure_project()` mirrors them by -- and that row is
    # where `board_ready()` stamps its ask, so `board_ask_sec` holds. The
    # sweep never writes the row: a team with none (a store recreated
    # after `project add`) is not asked about until a loop or `project
    # add` writes it.
    board_project = None
    if (not owed and not live and provider is not None
            and not store_mode(target)):
        row = conn.execute("SELECT id FROM projects WHERE linearTeamId = ?",
                           (provider.team,)).fetchone()
        board_project = row[0] if row is not None else None
        if board_project is not None:
            owed = [(None, None)] * board_ready(
                conn, board_project, provider, out, now=now,
                board_ask_ms=knobs.board_ask_ms)
    if owed and not linear_budget_low(now, out) and not lease_turn_held(target):
        start_loop_for(target, conn, owed, now, out, project_id=board_project)
    return asked



def launch_route_ready(target, conn, project, run_id, now, out):
    """Wait out backoff, then probe a usable primary or fallback before launch."""
    from holophyte.agents import probe_diagnostic, probe_implementer, probe_seat
    from store import launch_backoff

    state = launch_backoff.current(conn, project)
    if state and state["until"] > now:
        return False
    if state and state["interval"] == 0:
        reason = state["reason"]
    else:
        probe = probe_implementer(target)
        if (probe is not None and not probe.ok and
                (target.config().get("agents") or {}).get("implementer_fallback")):
            probe = probe_seat(target, "implement", fallback=True)
        if probe is None or probe.ok:
            launch_backoff.clear(conn, project)
            return True
        reason = probe_diagnostic(target, probe)
    note = launch_backoff.failure(conn, project, reason, now, run_id=run_id)
    print("[holo2] " + " ".join(note.splitlines()), file=out)
    return False


def start_loop_for(target, conn, owed, now, out, project_id=None):
    """Probe the route, respecting persistent backoff, then start one unit.

    Attempts land before systemctl. Successful starts retain the existing
    per-run launch record; before the first claim, evidence belongs to the
    project. A failed probe records one backoff step and starts nothing.
    """
    from store import launch_backoff

    project, run_id = launch_backoff.owed_project(conn, owed, project_id)
    deadline.check("the implementer route probe")
    if project and not launch_route_ready(
            target, conn, project[0], run_id, now, out):
        return
    unit = LOOP_UNIT + serve_config(target).name
    count = len(owed)
    noun = "ticket" if count == 1 else "tickets"
    attempt = (f"the supervisor is starting {unit} for {count} {noun}"
               " ready while no loop is live")
    with store.transaction(conn):
        for _ticket, run_id in owed:
            if run_id is not None:
                store.record_event(conn, run_id, "launch_loop_attempt",
                                   attempt, now=now)
        if project and all(run is None for _ticket, run in owed):
            launch_backoff.event(
                conn, project[0], "launch_loop_attempt", attempt, now)
    unit, ok, detail = start_loop(serve_config(target).name)
    if not ok:
        with store.transaction(conn):
            for _ticket, run_id in owed:
                if run_id is not None:
                    store.record_event(conn, run_id, "launch_loop_failed",
                                       f"{unit} could not be started"
                                       f" ({detail}); the next pass tries"
                                       " again", now=now)
        print(f"[holo2] {count} {noun} ready and no loop live, but"
              f" {unit} could not be started ({detail}); the next pass"
              " tries again", file=out)
        return
    note = (f"the supervisor started {unit} for {count} {noun} ready"
            " while no loop was live")
    with store.transaction(conn):
        for _ticket, run_id in owed:
            if run_id is not None:
                store.record_intervention(conn, run_id, "launch_loop", note,
                                          source="supervisor", now=now)
        if project and all(run is None for _ticket, run in owed):
            launch_backoff.intervention(
                conn, project[0], "launch_loop", note, now)
    print(f"[holo2] {count} {noun} ready and no loop live; started"
          f" {unit}", file=out)


def factory_revision():
    """The `HEAD` of the checkout the `holophyte` package is imported from,
    or None where that directory is not a git checkout.

    The factory checkout, not the target: for a self-hosted target they are
    the same directory, but for any other target the supervisor's code lives
    somewhere the target's `HEAD` says nothing about -- which is exactly the
    supervisor that went unnoticed for an hour after a self-merge bumped the
    store schema out from under it.
    """
    checkout = Path(holophyte.__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                              capture_output=True, text=True, check=False)
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


# The refusal `store.open()` raises for a store a newer build has stamped:
# a `SystemExit` whose message says the schema is newer than this build's,
# which the host sweep lists as that project's error.
NEWER_SCHEMA = "newer than the version"


def supervise(target, provider=None, interval=None, wait=None, out=None):
    from holophyte.admission import disabled_startup
    if disabled_startup(target, out):
        return
    return _supervise(target, provider, interval, wait, out)


def _supervise(target, provider=None, interval=None, wait=None, out=None):
    """`PROJECT --supervise`'s whole body: lock, sweep, sleep, repeat until
    a signal.

    The lock is taken before the first pass and given back on every way out
    -- a signal, a pass that raised -- so a supervisor that dies leaves the
    target free for the next one, and the one case the lock stays behind is
    a process killed without the chance, which `acquire_supervisor_lock()`'s
    dead-pid reclaim is for. The signal handlers set a flag the loop reads
    rather than raising into whatever the pass was doing, so a signal that
    lands mid-sweep lets the sweep's transaction finish and the pass that
    was in hand is a whole pass or none. The previous handlers are put back
    afterwards because this is a mode of a module other code imports, not
    the process's only occupant.

    No re-exec: the process runs the code it started with. A store a newer
    build has stamped past this build's reach refuses the pass's open with
    `store.open()`'s `SystemExit`, which ends the process with that message
    for its service manager (`Restart=on-failure`) to start again on the
    new code. Three passes in a row that find the store locked or corrupt
    end it the same way, exit 1.

    `wait` is the sleep, injectable so a test can drive the loop without
    one; it is called with the interval and its result is ignored. The
    default waits on the stop flag itself, so a signal ends the sleep at
    once instead of a minute later. `interval` defaults to the target's
    `[supervisor] sweep_interval_sec`.
    """
    out = out or sys.stdout
    interval = (sweep_config(target).sweep_interval_sec if interval is None
                else interval)
    pid = os.getpid()
    started_at = int(time() * 1000)
    path = acquire_supervisor_lock(supervisor_lock_path(target), target.path,
                                   pid, started_at, target=target)
    stop = threading.Event()
    wait = stop.wait if wait is None else wait
    memory = fresh_memory()

    def on_signal(signum, _frame):
        stop.set()

    skipped = 0
    previous = {signum: signal.signal(signum, on_signal)
                for signum in STOP_SIGNALS}
    try:
        print(f"[holo2] supervising {target.path} as pid {pid} on"
              f" {host_label(target, socket.gethostname())}: acting sweep"
              f" every {interval}s,"
              f" lock at {path}", file=out)
        while not stop.is_set():
            try:
                supervise_pass(target, pid, started_at, provider=provider,
                               out=out, memory=memory)
            except sqlite3.OperationalError as exc:
                skipped += 1
                next_step = (f"next pass in {interval}s" if skipped < 3 else
                             "exiting after 3 consecutive skipped passes")
                print("[holo2] supervisor pass skipped: store unavailable"
                      f" ({exc}); {next_step}", file=out)
                if skipped >= 3:
                    return 1
            else:
                skipped = 0
            wait(interval)
        print("[holo2] supervisor stopping on signal; lock released",
              file=out)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        release_supervisor_lock(path, pid)
    return 0


def supervisor_liveness_line(target, conn=None, now=None):
    """Describe supervisor liveness for loop startup output.

    Uses the same heartbeat threshold as the supervisor and console.
    A missing heartbeat is distinguished from a stale one."""
    now = int(time() * 1000) if now is None else now
    owned = conn is None
    if owned:
        if not target.store_path.exists():
            return "supervisor: none recorded"
        conn = open_store(target)
    try:
        beat = store.latest_supervisor_heartbeat(conn)
    finally:
        if owned:
            conn.close()
    if beat is None:
        return "supervisor: none recorded"
    pid, _started_at, last_beat, _passes, host = beat
    age = now - last_beat
    state = ("live" if age < sweep_config(target).heartbeat_stale_ms
             else "stale")
    # Pid 0 is the host sweep's sentinel row: one per store, bumped by
    # every run, and no process to name.
    who = "host sweep" if pid == HOST_SWEEP_PID else f"pid {pid}"
    return (f"supervisor: {state}, last heartbeat {format_age(age)} ago"
            f" ({who} on {host_label(target, host)})")

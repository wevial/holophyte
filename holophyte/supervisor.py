"""The supervisor: the stale-run sweep, its report, the lock and the loop.

The second process the factory runs, and the one that must keep working
while the loop is down. `sweep()` reads the live runs under one transaction,
counts strikes and returns a `Sweep` of `Trip`s; `act_on_trip()` fails a
tripped run through the loop's own `close_out_failure()` once
`still_tripped()` agrees the verdict survives; `sweep_lines()` and its
halves render a pass and `sweep_report()` is `--sweep`'s whole body. The
supervisor loop -- `supervise()`, one `supervise_pass()` per interval, held
to one process per target by `acquire_supervisor_lock()` and its helpers,
refused with `SupervisorHeld` -- is `--supervise`'s. `supervisor_liveness_line()`
is `--report`'s line about that process, read from the heartbeat rows
`supervise_pass()` writes. `supervise()` also watches the factory's own
code: `factory_revision()` is the checkout's `HEAD`, read once at startup and
again before each pass, and a supervisor whose code has moved -- or whose
store a newer build has stamped -- releases its lock and re-executes itself
through the `EXEC` seam rather than exiting. Beyond the standard library it
imports `store` and `store.read` for the rows, `open_store` from
`holophyte.runs`, `reexec_self` from `holophyte.reexec`, `close_out_failure`
from `holophyte.board`, `sweep_config` from `holophyte.config_tables`, and
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
import subprocess
import sys
import threading
from pathlib import Path
from time import time

import holophyte
import store
import store.read
from holophyte.board import close_out_failure, lease_turn_held, mirror_key
from holophyte.config import budget_scale, serve_config
from holophyte.config_tables import BOARD_ASK_SEC, sweep_config
from holophyte.reexec import LOOP_UNIT, reexec_self, start_loop
from holophyte.report import format_age, host_label
from holophyte.runs import MAX_ROUNDS, open_store
from holophyte.supervisor_lock import (
    acquire_supervisor_lock,
    release_supervisor_lock,
    supervisor_lock_path,
)
from holophyte.sweep_report import merge_lock_lines, sweep_lines

# How the supervisor restarts itself when the factory's code moves under it:
# the process image is replaced, never a module reloaded. A seam so tests can
# see the decision without exec-ing the test runner.
EXEC = os.execv

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
    two such rounds, or when either round found nothing: two empty rounds
    score 1.0 by the measure's definition (equal sets), but a `pass` after a
    `pass`, or a round after an approval, is a review that has nothing left
    to say rather than one repeating itself, and reading the sentinel
    fingerprint as overlap would trip every run whose review went well.

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
    if not earlier_findings or not later_findings:
        return None
    return earlier, later, store.findings_overlap(earlier_findings,
                                                  later_findings)


def still_tripped(target, conn, trip, knobs=None):
    """Does `trip`'s verdict still hold of the run it was reached on?

    Asked again at the moment of acting, under the write lock, because the
    classification that produced the trip committed and let the run's own
    process back in. What that process may have done since is the whole
    question: a run that ended is already closed out and must not be re-ended
    over the top of its real outcome, and a run that moved on is doing
    something and can wait for the sweep after this one -- the tally that
    tripped it survives, so a run that is really gone trips again a minute
    later, which is a cheap price for never failing a live one.

    A stale heartbeat asks one thing more: that `lastHeartbeat` is still the
    timestamp the verdict was read from. Any beat at all is the run answering
    the only question the condition asked, and a run that answered is alive
    however long it was quiet before. A blown time box asks the opposite --
    an overrunning run heartbeats, that is what makes it an overrun rather
    than a death -- so a fresh beat is no acquittal there and is not treated
    as one. A stuck review is alive too, so its heartbeat says nothing; what
    it asks instead is that the overlap still holds, recomputed over whatever
    rounds are on file now. The phase alone cannot tell: a run that went
    through `addressing` and back has a new finished round and the phase the
    verdict named, and if that round cleared the reviewer's complaints the
    review has moved and the run is acquitted. If it repeats them, the run
    is the same stuck review with one more round on file, and the verdict
    stands even though the rounds it now rests on are later than the ones
    the evidence names. And a run that left the phase and came back with no
    new round -- through `addressing` and `verifying` into its terminal
    adjudication -- is exactly the run the condition names: the adjudication
    is what the trip is meant to spare paying for, and a sweep arriving
    fresh at that moment would trip it on the same two rounds.

    `knobs` is the `SweepConfig` the verdict was reached under, so the
    overlap is re-asked against the threshold that tripped it.
    """
    knobs = sweep_config(target) if knobs is None else knobs
    run = store.read.run_snapshot(conn, trip.run_id)
    if run is None:
        return False
    if run.endedAt is not None or run.phase != trip.phase:
        return False
    if trip.condition == STALE_HEARTBEAT:
        return run.lastHeartbeat == trip.heartbeat
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

    A decline is recorded too, under the same event kind: the supervisor
    looked, found the run finished or moved on, and stood down. Without the
    row a reader of the run's stream cannot tell a sweep that declined from
    one that never arrived, and the summary line the operator reads is
    derived from this same answer -- `acted` here is what happened, never
    the flag the sweep was called with.

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
        return True

    acted = close_out_failure(
        target, conn, trip.run_id, ticket_id,
        f"swept by the supervisor in phase {trip.phase}: {trip.condition}"
        f" ({trip.evidence}); branch and worktree preserved for a human",
        # `close_out_failure()` calls its `confirm` with no arguments, so
        # the target is bound here, where the dependency is visible,
        # rather than captured from this scope.
        provider, functools.partial(confirm, target))
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
            heartbeat, started, time_box = (run.lastHeartbeat, run.startedAt,
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
            elapsed = now - started
            rounds, cap = run.reviewRoundCount, run.reviewRoundCap or MAX_ROUNDS
            turns = 1 + min(rounds, cap)
            if strikes >= strikes_needed:
                trips.append(Trip(
                    run_id, ticket, phase, STALE_HEARTBEAT,
                    f"silent for {silent / 60000:.1f} min"
                    f" over {strikes} consecutive sweeps", heartbeat, host))
            elif time_box and elapsed > time_box_allowance(
                    time_box, rounds, cap, grace, run_cap):
                trips.append(Trip(
                    run_id, ticket, phase, TIME_BOX,
                    f"{elapsed / 60000:.1f} min against a"
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


def supervise_pass(target, pid, started_at, now=None, provider=None, out=None):
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
    that mattered by morning.
    """
    out = out or sys.stdout
    now = int(time() * 1000) if now is None else now
    conn = open_store(target)
    try:
        seen = sweep(target, conn, now, act=True, provider=provider)
        if seen.trips or seen.watched or seen.restarts:
            print("\n".join(sweep_lines(seen, target)), file=out)
        reconcile_parked_pull_requests(target, conn, now, provider, out)
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
    """The process's Linear complexity budget, or None when the provider
    module was never imported -- no Linear answer has been seen, so nothing
    is spent -- or a test stubbed the module without one."""
    module = sys.modules.get("linear_provider")
    if module is None:
        return None
    return getattr(module, "__dict__", {}).get("LINEAR_BUDGET")


def linear_budget_low(now=None, out=None):
    """Whether a Linear board read waits for the complexity budget's reset
    -- and the one line that says so when it does, printed to `out` once
    per reset the budget names rather than once per pass that waits
    (KO-434). The supervisor's board fallback and the loop's idle
    relisting (`_mirror_queue()`) both skip under this rule."""
    budget = _linear_budget()
    if budget is None or not budget.low(now):
        return False
    line = budget.notice(now)
    if line is not None:
        print(f"[holo2] {line}", file=out or sys.stdout)
    return True


def board_ready(conn, project, provider, out, now=None, board_ask_ms=None):
    """How many of the board's ready issues are owed a loop, or 0 when it
    cannot be asked or has none: the KO-411 fall-through for a mirror
    that has no row for the ticket at all, less the rows the mirror
    already holds in a non-ready status (KO-420).

    The store mirrors a ticket only once a loop pass has seen it, so a
    ticket that became ready while no loop ran -- one filed with
    `--file-ticket`, one moved from Backlog to Todo -- has no row for
    `ready_tickets()` to find. An empty mirror therefore asks the board
    the same question the loop's claim asks, through the provider the
    pass was handed; for the Linear board that call is
    `linear_provider.ready_issues()` on the target's `[board]
    project_id`. The board's answer is its whole ready column, though,
    and that column keeps a ticket the store holds `blocked_on_operator`,
    `in_flight` or terminal -- the board never learns about a park, so a
    ticket parked on its pull request counted forever, and every pass
    started a loop whose claim could only refuse it (KO-420). Each issue
    is therefore checked against the row `mirror_task()` mirrors it
    under, `mirror_key()`'s `linearIssueId` in the board's project: no
    row, or a row still `ready` with no live run holding it, is owed a
    start; any other status is the loop's own claim to refuse and is not
    owed one. A board that cannot be asked is one printed line and a
    "no", as the reconcile's GitHub errors are: the next pass asks again.
    No provider is no board to ask, and asks nothing.

    Two guards spend the listing only on purpose (KO-434): Linear's
    complexity budget low -- under a tenth of its limit -- waits for the
    reset it names, once per reset rather than once per pass; and the
    last ask stamped on the project's row holds the next off for
    `board_ask_ms`, so a mirror that stays empty is not re-asked every
    sweep interval. The stamp lands before the ask rather than after it,
    so an ask that failed still spent the interval's one listing.
    """
    if provider is None:
        return 0
    now = int(time() * 1000) if now is None else now
    if linear_budget_low(now, out):
        return 0
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
            conn.execute("UPDATE projects SET boardAskedAt = ? WHERE id = ?",
                         (now, project))
    try:
        issues = provider.ready_issues()
    except Exception as e:  # noqa: BLE001 - never a strike, never the pass
        print(f"[holo2] the board could not be asked for its ready tickets"
              f" ({e}); the next pass asks again", file=out)
        return 0
    owed = 0
    for issue in issues:
        row = conn.execute(
            "SELECT status, activeRunId FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (mirror_key(issue), project)).fetchone()
        if row is None or (row[0] == "ready" and row[1] is None):
            owed += 1
    return owed


def reconcile_parked_pull_requests(target, conn, now, provider=None, out=None,
                                   knobs=None):
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
    ends a pass.

    Every pass then ends by asking the store whether any ticket is
    `ready` while no loop is live, and starts the target's loop unit
    through `start_loop()` -- the call the daemon's `launch-loop` action
    makes -- when so, printing that it did (KO-376, widened by KO-409):
    the loop exits on a board with no ready issue, so a ticket that
    becomes ready while no loop is running waits for a hand on the
    launcher otherwise. The reconcile's send-back is one way a ticket
    becomes ready; an operator's `--requeue` or `--babysit`, a ticket
    filed while the loop was down and a `--file-ticket --update` that
    turned a spec into a contract are the others, and all are owed the
    same start. What is owed a loop is read from the store
    (`store.read.ready_tickets()`): a ticket `ready` with no live run,
    whatever its newest run's history. That answer is the mirror's, and
    the mirror is a cache of the board: a ticket that became ready while
    no loop ran has no row for it, so an empty answer falls through to
    the board itself (`board_ready()`), the same `ready_issues()` read
    the loop claims from, and a non-empty answer is owed the same start
    (KO-411) -- less the issues the mirror already holds in a non-ready
    status, because the board's ready column keeps a ticket the store
    holds parked or leased, the board never learning about the park, and
    counting it relaunched a loop every pass only for the claim to refuse
    it (KO-420). The ask runs only on the miss and never while a loop is
    live to ask on its own tick, so a busy target pays nothing and an
    idle one at most one listing per `board_ask_sec` -- and none at all
    while Linear's complexity budget is under its tenth, which waits for
    the reset it names instead (KO-434). A board that cannot be asked is
    one printed line and a "no", as the reconcile's GitHub errors are. A
    start `systemctl` took is
    recorded as a `launch_loop` row on that run, and a start that failed
    is one printed line and no row; neither mark decides the next pass,
    which finds a ticket still `ready` with no loop live owed again --
    a refused start raised no loop and a taken one raised a loop that
    never claimed. Once per pass, however many tickets are owed: the
    unit is the target's, and a `systemctl start` on a unit already
    running is nothing. "No loop live" is two looks: no fresh heartbeat
    on a run of the project
    (`loop_is_live()`) and nobody holding the lease turn
    (`lease_turn_held()`), the flock a claim or close-out of this store
    holds between its look and its write -- a loop between its startup
    and its first claim's heartbeat is visible only there. A sweep that
    has nothing owed starts nothing.

    Returns the project ids reconciled.
    """
    # In the function, not at the top: `holophyte.loop` imports this module.
    from holophyte.reconcile import _reconcile_pull_requests

    out = out or sys.stdout
    knobs = sweep_config(target) if knobs is None else knobs
    asked = []
    owed = []
    live = False
    for (project,) in conn.execute("SELECT id FROM projects ORDER BY id"):
        if loop_is_live(conn, project, now, knobs.heartbeat_stale_ms):
            live = True
            continue
        try:
            with contextlib.redirect_stdout(out):
                _reconcile_pull_requests(target, conn, project, provider)
        except Exception as e:  # noqa: BLE001 - never a strike, never the pass
            print(f"[holo2] parked pull requests could not be reconciled"
                  f" ({e}); the next pass asks again", file=out)
        else:
            asked.append(project)
        # Owed however it got there -- this pass's send-back, an
        # operator's --requeue or --babysit, a ticket filed while the
        # loop was down -- and asked even when GitHub could not be: the
        # ready rows are the mirror's, not the reconcile's.
        owed.extend(store.read.ready_tickets(conn, project))
    # The mirror is a cache of the board, and a ticket that became ready
    # while no loop ran has no row in it: an empty mirror falls through
    # to the board itself (KO-411). A live loop asks the board on its own
    # tick, so the read is only for a target with no loop at all; the
    # answer's surviving issues carry no mirror row and no run, (None,
    # None) apiece. The board's mirror rows live under the provider's
    # team -- the key `ensure_project()` mirrors them by -- and ensuring
    # the row here gives `board_ready()` somewhere to stamp its ask, so
    # `board_ask_sec` holds even for a board nothing has mirrored yet.
    if not owed and not live:
        board_project = None
        if provider is not None:
            board_project = store.ensure_project(conn, provider.team,
                                                 target.path)
        owed = [(None, None)] * board_ready(
            conn, board_project, provider, out, now=now,
            board_ask_ms=knobs.board_ask_ms)
    if owed and not lease_turn_held(target):
        start_loop_for(target, conn, owed, now, out)
    return asked


def start_loop_for(target, conn, owed, now, out):
    """Start the target's loop unit for the `(ticket, run)` pairs `owed` a
    loop, printing the unit started or why it was not.

    Record before acting: a `launch_loop_attempt` event lands on each run
    and is committed before `systemctl` is asked, so a supervisor that
    dies between the ask and the answer still left the store saying it
    tried. A start `systemctl` took is then recorded as a `launch_loop`
    intervention on each run; a refused one records its refusal as a
    `launch_loop_failed` event and no intervention. Neither mark
    discharges the owing: the next pass reads the ticket's `ready` and
    the loop's liveness, not the record, so a start whose loop never
    came live is tried again -- one more `systemctl start` on a unit
    already running or dead, which is nothing.

    The run of a pair is the ticket's newest; a ticket no run has claimed
    yet (filed while the loop was down, say) carries None and gets no
    row -- record-before-acting has no run stream to write on -- but it
    is counted and the unit is started for it all the same. A ticket the
    board holds ready that the mirror has no row for at all (KO-411)
    arrives as `(None, None)` -- no ticket to name, no run to write on --
    and is likewise counted and started for.
    """
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
                                          source="supervisor",
                                          trigger="manual", now=now)
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
# a `SystemExit` whose message says the schema is newer than this build's.
NEWER_SCHEMA = "newer than the version"


def supervise(target, provider=None, interval=None, wait=None, out=None):
    """`--supervise`'s whole body: lock, sweep, sleep, repeat until a signal.

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

    The factory's own code is watched too. `factory_revision()` is read once
    here and again before every pass; when it has moved -- a self-merge on
    this host -- the supervisor prints the two revisions, releases its lock
    and replaces itself with the same command line through `EXEC`, so the
    fresh process takes the lock and carries on from the new code. A pass
    whose store open refuses a newer schema (`store.open()`'s `SystemExit`)
    does the same instead of exiting: the net that guards every other
    target on the host must not end without a sound over the one event the
    loop already restarts itself for. A stop request wins over a pending
    re-exec, and the exec never happens from inside a signal handler.

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
    started_from = factory_revision()
    stop = threading.Event()
    wait = stop.wait if wait is None else wait

    def on_signal(signum, _frame):
        stop.set()

    def reexec(reason):
        # The lock first: the fresh process must find it free, and nothing
        # can be released after the exec has replaced this process.
        release_supervisor_lock(path, pid)
        reexec_self(reason, EXEC, out)

    previous = {signum: signal.signal(signum, on_signal)
                for signum in STOP_SIGNALS}
    try:
        print(f"[holo2] supervising {target.path} as pid {pid} on"
              f" {host_label(target, socket.gethostname())}: acting sweep"
              f" every {interval}s,"
              f" lock at {path}", file=out)
        while not stop.is_set():
            current = factory_revision()
            if stop.is_set():
                break  # a signal landed in the git call: stop wins
            if current != started_from:
                reexec(f"factory code moved from {started_from} to {current};"
                       " supervisor re-executing")
                return 0  # only a test's EXEC returns
            try:
                supervise_pass(target, pid, started_at, provider=provider,
                               out=out)
            except SystemExit as refused:
                if NEWER_SCHEMA not in str(refused) or stop.is_set():
                    raise
                reexec(f"{refused}; supervisor re-executing")
                return 0  # only a test's EXEC returns
            wait(interval)
        print("[holo2] supervisor stopping on signal; lock released",
              file=out)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        release_supervisor_lock(path, pid)
    return 0


def supervisor_liveness_line(target, conn=None, now=None):
    """One line saying whether a supervisor is live for the target.

    `supervisor: live, last heartbeat 12s ago (pid N on HOST)` when the
    newest beat in `supervisorHeartbeats` is younger than the target's
    `[supervisor] heartbeat_stale_min`, the same boundary the sweep judges
    a run's heartbeat by; `stale` past it; `none recorded` when no
    supervisor has ever beaten -- or, with no `conn` given, when there is
    no store to ask. Read-only: it exists so an operator can tell from
    `--report`, or from a refused `--supervise`, whether the watcher they
    are about to launch is already running.
    """
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
    return (f"supervisor: {state}, last heartbeat {format_age(age)} ago"
            f" (pid {pid} on {host_label(target, host)})")

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
`supervise_pass()` writes. Beyond the standard library it imports `store` and
`store.read` for the rows, `open_store` from `holophyte.runs`,
`close_out_failure` from `holophyte.board`, `sweep_config` from
`holophyte.config`, and `host_label`, `format_age`, `REPORT_GAP` from
`holophyte.report`; nothing from `factory`.

Sixth slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import collections
import contextlib
import fcntl
import functools
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from time import time

import review_runner
import store
import store.read
from holophyte.board import close_out_failure
from holophyte.config import sweep_config
from holophyte.report import REPORT_GAP, format_age, host_label
from holophyte.runs import open_store

# --- the supervisor's stale-run sweep -----------------------------------------
# The loop watches itself only while it is alive. A run whose process crashed,
# hung, or was killed leaves a row in a work phase, a heartbeat that stopped
# and a project lease nobody will ever give back -- and nothing noticed.
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
# three a finished run sits in and `blocked_on_operator`. Derived from
# `store.PHASES` rather than listed, so a phase added there is swept by
# default -- the safe direction for a check whose failure mode is a hung run
# nobody looks at.
#
# `blocked_on_operator` is excluded because a parked run is *supposed* to have
# no heartbeat: the loop wrote the question, released the process and went
# home, and the run waits for a human for however long that takes. Sweeping it
# would report every parked run as dead within five minutes, and 2/5 would
# then fail the one state the design keeps open for an operator's answer.
SWEEPABLE_PHASES = tuple(
    phase for phase in store.PHASES
    if phase not in store.ENDED_PHASES and phase != "blocked_on_operator")

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
                                "outcomes", "restarts"), defaults=((),))
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
                                            run.timeBoxMs)
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
            if strikes >= strikes_needed:
                trips.append(Trip(
                    run_id, ticket, phase, STALE_HEARTBEAT,
                    f"silent for {silent / 60000:.1f} min"
                    f" over {strikes} consecutive sweeps", heartbeat, host))
            elif time_box and elapsed > time_box * grace:
                trips.append(Trip(
                    run_id, ticket, phase, TIME_BOX,
                    f"{elapsed / 60000:.1f} min against a"
                    f" {time_box / 60000:.0f} min box ({grace}x grace)",
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
                 restarts)


SWEEP_HEADERS = ("ticket", "run", "phase", "condition", "evidence", "host")


# Printed under a table with trips in it wherever the reader is an operator
# who did not ask for a sweep (startup, a refused claim): the table says what
# is wrong, this says what to type. `{target}` is filled at the print site so
# the line is copy-pasteable for a non-default target.
SWEEP_HINT = ("[holo2] tripped runs are failed by"
              " `factory.py {target} --sweep --act`;"
              " a bare --sweep re-checks first")


def _runs(n):
    """`n` runs, counted in English -- the summary line reads as a sentence."""
    return "1 run" if n == 1 else f"{n} runs"


def sweep_lines(result, target=None):
    """The sweep as lines: a header, one line per trip, a summary.

    A clean sweep prints what it checked rather than nothing. Empty output is
    ambiguous -- it reads the same as a crashed supervisor, a mistyped target
    or a store with no runs in it -- so the quiet case is an assertion an
    operator can act on, and the three quiet cases say which one they are.

    An acting sweep adds one outcome line per trip and a summary that counts
    the failed apart from the declined. Both come from `Outcome`, which is
    what `act_on_trip()` actually did, and never from the `acted` flag the
    sweep was called with: a re-check that stood down because the run had
    finished is reported as exactly that, naming the status it found, and
    the words "failed and leases released" are printed only for a run whose
    failure was written. A read-only sweep has no outcomes and prints as it
    always has.

    A restart the loop did not come back from is printed first, one line per
    restart naming the sha and how long ago the exec was: it is not about a
    run, so it sits above the run table, and it is printed above the quiet
    lines too, because "no runs in flight" is exactly what a loop that died
    in its exec leaves behind.
    """
    return restart_lines(result) + run_lines(result, target)


def restart_lines(result):
    """One line per self-merge re-exec the loop did not come back from."""
    return [f"loop did not return after re-exec from {sha}:"
            f" no claim, heartbeat or exit note in the {age / 60000:.1f} min"
            " since the exec"
            for sha, age in result.restarts]


def run_lines(result, target=None):
    """`sweep_lines()` less the restart lines: the per-run report.

    `target` supplies the `[report] host_label` the host column shows in
    place of the hostname; without one the column is the hostname itself.
    """
    if not result.swept:
        return ["no runs in flight, nothing to sweep"]
    if not result.trips:
        # A first-strike sighting must not read as health: "none tripped"
        # plus the watched lines is the honest quiet case.
        if result.watched:
            return [f"{_runs(result.swept)} swept, none tripped",
                    *result.watched]
        return [f"{_runs(result.swept)} swept, all healthy"]
    table = [SWEEP_HEADERS]
    table += [(trip.ticket, f"run {trip.run_id}", trip.phase, trip.condition,
               trip.evidence, host_label(target, trip.host))
              for trip in result.trips]
    widths = [max(len(cell) for cell in column) for column in zip(*table)]
    lines = [
        REPORT_GAP.join(cell.ljust(width)
                        for cell, width in zip(row, widths)).rstrip()
        for row in table
    ]
    for outcome in result.outcomes:
        run_id = outcome.trip.run_id
        if outcome.acted:
            lines.append(f"acted: failed run {run_id}, leases released")
        else:
            status = ("gone" if outcome.phase is None
                      else f"now {outcome.phase}")
            lines.append(f"declined: run {run_id} is {status}; no action")
    lines += list(result.watched)
    failed = sum(1 for outcome in result.outcomes if outcome.acted)
    declined = len(result.outcomes) - failed
    summary = f"{len(result.trips)} tripped of {_runs(result.swept)} swept"
    if failed:
        summary += f", {failed} failed and leases released"
    if declined:
        summary += f", {declined} declined, no action"
    return lines + [summary]


def sweep_report(target, conn=None, now=None, out=None, act=False, provider=None):
    """Print the target store's tripped runs, failing them when `act`.

    `--sweep`'s whole body, and a sibling of `report()` in what it refuses to
    do: no ticket is claimed and no worktree is cut, so it is safe to run
    against the store of a loop that is still working -- the case it exists
    for. Unlike `report()` it does write, to exactly one table: the strike
    tally `sweep()` keeps, without which "two consecutive sweeps" could not
    span two invocations.

    `act` is what `--act` adds, and it adds it to nothing else: a pass that
    trips no run writes exactly what a read-only pass writes, so acting costs
    nothing on the sweeps that find everything healthy. A pass that does trip
    something fails those runs, and only then is a provider needed -- and only
    if a ticket has reached its escalation threshold.

    The table is printed after the acting rather than before it, so it is a
    record of what happened rather than a promise: a best-effort push that
    warns on its way past appears above the summary claiming the runs were
    failed, not below it.

    A target with no store has no runs to sweep and is reported rather than
    created, the way `--report` answers the same mistake.

    The `review containers` section comes first, so the run summary stays
    the last line: it asks Docker rather than the store, and a reviewer
    leaked by a loop that died is the one thing here the store cannot see.
    """
    out = out or sys.stdout
    if conn is None and not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return
    print("\n".join(review_container_lines(act)), file=out)
    owned = conn is None
    conn = conn if conn is not None else open_store(target)
    try:
        if now is None:
            now = int(time() * 1000)
        print("\n".join(sweep_lines(sweep(target, conn, now, act, provider),
                                     target)),
              file=out)
    finally:
        if owned:
            conn.close()


def review_container_lines(act=False):
    """The `review containers` section: strays listed, and removed when `act`.

    A review container is removed by the loop that started it, on exit or on
    a stop signal; one still running after its scratch directory is gone
    belongs to a loop that died some other way (SIGKILL, a host reset) and
    holds two CPUs, 2 GB and a Codex session until something removes it. A
    container whose scratch directory still exists is a live review and is
    never touched. Without a `docker` to ask, the section says the check was
    skipped rather than claiming a clean host.
    """
    try:
        strays = review_runner.stray_containers()
    except review_runner.ReviewBoundaryError as e:
        return [f"review containers: skipped ({e})"]
    if not strays:
        return ["review containers: none stray"]
    lines = ["review containers:"]
    for name in strays:
        if not act:
            lines.append(f"  stray {name}")
            continue
        try:
            review_runner._remove_container(name)
        except review_runner.ReviewBoundaryError as e:
            lines.append(f"  stray {name}: {e}")
        else:
            lines.append(f"  removed stray {name}")
    return lines


# --- the supervisor loop --------------------------------------------------------
# A sweep only helps if something runs it. `--supervise` is the something: one
# process per target that runs the acting sweep, sleeps, and runs it again
# until a signal tells it to stop -- the smallest thing that makes "the
# factory runs overnight and the supervisor watches" true.
#
# One per target, because two supervisors sweeping one store would each take
# their own sighting of every silence and manufacture between them the second
# strike the two-strike rule exists to demand of two separate silences. The
# arbitration is a lockfile beside the store, taken with an exclusive create
# (v1 TUI mining, server.ts:111-160): create-then-check, never check-then-
# create, because the gap between a check and a create is exactly where a
# second starter slips through. A lock that exists is then read: a live pid
# means a rival and this starter aborts naming it; a dead pid is a supervisor
# that crashed without cleaning up, and its lock is reclaimed; a lock that
# says neither -- empty, half-written, not ours to parse -- is ambiguous, and
# an ambiguous probe never spawns a rival. It aborts and says what it saw.

# The interval between two acting sweeps is `SUPERVISE_INTERVAL_SEC`, or the
# target's `[supervisor] sweep_interval_sec`, read with the other thresholds
# by `sweep_config()`.
# The signals a supervisor stops on. Both mean the same thing here -- finish
# the pass in hand, give the lock back, exit clean -- because an operator's
# Ctrl-C and a service manager's stop are the same request.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def supervisor_lock_path(target):
    """The lockfile for `target`'s supervisor, in its state directory.

    Beside the store rather than inside the target for the store's own
    reason: nothing about the target checkout should have to know the factory
    exists, and a lock inside it is dirt a task's `git add -A` could commit.
    Taken from the `Target`'s state directory so the lock cannot end up
    addressing a different directory from the store it guards.
    """
    return target.holo_dir / "supervisor.lock"


class SupervisorHeld(Exception):
    """The target already has a supervisor, or a lock this one will not take.

    `pid` is the live holder when there is one, and None when the lock could
    not be read -- the two cases the message tells apart, because the first
    is answered by doing nothing and the second by an operator looking at the
    file.
    """

    def __init__(self, path, repo, pid=None, started_at=None, host=None,
                 target=None):
        self.path, self.repo, self.pid = path, repo, pid
        self.started_at, self.host = started_at, host
        if pid is None:
            what = (f"[holo2] supervisor lock {path} exists but names no"
                    " process; refusing to guess. remove it if no supervisor"
                    " is running")
        else:
            since = (f" since {started_at}" if started_at is not None else "")
            what = (f"[holo2] a supervisor is already running for {repo}:"
                    f" pid {pid} on {host_label(target, host)}{since}"
                    f" holds {path};"
                    " not starting another")
        super().__init__(what)


def pid_alive(pid):
    """Whether `pid` names a process that exists, by asking the kernel.

    Signal 0 delivers nothing and answers only whether it could have: a
    process that is gone is ESRCH, one that belongs to someone else is EPERM
    -- and EPERM is still alive, which is the answer that matters here.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_supervisor_lock(path):
    """The `(pid, started_at, host)` a lockfile names, or None if none.

    Written as `host pid started_at` on one line by
    `acquire_supervisor_lock()`. A lock an older supervisor wrote as the two
    integers alone still reads, with `host` None: it is a lock whose dead
    pid can be reclaimed, not a file somebody else wrote. Anything else --
    an empty file a crashed starter left between its create and its write,
    a file somebody else wrote -- is None, and the caller treats None as a
    lock it must not remove.
    """
    try:
        fields = Path(path).read_text().split()
        if len(fields) == 2:
            host, pid, started_at = None, int(fields[0]), int(fields[1])
        else:
            host, pid, started_at = fields[0], int(fields[1]), int(fields[2])
    except (OSError, ValueError, IndexError):
        return None
    return pid, started_at, host


@contextlib.contextmanager
def reclaim_turn(path):
    """Hold the reclaim sidecar of the lock at `path` for the block's span.

    A blocking flock on `<path>.reclaim`, which is never unlinked so every
    starter locks the same inode. It orders reclaims only; the lock itself
    stays the exclusive create, so a starter that never has to reclaim never
    touches the sidecar.
    """
    fd = os.open(f"{path}.reclaim", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def acquire_supervisor_lock(path, repo, pid=None, now=None, host=None,
                            target=None):
    """Take the supervisor lock at `path` for `pid`; raise `SupervisorHeld`.

    `repo` is the repository the lock guards, named in the refusal so the
    operator reading it knows which target already has its supervisor.

    The lock's content is `host pid started_at`, `host` defaulting to this
    machine's hostname: a target's state directory can be read from another
    machine, and a lock naming only a pid then names a process nobody there
    can find.

    The exclusive create is the arbitration: two starters racing here both
    reach the kernel, and the kernel lets one of them through. The loser
    reads the lock the winner wrote and finds a live pid. A lock left by a
    dead supervisor is reclaimed -- unlinked, then created again through the
    same exclusive door, so a reclaim that loses a race to another starter
    loses the way any second starter does. The reclaim itself -- read the
    holder, judge it dead, unlink -- runs under an flock on a sidecar beside
    the lock, so two starters that both read the same dead pid take turns:
    the second finds, once its turn comes, the live lock the first has just
    written, and is refused by it. (An inode compared before the unlink is
    not that guard: between the comparison and the unlink a rival can have
    reclaimed and re-created the file, and the unlink then takes the rival's
    live lock.) A lock naming this very pid is the stale case too: a
    supervisor is not its own rival, and a pid comes round again. Only one
    reclaim is attempted, because a create that fails after it is a starter
    that never needed a turn, not a second stale lock.
    """
    path = Path(path)
    pid = os.getpid() if pid is None else pid
    now = int(time() * 1000) if now is None else now
    host = socket.gethostname() if host is None else host
    # The target's state directory, on first need: a supervisor can be the
    # first thing to run against a target.
    path.parent.mkdir(parents=True, exist_ok=True)

    def created():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{host} {pid} {now}\n")
        return True

    if created():
        return path
    with reclaim_turn(path):
        holder = read_supervisor_lock(path)
        if holder is None and path.exists():
            raise SupervisorHeld(path, repo, target=target)
        if holder is not None:
            if holder[0] != pid and pid_alive(holder[0]):
                raise SupervisorHeld(path, repo, *holder, target=target)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        # Created (and written) before the turn is given up, so the starter
        # waiting for it reads a whole lock, never the empty file between a
        # create and its write.
        if created():
            return path
    holder = read_supervisor_lock(path)
    raise SupervisorHeld(path, repo, *(holder or ()), target=target)


def release_supervisor_lock(path, pid=None):
    """Remove the lock at `path` if it is `pid`'s; leave anyone else's alone.

    Checked before it is removed because the lock may not be ours any more: a
    supervisor that was wrongly judged dead has had its lock reclaimed, and
    removing the reclaimer's lock on the way out would let a third starter
    in beside it.
    """
    pid = os.getpid() if pid is None else pid
    holder = read_supervisor_lock(path)
    if holder is not None and holder[0] == pid:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


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
        store.record_supervisor_heartbeat(conn, pid, started_at, now)
    finally:
        conn.close()
    return seen


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

    def on_signal(signum, _frame):
        stop.set()

    previous = {signum: signal.signal(signum, on_signal)
                for signum in STOP_SIGNALS}
    try:
        print(f"[holo2] supervising {target.path} as pid {pid} on"
              f" {host_label(target, socket.gethostname())}: acting sweep"
              f" every {interval}s,"
              f" lock at {path}", file=out)
        while not stop.is_set():
            supervise_pass(target, pid, started_at, provider=provider, out=out)
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

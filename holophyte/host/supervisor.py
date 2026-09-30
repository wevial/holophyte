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
from holophyte.board.board_sync import observe_board
from holophyte.board.board_sync import owed as tickets_owed
from holophyte.board.projection import (
    body_problem,
    close_out_failure,
    lease_turn_held,
    mirror_key,
    mirror_task,
    refresh_board_states,
)
from holophyte.cli.report import format_age, host_label
from holophyte.config.agent_settings import budget_scale
from holophyte.config.config_tables import BOARD_ASK_SEC, sweep_config
from holophyte.config.serve_settings import serve_config
from holophyte.host.supervisor_lock import (
    acquire_supervisor_lock,
    release_supervisor_lock,
    supervisor_lock_path,
)
from holophyte.host.sweep_report import merge_lock_lines, sweep_lines
from holophyte.loop.claim_store import store_mode
from holophyte.loop.reexec import LOOP_UNIT, start_loop
from holophyte.loop.runs import MAX_ROUNDS, open_store
from store.working import agent_work

REVIEW_PHASES = ("reviewing", "addressing")


# Derived, so a new phase is swept; a parked run waits on a human with no beat.
SWEEPABLE_PHASES = tuple(
    phase for phase in store.PHASES
    if phase not in store.ENDED_PHASES and phase not in store.PARKED_PHASES)

STALE_HEARTBEAT = "stale_heartbeat"
TIME_BOX = "time_box"
REVIEW_STUCK = "review_stuck"

SWEEP_EVENT = "supervisor_sweep"

Trip = collections.namedtuple(
    "Trip",
    ("run_id", "ticket", "phase", "condition", "evidence", "heartbeat",
     "host"), defaults=(None,))
Sweep = collections.namedtuple("Sweep",
                               ("swept", "trips", "acted", "watched",
                                "outcomes", "restarts", "locks"),
                               defaults=((), ()))
Outcome = collections.namedtuple("Outcome", ("trip", "acted", "phase"))


def review_overlap(conn, run_id):
    rounds = store.read.newest_ended_rounds(conn, run_id)
    if len(rounds) < 2:
        return None
    later, earlier = rounds[0].round, rounds[1].round
    earlier_findings = json.loads(rounds[1].findings)
    later_findings = json.loads(rounds[0].findings)
    # Two empty rounds overlap fully, yet an approval is not a repeated review.
    if any(store.findings_fingerprint(findings) == store.EMPTY_FINGERPRINT
           for findings in (earlier_findings, later_findings)):
        return None
    return earlier, later, store.findings_overlap(earlier_findings,
                                                  later_findings)


def still_tripped(target, conn, trip, knobs=None):
    """Recheck a trip under the acting transaction's write lock."""
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
    knobs = sweep_config(target) if knobs is None else knobs
    ticket_id = store.read.run_snapshot(conn, trip.run_id).ticketId
    seen = {"phase": None}

    # Runs in close_out_failure()'s transaction: re-check and failure share a lock.
    def confirm(target):
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
        provider, functools.partial(confirm, target), failure_kind="swept")
    return Outcome(trip, acted, seen["phase"])


def time_box_allowance(time_box, rounds, cap, grace, run_cap):
    return min(time_box * (1 + min(rounds, cap)) * grace,
               time_box * run_cap)


def sweep(target, conn, now, act=False, provider=None, knobs=None):
    knobs = sweep_config(target) if knobs is None else knobs
    stale_ms, strikes_needed = knobs.heartbeat_stale_ms, knobs.stale_strikes
    grace, overlap_threshold = knobs.budget_grace, knobs.review_overlap_threshold
    run_cap = knobs.run_cap
    scale = budget_scale(target)
    trips, watched = [], []
    # One transaction: the watched loop writes the columns this reads.
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
                # Within one stale threshold of the last sighting: the same sample.
                strikes = on_file.strikes
            else:
                strikes = store.record_strike(
                    conn, run_id, stale, heartbeat, now)
            elapsed = agent_work(run, now)
            rounds, cap = run.reviewRoundCount, run.reviewRoundCap or MAX_ROUNDS
            turns = 1 + min(rounds, cap)
            # At most one trip, in order of what explains what: silence, box, review.
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
                watched.append(
                    f"run {run_id} ({ticket}, {phase}): silent"
                    f" {silent / 60000:.1f} min, strike {strikes} of"
                    f" {strikes_needed} on {host_label(target, host)}")
    outcomes = []
    # After the commit: acting may call Linear, never under the write lock.
    if act:
        outcomes = [act_on_trip(target, conn, trip, provider, knobs)
                    for trip in trips]
    return Sweep(len(swept), trips, act, tuple(watched), tuple(outcomes),
                 restarts, tuple(merge_lock_lines(target, conn, act)))


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# Never handed to pid_alive(): os.kill(0, 0) signals our own process group.
HOST_SWEEP_PID = 0


def supervise_pass(target, pid, started_at, now=None, provider=None, out=None,
                   memory=None):
    out = out or sys.stdout
    now = int(time() * 1000) if now is None else now
    # Opened per pass: a connection held across the sleep blocks WAL checkpoints.
    conn = open_store(target)
    try:
        seen = sweep(target, conn, now, act=True, provider=provider)
        if seen.trips or seen.watched or seen.restarts:
            print("\n".join(sweep_lines(seen, target)), file=out)
        reconcile_parked_pull_requests(target, conn, now, provider, out,
                                       memory=memory)
        # After the sweep, at its instant: a fresh beat vouches the sweep ran.
        store.record_supervisor_heartbeat(conn, pid, started_at, now)
    finally:
        conn.close()
    return seen


def loop_is_live(conn, project, now, stale_ms):
    phases = ", ".join("?" * len(SWEEPABLE_PHASES))
    return conn.execute(
        f"SELECT 1 FROM runs WHERE projectId = ? AND endedAt IS NULL"
        f" AND phase IN ({phases}) AND lastHeartbeat > ? LIMIT 1",
        (project, *SWEEPABLE_PHASES, now - stale_ms)).fetchone() is not None


def _linear_budget():
    module = sys.modules.get("linear_provider")
    if module is None:
        import linear_provider as module
    return getattr(module, "__dict__", {}).get("LINEAR_BUDGET")


def linear_budget_low(now=None, out=None):
    budget = _linear_budget()
    if budget is None or not budget.low(now):
        return False
    line = budget.notice(now)
    if line is not None:
        print(f"[holo2] {line}", file=out or sys.stdout)
    return True


def board_ready(conn, project, provider, out, now=None, board_ask_ms=None):
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


ReconcileMemory = collections.namedtuple(
    "ReconcileMemory", ("mirror_asked", "failed_asked", "states_asked"))


def fresh_memory():
    return ReconcileMemory({}, {}, {})


def reconcile_board_closes(conn, project, provider, target, now, out,
                           board_ask_ms, asked):
    from holophyte.host.reconcile import _reconcile_mirror

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
        # A deadline cut may precede the board's answer: ask again, unthrottled.
        if deadline.spent() and previous is None:
            del asked[project]
        elif deadline.spent():
            asked[project] = previous


def reconcile_parked_pull_requests(target, conn, now, provider=None, out=None,
                                   knobs=None, memory=None):
    # Imported here: `holophyte.loop.loop` imports this module.
    from holophyte.host.reconcile import _reconcile_pull_requests
    from holophyte.story.witness import TIP_FAILURES, pass_pending

    out = out or sys.stdout
    knobs = sweep_config(target) if knobs is None else knobs
    memory = fresh_memory() if memory is None else memory
    asked = []
    owed = []
    live = False
    for project, admission in conn.execute(
            "SELECT id, admission FROM projects"
            " WHERE admission IN ('enabled', 'held') ORDER BY id"):
        if provider is not None and not linear_budget_low(now, out):
            observe_board(target, conn, project, provider, now, out,
                          memory.states_asked, knobs.board_ask_ms)
        if admission == "held":
            continue
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
        owed.extend(tickets_owed(target, conn, project, provider, now, out,
                                 knobs))
        try:
            owed.extend((story_id, None) for story_id
                        in pass_pending(target, conn, project))
        except TIP_FAILURES as e:
            print(f"[holo2] main's tip could not be read for a witness pass"
                  f" ({e}); the next pass asks again", file=out)
    # The sweep never writes a `projects` row: a team with none is not asked.
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
    from holophyte.agents.agents import probe_diagnostic, probe_implementer, probe_seat
    from holophyte.host.registry import loop_unit_environment
    from store import launch_backoff

    state = launch_backoff.current(conn, project)
    if state and state["until"] > now:
        return False
    if state and state["interval"] == 0:
        reason = state["reason"]
    else:
        with loop_unit_environment(target):
            probe = probe_implementer(target)
            if (probe is not None and not probe.ok and (
                    target.config().get("agents") or {}).get("implementer_fallback")):
                probe = probe_seat(target, "implement", fallback=True)
            if probe is None or probe.ok:
                launch_backoff.clear(conn, project)
                return True
            reason = probe_diagnostic(target, probe)
    note = launch_backoff.failure(conn, project, reason, now, run_id=run_id)
    print("[holo2] " + " ".join(note.splitlines()), file=out)
    return False


def start_loop_for(target, conn, owed, now, out, project_id=None):
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
    checkout = Path(holophyte.__file__).resolve().parent.parent
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                              capture_output=True, text=True, check=False)
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


# Matches the `SystemExit` `store.open()` raises for a store a newer build stamped.
NEWER_SCHEMA = "newer than the version"


def supervise(target, provider=None, interval=None, wait=None, out=None):
    from holophyte.admission import disabled_startup
    if disabled_startup(target, out):
        return
    return _supervise(target, provider, interval, wait, out)


def _supervise(target, provider=None, interval=None, wait=None, out=None):
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

    # Only a flag: a signal mid-sweep lets the pass's transaction finish.
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
    who = "host sweep" if pid == HOST_SWEEP_PID else f"pid {pid}"
    return (f"supervisor: {state}, last heartbeat {format_age(age)} ago"
            f" ({who} on {host_label(target, host)})")

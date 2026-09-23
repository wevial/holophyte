"""Supervisor contract: `--supervise`, the loop's re-exec, its config table,
the waiting loop's heartbeat, the host label and the pull-request close-out.

The sweep itself is asserted in `test_supervisor_sweep.py`; these are the
tests for the watcher that runs it on a timer and what it does between sweeps.
The fixture is shared through `sweep_fixture.py`.

Run: python3 -m unittest discover -s tests -p 'test_supervise*' -v
"""
from __future__ import annotations

import fcntl
import io
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.cli  # noqa: E402 - after the sys.path insert above
import holophyte.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.pr_status  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor  # noqa: E402 - after the sys.path insert above
import holophyte.supervisor_lock  # noqa: E402 - after the sys.path insert above
import holophyte.sweep_report  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the shared fixture
from sweep_fixture import (  # noqa: E402 - after the insert
    MINUTE,
    T0,
    StubProvider,
    SweepTestCase,
    Tripwire,
    no_network,
)

from tests.phase_fixture import (  # noqa: E402 - after sys.path setup
    finish_run,
    park_run,
)


def a_dead_pid():
    """A pid the kernel no longer knows: a child that has already been reaped.

    Reuse is possible in principle and negligible in a test's lifetime; the
    alternative, a pid guessed to be free, is a guess.
    """
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


class SuperviseTests(SweepTestCase):
    """`factory.py --supervise <target>`: one watcher per target, on a timer.

    The loop body is driven with the clock as a parameter, like the sweep it
    wraps; the lock is exercised with real files in this test's directory;
    the signal is a real SIGTERM delivered to this process, because a handler
    that is never invoked proves nothing about clean exit.
    """

    def setUp(self):
        super().setUp()
        # `--supervise` posts to the board, so `cli()` needs one named; the
        # provider is a tripwire below, so the values are never sent.
        (self.db.parent / "config.toml").write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n')
        self.tgt = holophyte.project.Project.locate(self.target)
        self.lock = holophyte.supervisor_lock.supervisor_lock_path(self.tgt)

    def supervise(self, wait):
        """The mode with an injected sleep, and the provider as a tripwire."""
        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network():
                code = holophyte.supervisor.supervise(self.tgt, wait=wait, out=out)
        return code, out.getvalue()

    def heartbeats(self):
        return self.conn.execute(
            "SELECT pid, lastBeat, passes FROM supervisorHeartbeats"
            " ORDER BY startedAt").fetchall()

    def test_a_second_supervisor_exits_nonzero_naming_the_live_pid(self):
        """The first holder is a live child of this process -- a process of
        its own, as a running supervisor is -- and the second start is the
        mode end to end, as an operator (or a service manager retrying)
        would run it."""
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        holophyte.supervisor_lock.acquire_supervisor_lock(self.lock, self.tgt.path,
                                        pid=holder.pid, now=T0)
        complaint = io.StringIO()

        with patch.object(sys, "stderr", complaint), \
                self.assertRaises(SystemExit) as exited:
            holophyte.cli.cli(["--supervise", str(self.target)])

        self.assertNotEqual(exited.exception.code, 0)
        self.assertIn(f"pid {holder.pid} on {socket.gethostname()}",
                      str(exited.exception))
        # The refusal names the repository as well as the lock: an operator
        # supervising several targets has to know which one is already taken.
        self.assertIn(f"for {self.tgt.path}:", str(exited.exception))
        # And the holder's lock is untouched: a refused starter must not
        # take the file out from under the supervisor it deferred to.
        self.assertEqual(holophyte.supervisor_lock.read_supervisor_lock(self.lock),
                         (holder.pid, T0, socket.gethostname()))
        self.assertEqual(self.lock.read_text().split(),
                         [socket.gethostname(), str(holder.pid), str(T0)])

    def test_a_refused_start_says_whether_the_holder_is_still_beating(self):
        """The refusal is actionable only with the liveness beside it: the
        holder's lock plus a fresh heartbeat is a watcher at work, which is
        the answer to "do I relaunch?" that the lock alone never gave."""
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        holophyte.supervisor_lock.acquire_supervisor_lock(self.lock, self.tgt.path,
                                        pid=holder.pid, now=T0)
        now = int(holophyte.supervisor.time() * 1000)
        store.record_supervisor_heartbeat(self.conn, holder.pid, T0,
                                          now=now - 12_000)
        self.conn.commit()

        with patch.object(sys, "stderr", io.StringIO()), \
                self.assertRaises(SystemExit) as exited:
            holophyte.cli.cli(["--supervise", str(self.target)])

        message = str(exited.exception)
        self.assertIn(f"pid {holder.pid}", message.splitlines()[0])
        self.assertRegex(
            message.splitlines()[-1],
            rf"^supervisor: live, last heartbeat 1[2-9]s ago"
            rf" \(pid {holder.pid} on {socket.gethostname()}\)$")

    def test_a_lock_naming_no_pid_is_refused_rather_than_guessed_about(self):
        """Never spawn a rival on one ambiguous probe: an empty lock is a
        starter that crashed between its create and its write, or something
        else entirely -- either way not this starter's to remove."""
        self.lock.write_text("")

        with self.assertRaises(holophyte.supervisor_lock.SupervisorHeld) as refused:
            holophyte.supervisor_lock.acquire_supervisor_lock(self.lock, self.tgt.path,
                                            pid=os.getpid(), now=T0)

        self.assertIsNone(refused.exception.pid)
        self.assertIn(str(self.lock), str(refused.exception))
        self.assertTrue(self.lock.exists())

    def test_a_stale_lock_is_reclaimed_and_the_supervisor_runs(self):
        """A dead pid in the lock is a supervisor killed without the chance
        to clean up. The proof of "runs" is a pass on file under this
        process's pid, made while the lock named this process."""
        self.lock.write_text(f"{socket.gethostname()} {a_dead_pid()} {T0}\n")
        held_during_pass = []

        def stop_after_one_pass(_interval):
            held_during_pass.append(
                holophyte.supervisor_lock.read_supervisor_lock(self.lock))
            os.kill(os.getpid(), signal.SIGTERM)

        code, printed = self.supervise(stop_after_one_pass)

        self.assertEqual(code, 0)
        self.assertEqual(held_during_pass[0][0], os.getpid())
        self.assertEqual([(pid, passes) for pid, _at, passes
                          in self.heartbeats()], [(os.getpid(), 1)])
        self.assertIn(f"pid {os.getpid()}", printed)

    def test_two_starters_reclaiming_one_stale_lock_admit_only_one(self):
        """Both starters read the same dead pid; the rival gets its reclaim
        and its new lock in *between* this starter's last look at the stale
        file and its unlink -- the widest window the old inode guard left
        open. The rival's live lock must survive, and this starter must
        lose to it, or two supervisors run side by side."""
        self.lock.write_text(f"{a_dead_pid()} {T0}\n")
        us, rival = os.getpid(), os.getpid() + 1
        real_unlink, real_alive = os.unlink, holophyte.supervisor_lock.pid_alive
        rival_outcome, fired = [], []

        def rival_starts():
            try:
                rival_outcome.append(
                    holophyte.supervisor_lock.acquire_supervisor_lock(
                        self.lock, self.tgt.path, pid=rival, now=T0 + 1))
            except holophyte.supervisor_lock.SupervisorHeld as held:
                rival_outcome.append(held)

        def unlink_with_a_rival_in_the_gap(path, *args, **kwargs):
            if not fired and Path(path) == self.lock:
                fired.append(threading.Thread(target=rival_starts))
                fired[0].start()
                fired[0].join(0.5)
            return real_unlink(path, *args, **kwargs)

        with patch.object(holophyte.supervisor_lock, "pid_alive",
                          lambda pid: pid in (us, rival) or real_alive(pid)), \
                patch.object(os, "unlink", unlink_with_a_rival_in_the_gap):
            try:
                ours = holophyte.supervisor_lock.acquire_supervisor_lock(
                    self.lock, self.tgt.path, pid=us, now=T0)
            except holophyte.supervisor_lock.SupervisorHeld as held:
                ours = held
            fired[0].join(5)

        self.assertTrue(fired, "the rival never got its turn in the gap")
        outcomes = {us: ours, rival: rival_outcome[0]}
        admitted = [who for who, got in outcomes.items()
                    if not isinstance(got, Exception)]
        self.assertEqual(len(admitted), 1, outcomes)
        # The lock on disk names the one starter that was admitted, and the
        # other was refused naming exactly that pid.
        self.assertEqual(holophyte.supervisor_lock.read_supervisor_lock(self.lock)[0],
                         admitted[0])
        refused = outcomes[rival if admitted == [us] else us]
        self.assertEqual(refused.pid, admitted[0])

    def test_sigterm_ends_the_loop_cleanly_and_releases_the_lock(self):
        """A real SIGTERM to this process, delivered while the supervisor is
        between passes: the loop returns rather than raising, the lock is
        gone for the next starter, and the handler this process had before
        is back in place."""
        before = signal.getsignal(signal.SIGTERM)
        passes = []

        def stop_on_second_sleep(_interval):
            passes.append(len(self.heartbeats()))
            if len(passes) == 2:
                os.kill(os.getpid(), signal.SIGTERM)

        code, printed = self.supervise(stop_on_second_sleep)

        self.assertEqual(code, 0)
        self.assertFalse(self.lock.exists())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        # Two sleeps, two passes, and none after the signal: the flag is read
        # before each pass, so a signal ends the loop at the next check
        # rather than one more sweep later.
        self.assertEqual(self.heartbeats()[0][2], 2)
        self.assertIn("stopping on signal", printed)

    def test_a_moved_factory_revision_releases_the_lock_and_re_executes(self):
        """The revision is read at startup and before each pass; on the
        first pass that finds it moved, the lock is gone before the exec
        and the printed line names both revisions. Through the `EXEC` seam:
        the test runner is never exec-ed, and a real one never returns."""
        execs = []

        def record_exec(program, argv):
            execs.append((program, argv, self.lock.exists()))

        orig = ["/usr/bin/python3", "-u", "factory.py", "--supervise", "/r"]
        revisions = iter(["aaa", "bbb", "bbb"])
        with patch.object(holophyte.supervisor, "EXEC", record_exec), \
                patch.object(holophyte.supervisor, "factory_revision",
                             lambda: next(revisions)), \
                patch.object(sys, "orig_argv", orig):
            code, printed = self.supervise(lambda _interval: None)

        self.assertEqual(code, 0)
        self.assertEqual(execs, [("/usr/bin/python3", orig, False)])
        self.assertIn("[holo2] factory code moved from aaa to bbb;"
                      " supervisor re-executing", printed)
        # `aaa` at startup, `bbb` before the first pass: no pass ran on the
        # stale code, so no heartbeat was written.
        self.assertEqual(self.heartbeats(), [])

    def test_a_store_a_newer_build_stamped_re_executes_instead_of_exiting(self):
        """The 55 unsupervised minutes: a self-merge bumped the schema, the
        pass's store open refused it with `SystemExit`, and the supervisor
        exited. Now the refusal is the re-exec's second trigger, with the
        lock released first."""
        execs = []

        def record_exec(program, argv):
            execs.append((program, argv, self.lock.exists()))

        self.conn.execute(
            f"PRAGMA user_version = {store.SCHEMA_VERSION + 1}")
        self.conn.commit()
        with patch.object(holophyte.supervisor, "EXEC", record_exec), \
                patch.object(holophyte.supervisor, "factory_revision",
                             lambda: "aaa"), \
                patch.object(sys, "orig_argv", ["python3", "factory.py"]):
            code, printed = self.supervise(lambda _interval: None)

        self.assertEqual(code, 0)
        ((program, argv, held),) = execs
        self.assertTrue(os.path.isabs(program), program)
        self.assertEqual(argv, ["python3", "factory.py"])
        self.assertFalse(held)
        self.assertIn("newer than the version", printed)
        self.assertIn("supervisor re-executing", printed)

    def test_a_stop_request_wins_over_a_pending_re_exec(self):
        """A signal that lands during the refused pass ends the loop; the
        refusal is re-raised as before rather than exec-ed past."""
        execs = []

        def refuse_then_stop(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            raise SystemExit("x: newer than the version this build understands")

        with patch.object(holophyte.supervisor, "EXEC",
                          lambda *a: execs.append(a)), \
                patch.object(holophyte.supervisor, "supervise_pass",
                             refuse_then_stop), \
                self.assertRaises(SystemExit):
            self.supervise(lambda _interval: None)

        self.assertEqual(execs, [])
        self.assertFalse(self.lock.exists())

    def test_a_stop_request_wins_over_a_revision_triggered_re_exec(self):
        """The other trigger: a signal that lands while the revision is
        being read -- inside the git call, after the loop's own stop check
        -- ends the loop with the lock released, even though the revision
        came back moved."""
        execs = []
        reads = []

        def moved_but_stopped():
            # Startup's read is plain; the one before the first pass is the
            # one the signal lands in.
            reads.append(1)
            if len(reads) == 1:
                return "aaa"
            os.kill(os.getpid(), signal.SIGTERM)
            return "bbb"

        with patch.object(holophyte.supervisor, "EXEC",
                          lambda *a: execs.append(a)), \
                patch.object(holophyte.supervisor, "factory_revision",
                             moved_but_stopped):
            code, printed = self.supervise(lambda _interval: None)

        self.assertEqual(code, 0)
        self.assertEqual(execs, [])
        self.assertFalse(self.lock.exists())
        self.assertNotIn("re-executing", printed)
        self.assertIn("stopping on signal", printed)

    def test_the_loop_body_sweeps_with_action_and_records_a_heartbeat(self):
        """The body is `--sweep --act` plus a beat: a run silent across two
        passes is failed with its lease released, and each pass bumps the
        supervisor's own row to the instant it swept at."""
        run_id = self.a_run()
        self.conn.commit()
        pid = os.getpid()

        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network(), patch.object(sys, "stdout", io.StringIO()):
                holophyte.supervisor.supervise_pass(self.tgt, pid, T0,
                                                    now=T0 + 6 * MINUTE)
                holophyte.supervisor.supervise_pass(self.tgt, pid, T0,
                                                    now=T0 + 12 * MINUTE)

        self.assertEqual(
            self.conn.execute(
                "SELECT phase, outcome FROM runs WHERE id = ?",
                (run_id,)).fetchone(), ("failed", "failed"))
        self.assertIsNone(
            self.conn.execute(
                "SELECT activeRunId FROM projects").fetchone()[0])
        self.assertEqual(self.heartbeats(), [(pid, T0 + 12 * MINUTE, 2)])


class LoopRestartTests(SweepTestCase):
    """A self-merge re-exec the loop did not come back from.

    The loop leaves a `loopRestarts` note before `os.execv()` replaces it; a
    loop that returned claims (a heartbeat) or writes its exit note. Past the
    grace window with neither, the sweep says so -- once -- and is otherwise
    exactly as quiet as it was.
    """

    SHA = "abc1234"
    SECOND = 1000

    def lines(self, now):
        return holophyte.sweep_report.sweep_lines(
            holophyte.supervisor.sweep(self.tgt, self.conn, now))

    def restart_lines(self, now):
        return [line for line in self.lines(now) if "re-exec" in line]

    def reported(self):
        return self.conn.execute(
            "SELECT reportedAt FROM loopRestarts ORDER BY id").fetchall()

    def test_a_restart_followed_by_a_heartbeat_is_quiet(self):
        """Restart at T, a claim (which stamps the run's heartbeat) at T+30s,
        sweep at T+200s: nothing about restarts, and the run report reads
        exactly as it did before restarts existed."""
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)
        self.a_run(claimed_at=T0 + 30 * self.SECOND)

        lines = self.lines(T0 + 200 * self.SECOND)

        self.assertEqual(lines, ["1 run swept, all healthy"])
        self.assertEqual(self.reported(), [(None,)])

    def test_a_restart_followed_by_the_exit_note_is_quiet(self):
        """A loop that came back to no ready tickets never heartbeats; its
        exit note is what says it returned."""
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)
        store.record_loop_return(self.conn, self.project,
                                 now=T0 + 30 * self.SECOND)

        lines = self.lines(T0 + 200 * self.SECOND)

        self.assertEqual(lines, ["no runs in flight, nothing to sweep"])
        self.assertEqual(self.reported(), [(None,)])

    def test_a_restart_nothing_followed_is_reported_once_past_the_grace(self):
        """Restart at T and silence: inside the default two minutes nothing
        is said; at T+200s the line names the sha and the store records the
        condition; a second sweep neither prints nor records it again."""
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)

        self.assertEqual(self.restart_lines(T0 + 60 * self.SECOND), [])
        self.assertEqual(self.reported(), [(None,)])

        first = self.lines(T0 + 200 * self.SECOND)

        line, = [line for line in first if "re-exec" in line]
        self.assertIn(f"loop did not return after re-exec from {self.SHA}",
                      line)
        self.assertIn("3.3 min", line)
        # Above the run report, which is the quiet case: a dead loop leaves
        # nothing in flight, and that must not hide the line.
        self.assertEqual(first, [line, "no runs in flight, nothing to sweep"])
        self.assertEqual(self.reported(), [(T0 + 200 * self.SECOND,)])

        second = self.lines(T0 + 260 * self.SECOND)

        self.assertEqual(second, ["no runs in flight, nothing to sweep"])
        self.assertEqual(self.reported(), [(T0 + 200 * self.SECOND,)])

    def test_a_claim_from_before_the_restart_does_not_vouch_for_it(self):
        """The heartbeat that clears a restart has to be newer than it: the
        run the old loop merged just before re-executing is history."""
        run_id = self.a_run(claimed_at=T0 - 10 * MINUTE)
        self.heartbeat_at(run_id, T0 - self.SECOND)
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)

        self.assertEqual(len(self.restart_lines(T0 + 200 * self.SECOND)), 1)

    def test_the_supervisor_pass_prints_the_line_on_an_otherwise_quiet_pass(self):
        """`--supervise` prints nothing on a healthy pass; an unreturned
        restart is not a healthy pass."""
        store.record_loop_restart(self.conn, self.project, self.SHA, now=T0)
        self.conn.commit()
        out = io.StringIO()

        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network():
                holophyte.supervisor.supervise_pass(self.tgt, os.getpid(), T0,
                                       now=T0 + 200 * self.SECOND, out=out)
                holophyte.supervisor.supervise_pass(self.tgt, os.getpid(), T0,
                                       now=T0 + 260 * self.SECOND, out=out)

        self.assertEqual(
            out.getvalue().count("loop did not return after re-exec from"
                                 f" {self.SHA}"), 1)


class SupervisorConfigTests(SweepTestCase):
    """`[supervisor]` in the target's `config.toml`: the thresholds have an address.

    An absent table is the constants the tests above were written against; a
    key that is present moves exactly the trip it names; a key outside its
    constraint is refused at startup, before anything is swept.
    """

    def configure(self, text):
        """Write the target's config and build the `Project` that reads it.

        A fresh value rather than the fixture's: a `Project` parses its config
        once, and this test wants the file it just wrote.
        """
        (self.db.parent / "config.toml").write_text(text)
        self.tgt = holophyte.project.Project.locate(self.target)

    def test_an_absent_table_is_the_documented_defaults(self):

        self.assertEqual(holophyte.config_tables.sweep_config(self.tgt),
                         (5 * MINUTE, 2, 1.5, 3.0, 0.5, 60, 2 * MINUTE,
                          10 * MINUTE))

    def test_heartbeat_stale_min_moves_the_silence_a_trip_needs(self):
        """A heartbeat two and three minutes old on two consecutive sweeps:
        not even a strike under the default five, a trip under one."""
        run_id = self.a_run()
        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 2 * MINUTE)
        default = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 3 * MINUTE)
        self.assertEqual(default.trips, [])
        self.assertIsNone(self.strikes(run_id))

        self.configure("[supervisor]\nheartbeat_stale_min = 1\n")
        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 2 * MINUTE)
        result = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 3 * MINUTE)

        trip, = result.trips
        self.assertEqual((trip.run_id, trip.condition),
                         (run_id, holophyte.supervisor.STALE_HEARTBEAT))
        self.assertIn("over 2 consecutive sweeps", trip.evidence)

    def test_stale_strikes_moves_how_many_sightings_a_trip_needs(self):
        run_id = self.a_run()
        self.configure("[supervisor]\nstale_strikes = 3\n")

        holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 6 * MINUTE)
        second = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 12 * MINUTE)
        third = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 18 * MINUTE)

        self.assertEqual(second.trips, [])
        (line,) = second.watched
        self.assertIn("strike 2 of 3", line)
        self.assertEqual([t.run_id for t in third.trips], [run_id])

    def test_unknown_keys_in_the_table_are_left_alone(self):
        self.configure("[supervisor]\nstale_heartbeat_min = 7\n")

        self.assertEqual(holophyte.config_tables.sweep_config(self.tgt).heartbeat_stale_ms,
                         5 * MINUTE)

    def test_a_value_outside_its_constraint_is_refused_at_startup(self):
        """Named key, named constraint, and no sweep: the strike table is
        as empty afterwards as it was before."""
        self.a_run()
        self.conn.commit()
        for line, key, constraint in (
                ("heartbeat_stale_min = -1", "heartbeat_stale_min",
                 "a finite positive number"),
                # TOML spells infinity; `inf > 0` holds, and an infinite
                # threshold never trips while an infinite interval crashes
                # `sleep()` with OverflowError. Both are refused up front.
                ("heartbeat_stale_min = inf", "heartbeat_stale_min",
                 "a finite positive number"),
                ("sweep_interval_sec = inf", "sweep_interval_sec",
                 "a finite positive number"),
                ("budget_grace = nan", "budget_grace",
                 "a finite positive number"),
                ("review_overlap_threshold = 1.5", "review_overlap_threshold",
                 "a number in (0, 1]"),
                ("review_overlap_threshold = 0", "review_overlap_threshold",
                 "a number in (0, 1]"),
                ("stale_strikes = 1.5", "stale_strikes",
                 "a positive integer"),
                ("budget_grace = true", "budget_grace",
                 "a finite positive number"),
                ('sweep_interval_sec = "60"', "sweep_interval_sec",
                 "a finite positive number"),
                ("restart_grace_sec = 0", "restart_grace_sec",
                 "a finite positive number")):
            with self.subTest(line=line):
                self.configure(f"[supervisor]\n{line}\n")
                with self.assertRaises(SystemExit) as raised, \
                        patch.object(holophyte.sweep_report, "time",
                                     lambda: (T0 + 6 * MINUTE) / 1000):
                    holophyte.cli.cli(["--sweep", str(self.target)])
                message = str(raised.exception)
                self.assertIn(f"[supervisor] {key}", message)
                self.assertIn(constraint, message)
                self.assertIn(str(self.db.parent / "config.toml"), message)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM sweepStrikes").fetchone(),
            (0,))

    def test_a_non_table_supervisor_value_is_refused_even_when_falsy(self):
        """`supervisor = false` is not "no table": it is a wrong-typed key,
        and gets the same startup refusal a string or list would."""
        for line, kind in (("supervisor = false", "bool"),
                           ("supervisor = 0", "int"),
                           ('supervisor = ""', "str"),
                           ("supervisor = []", "list"),
                           ('supervisor = "table"', "str")):
            with self.subTest(line=line):
                self.configure(line + "\n")
                with self.assertRaises(SystemExit) as raised:
                    holophyte.config_tables.sweep_config(self.tgt)
                message = str(raised.exception)
                self.assertIn("[supervisor] must be a table", message)
                self.assertIn(f"got {kind}", message)

    def test_restart_grace_sec_moves_how_long_a_re_exec_may_take(self):
        """A restart 200s old: reported under the default two minutes, not
        under a five-minute grace."""
        store.record_loop_restart(self.conn, self.project, "abc1234", now=T0)
        self.configure("[supervisor]\nrestart_grace_sec = 300\n")

        patient = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 200_000)
        self.assertEqual(patient.restarts, ())

        self.configure("[supervisor]\nrestart_grace_sec = 120\n")
        default = holophyte.supervisor.sweep(self.tgt, self.conn, T0 + 200_000)
        self.assertEqual(default.restarts, (("abc1234", 200_000),))

    def test_sweep_interval_sec_is_the_supervisor_s_sleep(self):
        self.configure("[supervisor]\nsweep_interval_sec = 7\n")
        slept = []

        def stop_after_one(interval):
            slept.append(interval)
            os.kill(os.getpid(), signal.SIGTERM)

        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network():
                code = holophyte.supervisor.supervise(self.tgt, wait=stop_after_one,
                                                      out=out)

        self.assertEqual(code, 0)
        self.assertEqual(slept, [7])
        self.assertIn("every 7s", out.getvalue())


class HeartbeatWhileTests(SweepTestCase):
    """`heartbeat_while()` keeps a waiting loop off the sweep's stale list.

    Real clock, unlike the rest of this file: the beat is a timer thread, so
    the sweeps here are given the wall-clock `now` the beats are stamped
    against, and the thresholds are set in tens of milliseconds to match.
    """

    def knobs(self, stale_ms):
        return holophyte.config_tables.sweep_config(self.tgt)._replace(
            heartbeat_stale_ms=stale_ms)

    def sweep_now(self, knobs):
        return holophyte.supervisor.sweep(
            self.tgt, self.conn, int(time.time() * 1000), knobs=knobs)

    def test_a_loop_that_beats_is_alive_and_one_that_stopped_is_dead(self):
        """Inside the block the run outlives the stale threshold untripped;
        after it the beats stop and the same threshold trips it on the
        second sighting, as a dead loop's run has always been."""
        run_id = self.a_run(claimed_at=int(time.time() * 1000))
        knobs = self.knobs(stale_ms=300)
        stale_span = knobs.heartbeat_stale_ms * knobs.stale_strikes / 1000

        with holophyte.runs.heartbeat_while(self.conn, run_id, 0.05):
            phase_before = store.run_phase(self.conn, run_id)
            time.sleep(stale_span)
            alive = self.sweep_now(knobs)
            time.sleep(stale_span)
            still_alive = self.sweep_now(knobs)

        self.assertEqual(alive.trips, [])
        self.assertEqual(still_alive.trips, [])
        self.assertIsNone(self.strikes(run_id))
        # The beat moved the heartbeat without touching the phase or the
        # narrative: no `phase_change` beyond the one `a_run()` made.
        self.assertEqual(store.run_phase(self.conn, run_id), phase_before)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM runEvents WHERE runId = ?",
            (run_id,)).fetchone()[0], 1)

        time.sleep(stale_span)
        first = self.sweep_now(knobs)
        first_strikes = self.strikes(run_id)[0]
        time.sleep(stale_span)
        second = self.sweep_now(knobs)

        self.assertEqual((first.trips, first_strikes), ([], 1))
        self.assertEqual([(t.run_id, t.condition) for t in second.trips],
                         [(run_id, "stale_heartbeat")])

    def test_a_heartbeat_on_an_ended_run_changes_nothing(self):
        run_id = self.a_run()
        finish_run(self.conn, run_id, "merged", now=T0 + MINUTE)
        before = self.conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

        moved = store.heartbeat(self.conn, run_id, now=T0 + 5 * MINUTE)

        self.assertFalse(moved)
        after = self.conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(after, before)


class HostLabelTests(SweepTestCase):
    """`[report] host_label` on the sweep table and the supervisor's lines.

    The lock file and the store keep the real hostname -- `reclaim_turn` and
    `still_tripped` compare against it -- and only the printed lines change.
    """

    LABEL = "writer-1"

    def setUp(self):
        super().setUp()
        (self.db.parent / "config.toml").write_text(
            f'[report]\nhost_label = "{self.LABEL}"\n'
            '[board]\nproject_id = "p-1"\nteam = "T"\n')
        self.tgt = holophyte.project.Project.locate(self.target)
        self.lock = holophyte.supervisor_lock.supervisor_lock_path(self.tgt)

    def test_the_sweep_table_and_watched_line_show_the_label(self):
        self.a_run()
        self.conn.commit()

        first = self.run_sweep(T0 + 6 * MINUTE)
        printed = self.run_sweep(T0 + 12 * MINUTE)

        hostname = socket.gethostname()
        self.assertNotIn(hostname, "\n".join(first + printed))
        # Line 0 of each pass is the review-container section.
        self.assertTrue(first[2].endswith(f" on {self.LABEL}"), first[2])
        self.assertEqual(printed[2].split()[-1], self.LABEL)

    def test_the_startup_and_refusal_lines_show_the_label_over_a_real_lock(self):
        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network(), \
                    patch.object(holophyte.supervisor, "supervise_pass"):
                holophyte.supervisor.supervise(
                    self.tgt, wait=lambda _i: os.kill(os.getpid(), signal.SIGTERM),
                    out=out)
        holder = subprocess.Popen(["sleep", "60"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        holophyte.supervisor_lock.acquire_supervisor_lock(
            self.lock, self.tgt.path, pid=holder.pid, now=T0)

        with patch.object(sys, "stderr", io.StringIO()), \
                self.assertRaises(SystemExit) as exited:
            holophyte.cli.cli(["--supervise", str(self.target)])

        hostname = socket.gethostname()
        self.assertIn(f"as pid {os.getpid()} on {self.LABEL}", out.getvalue())
        self.assertNotIn(hostname, out.getvalue())
        self.assertIn(f"pid {holder.pid} on {self.LABEL}", str(exited.exception))
        self.assertNotIn(hostname, str(exited.exception))
        # The lock itself still names the machine: another host reading the
        # state directory has to know whose pid that is.
        self.assertEqual(self.lock.read_text().split()[0], hostname)


class ParkedPullRequestTests(SweepTestCase):
    """Reconcile parked pull requests and restart loops for ready work."""

    URL = "https://github.com/example/repo/pull/7"
    MERGE_SHA = "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
    MERGED_PULL = {"state": "MERGED", "merged": True,
                   "mergeCommit": {"oid": MERGE_SHA},
                   "mergedBy": {"login": "coworker"}}

    def setUp(self):
        super().setUp()
        (self.db.parent / "config.toml").write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n'
            '[merge]\nmode = "pr"\napprove = "human"\n')
        self.tgt = holophyte.project.Project.locate(self.target)

    def parked_on_pr(self):
        """A run parked on its pull request the way `_park_on_pr()` leaves
        it: the ticket `blocked_on_operator` asking about the URL, the run
        in `awaiting_merge_approval` with `prUrl` set and its lease given
        back."""
        run_id = self.a_run()
        ticket = self.ticket_of[run_id]
        store.tickets.transition(self.conn, ticket, "blocked_on_operator")
        self.conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                          (f"PR open: {self.URL}", ticket))
        self.conn.commit()
        park_run(self.conn, run_id, "awaiting_merge_approval",
                   pr_url=self.URL, now=T0)
        return run_id

    def fake_github(self, answer):
        """`holophyte.pr_status.graphql` faked at the pull-status read: answers
        `answer` (raised when it is an exception) and records each ask."""
        asked = []

        def graphql(target, pull, query, variables):
            asked.append((pull.url, variables["number"]))
            if isinstance(answer, Exception):
                raise answer
            return {"repository": {"pullRequest": answer}}

        patcher = patch.object(holophyte.pr_status, "graphql", graphql)
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def one_pass(self, at, provider=None):
        out = io.StringIO()
        with no_network():
            holophyte.supervisor.supervise_pass(
                self.tgt, os.getpid(), T0, now=at, provider=provider, out=out)
        return out.getvalue()

    def run_row(self, run_id):
        return self.conn.execute(
            "SELECT phase, outcome, mergeSha FROM runs WHERE id = ?",
            (run_id,)).fetchone()

    def test_one_sweep_with_no_loop_ships_a_merged_pull_request(self):
        run_id = self.parked_on_pr()
        asked = self.fake_github(self.MERGED_PULL)
        provider = StubProvider()

        out = self.one_pass(T0 + 20 * MINUTE, provider)

        self.assertEqual(asked, [(self.URL, 7)])
        self.assertEqual(self.run_row(run_id),
                         ("done", "merged", self.MERGE_SHA))
        self.assertEqual(
            self.conn.execute("SELECT status, blockedQuestion FROM tickets"
                              " WHERE id = ?",
                              (self.ticket_of[run_id],)).fetchone(),
            ("merged", None))
        self.assertEqual(provider.states, [("issue-1", "Done")])
        self.assertIn(f"{self.URL} was merged on GitHub by coworker", out)
        self.assertIn(f"run {run_id} closed out as merged", out)

    # KO-376: a sweep that sent a run back to the babysitter walked its
    # ticket to `ready` on a board whose loop has exited, so it starts the
    # loop as the console's launch-loop action does, through a `systemctl`
    # a fake on PATH records.
    ACTIVE_PULL = {"state": "OPEN", "merged": False,
                   "updatedAt": "2026-09-02T10:00:00Z",
                   "reviewThreads": {"totalCount": 2},
                   "comments": {"nodes": [{"id": "new-comment",
                       "createdAt": "2026-09-02T10:00:00Z",
                       "author": {"login": "reviewer"}, "body": "Please check"}]}}

    def fake_systemctl(self):
        """A `systemctl` first on PATH that records each call's arguments,
        one line per call, and exits 0; the lines so far."""
        bin_dir = self.root / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        record = self.root / "systemctl.calls"
        script = bin_dir / "systemctl"
        script.write_text(f"#!/bin/sh\necho \"$@\" >> '{record}'\n")
        script.chmod(0o755)
        patcher = patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        return lambda: (record.read_text().splitlines()
                        if record.exists() else [])

    def seen_before_activity(self, run_id):
        """The mark the babysitter's park left: a read older than
        `ACTIVE_PULL`'s activity, so the next read is new activity."""
        store.record_pr_seen(self.conn, run_id,
                             ("2026-09-01T10:00:00Z", 1, None, None, None))

    def test_a_sweep_that_sent_a_ticket_back_starts_the_loop_unit(self):
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        self.fake_github(self.ACTIVE_PULL)
        calls = self.fake_systemctl()

        out = self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertIn(f"run {run_id} sent back to the babysitter", out)
        self.assertEqual(
            self.conn.execute("SELECT status FROM tickets WHERE id = ?",
                              (self.ticket_of[run_id],)).fetchone(),
            ("ready",))
        self.assertEqual(calls(), ["--user start holophyte-loop@repo"])
        self.assertIn("started holophyte-loop@repo", out)

    def test_a_sweep_under_a_live_loop_starts_nothing(self):
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        self.fake_github(self.ACTIVE_PULL)
        calls = self.fake_systemctl()
        self.a_run(claimed_at=T0 + 19 * MINUTE)

        out = self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertEqual(calls(), [])
        self.assertNotIn("holophyte-loop@", out)

    def test_a_sweep_under_a_held_lease_turn_starts_nothing(self):
        """A loop between its startup and its first claim's heartbeat is
        visible only as the holder of the lease turn (`lease.lock`), so a
        held turn is a live loop for the launch, whatever the runs say."""
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        self.fake_github(self.ACTIVE_PULL)
        calls = self.fake_systemctl()
        lock = holophyte.board.lease_turn_path(self.tgt)
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

        out = self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertIn(f"run {run_id} sent back to the babysitter", out)
        self.assertEqual(calls(), [])
        self.assertNotIn("holophyte-loop@", out)

    def test_a_failed_start_is_retried_and_a_taken_one_that_raised_no_loop(
            self):
        """The ticket the send-back walked to `ready` is still owed a loop
        after a start `systemctl` refused, so the next pass tries again; a
        taken start is owed again the same way while the ticket stays
        `ready` and nothing came live -- the `launch_loop` row records the
        start, it does not discharge it, so a loop that exited before
        claiming is raised again one pass later."""
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        self.fake_github(self.ACTIVE_PULL)
        calls = self.fake_systemctl()
        script = self.root / "fake-bin" / "systemctl"
        good = script.read_text()
        script.write_text("#!/bin/sh\necho 'Failed to connect to bus' >&2\n"
                          "exit 1\n")

        first = self.one_pass(T0 + 20 * MINUTE, StubProvider())
        script.write_text(good)
        second = self.one_pass(T0 + 21 * MINUTE, StubProvider())
        third = self.one_pass(T0 + 22 * MINUTE, StubProvider())

        self.assertIn("could not be started (Failed to connect to bus)", first)
        self.assertIn("started holophyte-loop@repo", second)
        self.assertIn("started holophyte-loop@repo", third)
        self.assertEqual(calls(), ["--user start holophyte-loop@repo",
                                   "--user start holophyte-loop@repo"])
        self.assertEqual(
            self.conn.execute("SELECT status FROM tickets WHERE id = ?",
                              (self.ticket_of[run_id],)).fetchone(),
            ("ready",))

    def events_of(self, run_id):
        """The `(kind, summary)` rows of `run_id`'s event stream, in order."""
        return self.conn.execute(
            "SELECT kind, summary FROM runEvents WHERE runId = ?"
            " ORDER BY seq", (run_id,)).fetchall()

    def test_the_attempt_is_recorded_before_the_start_is_asked_for(self):
        """Record before acting: the attempt row is committed before
        `systemctl` is asked, so a supervisor that dies mid-start still
        left the store saying it tried. The fake `systemctl` dumps the
        run's event kinds as it is called, which is the only witness of
        the order; the `launch_loop` intervention stays the success mark
        and lands after."""
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        self.fake_github(self.ACTIVE_PULL)
        calls = self.fake_systemctl()
        seen = self.root / "events-at-call"
        (self.root / "fake-bin" / "systemctl").write_text(
            "#!/bin/sh\n"
            f"{sys.executable} -c \"import sqlite3;"
            f" c = sqlite3.connect('{self.db}');"
            " print('\\n'.join(k for (k,) in c.execute("
            "'SELECT kind FROM runEvents WHERE runId = ? ORDER BY seq',"
            f" ({run_id},))))\" > '{seen}'\n")

        self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertEqual(calls(), [])  # the recorder was replaced above
        at_call = seen.read_text().splitlines()
        self.assertIn("launch_loop_attempt", at_call)
        marks = [f"{k}: {s}" for k, s in self.events_of(run_id)
                 if "launch_loop" in f"{k}: {s}"]
        self.assertEqual(len(marks), 2, marks)
        self.assertTrue(marks[0].startswith("launch_loop_attempt:"), marks)
        self.assertTrue(marks[1].startswith("intervention: supervisor"
                                            " launch_loop:"), marks)
        # The success mark was not there when `systemctl` was asked: the
        # dump holds one row fewer than the stream ends with.
        self.assertEqual(len(at_call), len(self.events_of(run_id)) - 1)

    def test_a_failed_start_leaves_the_attempt_and_its_refusal_on_record(self):
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        self.fake_github(self.ACTIVE_PULL)
        self.fake_systemctl()
        (self.root / "fake-bin" / "systemctl").write_text(
            "#!/bin/sh\necho 'Failed to connect to bus' >&2\nexit 1\n")

        self.one_pass(T0 + 20 * MINUTE, StubProvider())

        marks = [f"{k}: {s}" for k, s in self.events_of(run_id)
                 if "launch_loop" in f"{k}: {s}"]
        self.assertEqual(len(marks), 2, marks)
        self.assertTrue(marks[0].startswith("launch_loop_attempt:"), marks)
        self.assertTrue(marks[1].startswith("launch_loop_failed:"), marks)
        self.assertIn("Failed to connect to bus", marks[1])

    def test_a_sweep_that_sent_nothing_back_starts_nothing(self):
        """An open pull request with no activity past the mark sends
        nothing back, and a merged one lands the run rather than sending
        it back: neither is a ticket waiting for a loop."""
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        calls = self.fake_systemctl()
        quiet = dict(self.ACTIVE_PULL, updatedAt="2026-09-01T10:00:00Z",
                     reviewThreads={"totalCount": 1}, comments={"nodes": []})
        self.fake_github(quiet)

        out = self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertNotIn("sent back", out)
        self.assertEqual(calls(), [])
        self.assertNotIn("holophyte-loop@", out)

    def test_a_failed_systemctl_is_printed_and_the_pass_ends_normally(self):
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        self.fake_github(self.ACTIVE_PULL)
        self.fake_systemctl()
        (self.root / "fake-bin" / "systemctl").write_text(
            "#!/bin/sh\necho 'Unit holophyte-loop@repo.service not found.'"
            " >&2\nexit 5\n")

        out = self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertIn(f"run {run_id} sent back to the babysitter", out)
        self.assertIn("holophyte-loop@repo could not be started"
                      " (Unit holophyte-loop@repo.service not found.)", out)
        self.assertEqual(
            self.conn.execute("SELECT lastBeat FROM supervisorHeartbeats")
            .fetchone(), (T0 + 20 * MINUTE,))

    # KO-409: the send-back above is one case of the rule. A ticket ready
    # for any other reason -- an operator's --requeue or --babysit, a
    # filing while the loop was down -- is owed the same start.
    def ready_ticket(self):
        """A ticket `ready` the way `--file-ticket` leaves one: mirrored
        with a contract and never run."""
        self.tickets += 1
        n = self.tickets
        return store.tickets.mirror_ticket(
            self.conn, self.project, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            acceptance_criteria=[f"Given ticket {n}, then it is worked"],
            verification_commands=["echo ok"])

    def test_low_budget_holds_a_loop_start_for_mirrored_ready_work(self):
        import linear_provider
        self.ready_ticket()
        calls = self.fake_systemctl()
        budget = linear_provider.LinearBudget()
        budget.remember({
            "x-ratelimit-complexity-limit": "3000000",
            "x-ratelimit-complexity-remaining": "0",
            "x-ratelimit-complexity-reset": str(T0 + 60 * MINUTE)})
        with patch.object(linear_provider, "LINEAR_BUDGET", budget):
            out = self.one_pass(T0 + 20 * MINUTE, StubProvider())
            self.assertEqual(calls(), [])
            self.assertIn("board not asked: budget resets at", out)
            self.one_pass(T0 + 60 * MINUTE, StubProvider())
        self.assertTrue(calls())

    def test_a_ready_ticket_without_a_send_back_starts_the_loop_unit(self):
        """A pass that sent nothing back but finds a ticket `ready`
        starts the unit; a store holding nothing ready starts nothing."""
        calls = self.fake_systemctl()
        provider = StubProvider()

        quiet = self.one_pass(T0 + 20 * MINUTE, provider)

        self.assertEqual(calls(), [])
        self.assertNotIn("holophyte-loop@", quiet)

        self.ready_ticket()
        asked = provider.ready_asked
        out = self.one_pass(T0 + 21 * MINUTE, provider)

        self.assertEqual(calls(), ["--user start holophyte-loop@repo"])
        self.assertIn("started holophyte-loop@repo", out)
        # A mirror hit owes no board read: the ask is the empty-mirror
        # fall-through's, and the pass that found the ticket `ready`
        # never made it (KO-411).
        self.assertEqual(provider.ready_asked, asked)

    # KO-411: the mirror is a cache of the board. A ticket that became
    # ready while no loop ran has no mirror row, so an empty answer falls
    # through to the board's own `ready_issues()` before nothing is owed.
    def test_a_ready_issue_the_mirror_never_saw_starts_the_loop_unit(self):
        """`--file-ticket`'s case: the board holds a ready issue the
        store has no row for, and the pass starts the unit for it."""
        provider = StubProvider(ready=[{"id": "KO-9", "issue_id": "issue-9"}])
        calls = self.fake_systemctl()

        out = self.one_pass(T0 + 20 * MINUTE, provider)

        self.assertEqual(provider.ready_asked, 1)
        self.assertEqual(calls(), ["--user start holophyte-loop@repo"])
        self.assertIn("started holophyte-loop@repo", out)

    def test_a_board_with_nothing_ready_starts_nothing(self):
        """The fall-through asks once and owes nothing when the board
        agrees with the mirror's empty answer."""
        provider = StubProvider()
        calls = self.fake_systemctl()

        out = self.one_pass(T0 + 20 * MINUTE, provider)

        self.assertEqual(provider.ready_asked, 1)
        self.assertEqual(calls(), [])
        self.assertNotIn("holophyte-loop@", out)

    def test_a_board_that_cannot_be_asked_is_printed_and_starts_nothing(self):
        """A failed board ask prints its error and starts nothing."""
        provider = StubProvider(ready=RuntimeError("Linear is down"))
        calls = self.fake_systemctl()

        out = self.one_pass(T0 + 20 * MINUTE, provider)

        self.assertEqual(provider.ready_asked, 1)
        self.assertIn("Linear is down", out)
        self.assertEqual(calls(), [])

    # KO-434: the fallback's ask is stamped on the projects row and held to
    # `[supervisor] board_ask_sec` -- an empty mirror is not a reason to
    # spend one ready listing a minute on it forever.
    def test_an_empty_mirror_is_not_re_asked_within_board_ask_sec(self):
        """Fallback asks are throttled for ten minutes, then resume."""
        provider = StubProvider()
        self.fake_systemctl()

        self.one_pass(T0 + 20 * MINUTE, provider)
        second = self.one_pass(T0 + 20 * MINUTE + 30_000, provider)

        self.assertEqual(provider.ready_asked, 1)
        self.assertNotIn("holophyte-loop@", second)
        # The stamp is the first ask's instant; the throttled pass left it.
        self.assertEqual(self.conn.execute(
            "SELECT boardAskedAt FROM projects WHERE id = ?",
            (self.project,)).fetchone(), (T0 + 20 * MINUTE,))

        self.one_pass(T0 + 31 * MINUTE, provider)

        self.assertEqual(provider.ready_asked, 2)

    def test_a_low_complexity_budget_asks_nothing_and_says_the_reset_once(
            self):
        """Low budget suppresses board asks and announces each reset only once."""
        import linear_provider
        budget = linear_provider.LinearBudget()
        budget.remember({
            "x-ratelimit-complexity-limit": "3000000",
            "x-ratelimit-complexity-remaining": "200000",
            "x-ratelimit-complexity-reset": str(T0 + 60 * MINUTE)})
        provider = StubProvider()
        self.fake_systemctl()
        clock = time.strftime("%H:%M",
                              time.localtime((T0 + 60 * MINUTE) / 1000))

        with patch.object(linear_provider, "LINEAR_BUDGET", budget):
            first = self.one_pass(T0 + 20 * MINUTE, provider)
            second = self.one_pass(T0 + 21 * MINUTE, provider)

        self.assertEqual(provider.ready_asked, 0)
        self.assertIn(f"board not asked: budget resets at {clock}", first)
        self.assertNotIn("board not asked", second)
        self.assertNotIn("holophyte-loop@", first + second)

    # KO-420: the board's ready column keeps a ticket the store holds
    # parked -- the board never learns about a park. The fall-through
    # subtracts the issues the mirror already holds in a non-ready
    # status, so a board whose only ready issue is a parked one is no
    # tickets owed and no start.
    def test_a_board_issue_the_mirror_holds_parked_owes_no_start(self):
        """The Relos incident's shape: parked on its pull request and
        still in the board's ready column, the ticket counted ready on
        every pass -- a start and a claim's refusal, a minute apart, all
        day. The mirror's `blocked_on_operator` row now subtracts it."""
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        quiet = dict(self.ACTIVE_PULL, updatedAt="2026-09-01T10:00:00Z",
                     reviewThreads={"totalCount": 1}, comments={"nodes": []})
        self.fake_github(quiet)
        provider = StubProvider(
            ready=[{"id": "KO-1", "issue_id": "issue-1"}])
        calls = self.fake_systemctl()

        out = self.one_pass(T0 + 20 * MINUTE, provider)

        self.assertEqual(provider.ready_asked, 1)
        self.assertEqual(calls(), [])
        self.assertNotIn("ready", out)
        self.assertNotIn("holophyte-loop@", out)

    def test_a_parked_issue_and_an_unmirrored_one_owe_a_single_start(self):
        """The subtraction is per issue: the parked one is not owed, the
        one the mirror never saw is, so the pass starts the unit once
        and the line says `1 ticket ready`."""
        run_id = self.parked_on_pr()
        self.seen_before_activity(run_id)
        quiet = dict(self.ACTIVE_PULL, updatedAt="2026-09-01T10:00:00Z",
                     reviewThreads={"totalCount": 1}, comments={"nodes": []})
        self.fake_github(quiet)
        provider = StubProvider(
            ready=[{"id": "KO-1", "issue_id": "issue-1"},
                   {"id": "KO-9", "issue_id": "issue-9"}])
        calls = self.fake_systemctl()

        out = self.one_pass(T0 + 20 * MINUTE, provider)

        self.assertEqual(provider.ready_asked, 1)
        self.assertEqual(calls(), ["--user start holophyte-loop@repo"])
        self.assertIn("1 ticket ready", out)

    def test_a_ready_ticket_under_a_held_lease_turn_starts_nothing(self):
        """A loop between its startup and its first claim's heartbeat
        holds `lease.lock`: a ready ticket under that held turn is the
        booting loop's own, and the pass starts nothing."""
        self.ready_ticket()
        calls = self.fake_systemctl()
        lock = holophyte.board.lease_turn_path(self.tgt)
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

        out = self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertEqual(calls(), [])
        self.assertNotIn("holophyte-loop@", out)

    def test_a_ready_ticket_whose_start_was_taken_is_owed_again(self):
        """The reviewer's repro: an operator's requeue put the ticket back
        `ready`, the supervisor's start was taken and recorded as a
        `launch_loop` row on the run, and the loop it raised exited
        before claiming. An hour on, the ticket is still `ready` with no
        heartbeat and no held turn, so the pass owes it the one start a
        pass allows -- the row is the record of the start, not a reason
        never to start again."""
        run_id = self.a_run(claimed_at=T0)
        ticket = self.ticket_of[run_id]
        store.release(self.conn, run_id, "failed", "the worker gave up",
                      now=T0 + MINUTE)
        store.requeue(self.conn, ticket, "another go", now=T0 + 2 * MINUTE)
        store.record_intervention(
            self.conn, run_id, "launch_loop",
            "the supervisor started holophyte-loop@repo for 1 ticket ready"
            " while no loop was live",
            source="supervisor", trigger="manual", now=T0 + 3 * MINUTE)
        calls = self.fake_systemctl()

        out = self.one_pass(T0 + 60 * MINUTE, StubProvider())

        self.assertEqual(
            self.conn.execute("SELECT status FROM tickets WHERE id = ?",
                              (ticket,)).fetchone(), ("ready",))
        self.assertEqual(calls(), ["--user start holophyte-loop@repo"])
        self.assertIn("started holophyte-loop@repo", out)

    def test_a_live_loop_heartbeat_leaves_the_reconcile_to_the_loop(self):
        """The loop's own tick covers a parked pull request while the loop
        is live, so the supervisor does not ask GitHub twice a minute
        about the same one: a run heartbeating within the stale threshold
        is that liveness."""
        run_id = self.parked_on_pr()
        asked = self.fake_github(self.MERGED_PULL)
        self.a_run(claimed_at=T0 + 19 * MINUTE)

        self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertEqual(asked, [])
        self.assertEqual(self.run_row(run_id),
                         ("awaiting_merge_approval", None, None))

    def test_a_github_error_is_printed_and_the_pass_completes(self):
        run_id = self.parked_on_pr()
        asked = self.fake_github(RuntimeError("GitHub is down"))

        out = self.one_pass(T0 + 20 * MINUTE, StubProvider())

        self.assertEqual(len(asked), 1)
        self.assertIn("GitHub is down", out)
        self.assertIn("stays parked", out)
        self.assertEqual(self.run_row(run_id),
                         ("awaiting_merge_approval", None, None))
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM sweepStrikes").fetchone(),
            (0,))
        (beat,) = self.conn.execute(
            "SELECT passes FROM supervisorHeartbeats").fetchall()
        self.assertEqual(beat, (1,))

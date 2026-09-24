"""The host sweep, `factory.py --supervise [--once]` with no project.

Real stores, real lock files, real SQLite locks and a real killed process:
the fixture registers git repositories under a temporary home with
`project add`, seeds runs straight into their stores, and drives runs of
the sweep with the clock as a parameter. Linear is a stub or nothing;
GitHub's read and `systemctl` are patched where a test reaches them.

Run: python3 -m unittest discover -s tests -p 'test_sweep_host*' -v
"""
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_fixture import REPO, HostFixture  # noqa: E402
from loop_fixture import StubProvider  # noqa: E402
from phase_fixture import advance_phase  # noqa: E402

import holophyte.cli  # noqa: E402
import holophyte.supervisor_lock as supervisor_lock  # noqa: E402
import holophyte.sweep_host as sweep_host  # noqa: E402
import store  # noqa: E402
import store.schema  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.host import Host  # noqa: E402
from holophyte.pr_status import PullStatus  # noqa: E402
from holophyte.project import Project  # noqa: E402
from holophyte.reconcile import GITHUB_BUDGET  # noqa: E402
from holophyte.supervisor import supervisor_liveness_line  # noqa: E402

MINUTE = 60 * 1000
T0 = 1_700_000_000_000


def a_dead_pid():
    """A pid the kernel no longer knows: a child already reaped."""
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def a_live_pid(case):
    """A pid the kernel knows for the test's span: a sleeping child."""
    child = subprocess.Popen(["sleep", "60"])
    case.addCleanup(child.wait)
    case.addCleanup(child.kill)
    return child.pid


class CountingProvider(StubProvider):
    """The board, counting its closed-issue asks."""

    def __init__(self, team):
        super().__init__()
        self.team = team
        self.closed_asks = 0

    def closed_identifiers(self, identifiers):
        self.closed_asks += 1
        return super().closed_identifiers(identifiers)


class HostSweepFixture(HostFixture):
    """Projects registered under a temporary home, the sweep run in-process."""

    NAMES = ("alpha", "beta", "gamma")

    def setUp(self):
        super().setUp()
        self.paths, self.tickets = {}, 0
        for name in self.NAMES:
            self.paths[name] = self.repo(name)
            code, _ = self.cli("project", "add", str(self.paths[name]))
            self.assertEqual(code, None)
        # A store another connection holds answers "locked" in a fifth of
        # a second rather than the store's thirty.
        patcher = patch.object(store.schema, "BUSY_TIMEOUT_S", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.started_loops = []
        patcher = patch("holophyte.supervisor.start_loop_for",
                        lambda target, *a, **k:
                        self.started_loops.append(target.path.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        GITHUB_BUDGET.remaining = GITHUB_BUDGET.reset_at = None

    def target(self, name):
        return Project.locate(self.paths[name], adopt=False)

    def conn(self, name):
        conn = store.open(str(self.target(name).store_path))
        self.addCleanup(conn.close)
        return conn

    def a_run(self, name, claimed_at=T0):
        """A run of a new ticket, working since `claimed_at`."""
        conn = self.conn(name)
        project = conn.execute("SELECT id FROM projects").fetchone()[0]
        self.tickets += 1
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id=f"issue-{self.tickets}",
            linear_identifier=f"KO-{self.tickets}", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)
        store.tickets.transition(conn, ticket, "in_flight")
        run = store.claim(conn, project, ticket, now=claimed_at)
        advance_phase(conn, run, "working", now=claimed_at)
        return run

    def a_failed_run_with_pr(self, name, number):
        """A run that ended failed holding a pull request, its ticket still
        in flight with no live run: KO-722's and KO-723's shape."""
        run = self.a_run(name)
        conn = self.conn(name)
        store.release(conn, run, "failed", "crashed")
        with conn:
            conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?",
                         (f"https://github.com/o/r/pull/{number}", run))
        return run

    def run_once(self, at, provider=None, reads=None, status=None):
        """One `--supervise --once` run at `at`; `(exit, printed)`. GitHub's
        read answers `status`, an open pull request by default, and is
        counted in `reads`."""
        def pull_status(_target, pull):
            (reads if reads is not None else []).append(pull.number)
            return status or PullStatus(merged=False, closed=False)

        out = io.StringIO()
        with patch.object(sweep_host, "_provider", lambda _target: provider), \
                patch("holophyte.pr_status.pull_status", pull_status), \
                patch("holophyte.supervisor.linear_budget_low",
                      return_value=False):
            code = sweep_host.supervise_host(Host.locate(), once=True, out=out,
                                             clock=lambda: at)
        return code, out.getvalue()

    def run_once_with(self, at, pull_status):
        """`run_once()` with GitHub's read replaced by `pull_status`."""
        out = io.StringIO()
        with patch.object(sweep_host, "_provider", lambda _target: None), \
                patch("holophyte.pr_status.pull_status", pull_status):
            code = sweep_host.supervise_host(Host.locate(), once=True, out=out,
                                             clock=lambda: at)
        return code, out.getvalue()

    def state(self):
        return json.loads((self.home / "sweep.json").read_text())

    def beats(self, name):
        return self.conn(name).execute(
            "SELECT pid, lastBeat, passes FROM supervisorHeartbeats"
            " ORDER BY startedAt").fetchall()

    def hold_write_lock(self, name):
        """A real connection holding the store's write lock until cleanup."""
        holder = sqlite3.connect(str(self.target(name).store_path),
                                 isolation_level=None)
        holder.execute("BEGIN EXCLUSIVE")

        def release():
            if not released:
                released.append(holder.execute("ROLLBACK"))
                holder.close()
        released = []
        return release


class HostSweepTests(HostSweepFixture):
    def test_two_stores_beat_across_two_runs_while_a_third_is_locked(self):
        """The stage checkpoint: alpha's silent run trips on the second run
        and is failed, beta is held and still beaten, gamma is locked and is
        that project's error alone; `sweep.json` names all three."""
        silent = self.a_run("alpha")
        beta = self.conn("beta")
        store.set_admission(beta, 1, "held", "operator away")
        release = self.hold_write_lock("gamma")
        self.addCleanup(release)

        first, _ = self.run_once(T0 + 6 * MINUTE)
        second, printed = self.run_once(T0 + 12 * MINUTE)

        self.assertEqual((first, second), (1, 1))
        state = self.state()
        self.assertEqual(state["projects"]["alpha"], "ok")
        self.assertEqual(state["projects"]["beta"], "ok")
        self.assertIn("database is locked", state["projects"]["gamma"])
        self.assertEqual(state["skipped"], {"gamma": 2})
        self.assertEqual((state["ended"], state["exit"]),
                         (T0 + 12 * MINUTE, 1))
        # One sentinel row per store, bumped by both runs.
        for name in ("alpha", "beta"):
            self.assertEqual(self.beats(name), [(0, T0 + 12 * MINUTE, 2)])
        self.assertEqual(
            self.conn("alpha").execute(
                "SELECT phase, outcome FROM runs WHERE id = ?",
                (silent,)).fetchone(), ("failed", "failed"))
        self.assertIn("[alpha] ", printed)
        self.assertNotIn("beta", self.started_loops)
        release()
        self.assertEqual(self.beats("gamma"), [])
        # The host `--status` names each project's last outcome, and the
        # store's liveness line names the host sweep, never a pid 0.
        _, status = self.cli("--status")
        self.assertIn("[alpha] last sweep: ok", status)
        self.assertIn("[gamma] last sweep: error: store unavailable", status)
        self.assertRegex(supervisor_liveness_line(self.target("beta")),
                         r"\(host sweep on [^)]+\)$")

    def test_a_store_locked_three_runs_is_unavailable_until_it_beats(self):
        release = self.hold_write_lock("gamma")
        outcomes = []
        for minute in range(4):
            self.run_once(T0 + minute * MINUTE)
            outcomes.append(self.state()["projects"]["gamma"])
        release()
        code, _ = self.run_once(T0 + 4 * MINUTE)

        self.assertIn("run 1 of 3", outcomes[0])
        self.assertIn("run 2 of 3", outcomes[1])
        self.assertTrue(outcomes[2].startswith("error: unavailable"))
        self.assertTrue(outcomes[3].startswith("error: unavailable"))
        self.assertEqual(code, 0)
        self.assertEqual(self.state()["projects"]["gamma"], "ok")
        self.assertEqual(self.state()["skipped"], {})
        self.assertEqual(self.beats("gamma"), [(0, T0 + 4 * MINUTE, 1)])

    def test_per_project_locks_live_skips_and_dead_is_reclaimed(self):
        live = a_live_pid(self)
        alpha_lock = supervisor_lock.supervisor_lock_path(self.target("alpha"))
        beta_lock = supervisor_lock.supervisor_lock_path(self.target("beta"))
        supervisor_lock.acquire_supervisor_lock(alpha_lock, "alpha", pid=live)
        dead = a_dead_pid()
        beta_lock.write_text(f"somewhere {dead} {T0}\n")

        code, printed = self.run_once(T0)

        self.assertEqual(code, 1)
        alpha = self.state()["projects"]["alpha"]
        self.assertIn(f"pid {live}", alpha)
        self.assertIn(str(alpha_lock), alpha)
        self.assertEqual(self.beats("alpha"), [])
        self.assertEqual(supervisor_lock.read_supervisor_lock(alpha_lock)[0],
                         live)
        self.assertFalse(beta_lock.exists())
        self.assertIn(f"removed per-project supervisor lock {beta_lock}:"
                      f" pid {dead} is gone", printed)
        self.assertEqual(self.state()["projects"]["beta"], "ok")
        self.assertEqual(self.beats("beta"), [(0, T0, 1)])

    def test_a_disabled_or_rowless_project_is_skipped_and_not_an_error(self):
        store.set_admission(self.conn("alpha"), 1, "disabled", "retired")
        with self.conn("beta") as conn:
            for table in ("runEvents", "interventions", "projects"):
                conn.execute(f"DELETE FROM {table}")

        code, _ = self.run_once(T0)

        projects = self.state()["projects"]
        self.assertEqual(code, 0)
        self.assertEqual(projects["alpha"], "skipped: disabled: retired")
        self.assertTrue(projects["beta"].startswith("skipped: no project row"))
        self.assertIn("project add", projects["beta"])
        self.assertEqual(self.conn("beta").execute(
            "SELECT count(*) FROM projects").fetchone(), (0,))
        self.assertEqual(self.beats("alpha") + self.beats("beta"), [])
        self.assertEqual(projects["gamma"], "ok")


class HomeLockTests(HostSweepFixture):
    """The home lock is the per-target lock's code at `HOLOPHYTE_HOME`."""

    NAMES = ("alpha",)

    def lock(self):
        return self.home / "supervisor.lock"

    def test_a_second_run_beside_a_live_one_exits_naming_the_pid(self):
        holder = a_live_pid(self)
        supervisor_lock.acquire_supervisor_lock(self.lock(), self.home,
                                                pid=holder, now=T0)

        code, printed = self.cli("--supervise", "--once")

        self.assertEqual(code, 1)
        self.assertIn(f"pid {holder}", printed)
        self.assertIn(str(self.lock()), printed)
        self.assertEqual(supervisor_lock.read_supervisor_lock(self.lock())[0],
                         holder)
        self.assertFalse((self.home / "sweep.json").exists())

    def test_a_dead_runs_lock_is_reclaimed_and_the_run_released_it(self):
        self.lock().write_text(f"somewhere {a_dead_pid()} {T0}\n")

        code, _ = self.run_once(T0)

        self.assertEqual(code, 0)
        self.assertFalse(self.lock().exists())
        self.assertEqual(self.beats("alpha"), [(0, T0, 1)])

    def test_two_starters_reclaiming_one_stale_lock_admit_only_one(self):
        """Both read the same dead pid; the rival reclaims and locks in the
        gap before this starter's unlink. One is admitted, the other is
        refused naming it."""
        lock = self.lock()
        lock.write_text(f"{a_dead_pid()} {T0}\n")
        us, rival = os.getpid(), os.getpid() + 1
        real_unlink, real_alive = os.unlink, supervisor_lock.pid_alive
        rival_outcome, fired = [], []

        def rival_starts():
            try:
                rival_outcome.append(supervisor_lock.acquire_supervisor_lock(
                    lock, self.home, pid=rival, now=T0 + 1))
            except supervisor_lock.SupervisorHeld as held:
                rival_outcome.append(held)

        def unlink_with_a_rival_in_the_gap(path, *args, **kwargs):
            if not fired and Path(path) == lock:
                fired.append(threading.Thread(target=rival_starts))
                fired[0].start()
                fired[0].join(0.5)
            return real_unlink(path, *args, **kwargs)

        with patch.object(supervisor_lock, "pid_alive",
                          lambda pid: pid in (us, rival) or real_alive(pid)), \
                patch.object(os, "unlink", unlink_with_a_rival_in_the_gap):
            try:
                ours = supervisor_lock.acquire_supervisor_lock(
                    lock, self.home, pid=us, now=T0)
            except supervisor_lock.SupervisorHeld as held:
                ours = held
            fired[0].join(5)

        outcomes = {us: ours, rival: rival_outcome[0]}
        admitted = [who for who, got in outcomes.items()
                    if not isinstance(got, Exception)]
        self.assertEqual(len(admitted), 1, outcomes)
        self.assertEqual(supervisor_lock.read_supervisor_lock(lock)[0],
                         admitted[0])
        refused = outcomes[rival if admitted == [us] else us]
        self.assertEqual(refused.pid, admitted[0])

    def test_the_loop_form_holds_the_lock_for_its_life_without_re_exec(self):
        runs = []

        def stop_after_two(_interval):
            runs.append(self.lock().exists())
            if len(runs) == 2:
                os.kill(os.getpid(), signal.SIGTERM)

        before = signal.getsignal(signal.SIGTERM)
        with patch.object(sweep_host, "_provider", lambda _target: None):
            code = sweep_host.supervise_host(
                Host.locate(), out=io.StringIO(), wait=stop_after_two)

        self.assertEqual((code, runs), (0, [True, True]))
        self.assertFalse(self.lock().exists())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)
        self.assertEqual([passes for _pid, _at, passes
                          in self.beats("alpha")], [2])


# Run in a child process: one host sweep run whose reconcile of beta hangs
# after touching a marker, until the test kills it.
HANGING_RUN = """
import sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import holophyte.sweep_host as sweep_host
from holophyte.host import Host

def reconcile(entry, seen, state, now, out):
    if entry.name == "beta":
        Path(sys.argv[2]).touch()
        time.sleep(60)

with patch.object(sweep_host, "reconcile_store", reconcile):
    sweep_host.supervise_host(Host.locate(), once=True)
"""


class KilledRunTests(HostSweepFixture):
    NAMES = ("alpha", "beta")

    def test_a_killed_run_is_reported_and_the_next_resumes_at_the_cursor(self):
        marker = self.root / "hanging"
        child = subprocess.Popen(
            [sys.executable, "-c", HANGING_RUN, str(REPO), str(marker)],
            env=dict(os.environ), stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.monotonic() + 30
        while not marker.exists() and child.poll() is None \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        if not marker.exists():
            child.kill()
            self.fail(child.communicate()[1].decode())
        child.kill()
        child.wait()
        child.stderr.close()

        killed = self.state()
        self.assertIsInstance(killed["started"], int)
        self.assertIsNone(killed["ended"])
        self.assertEqual(killed["reconcile_cursor"], "beta")
        self.assertEqual(
            supervisor_lock.read_supervisor_lock(self.home / "supervisor.lock")
            [0], child.pid)

        order = []
        with patch.object(sweep_host, "reconcile_store",
                          lambda entry, *_: order.append(entry.name)):
            code, printed = self.run_once(T0)

        self.assertEqual(code, 0)
        self.assertIn(f"started at {killed['started']} as pid {child.pid}"
                      " did not end; the reconcile resumes at beta", printed)
        self.assertEqual(order, ["beta", "alpha"])
        state = self.state()
        self.assertEqual(state["interrupted"],
                         {"started": killed["started"], "pid": child.pid})
        self.assertEqual((state["ended"], state["exit"]), (T0, 0))
        self.assertEqual(state["reconcile_cursor"], "alpha")


class ThrottleTests(HostSweepFixture):
    """What a run must remember lives in `sweep.json`, not in the process:
    two runs a minute apart ask once, and the same two runs with the file
    removed between them ask twice."""

    NAMES = ("alpha",)

    def two_runs(self, forget, **run):
        self.run_once(T0, **run)
        GITHUB_BUDGET.remaining = GITHUB_BUDGET.reset_at = None
        if forget:
            (self.home / "sweep.json").unlink()
        self.run_once(T0 + MINUTE, **run)

    def test_failed_run_pull_request_reads_ko_722(self):
        run = self.a_failed_run_with_pr("alpha", 7)
        for forget, expected in ((False, [7]), (True, [7, 7])):
            with self.subTest(forget=forget):
                reads = []
                (self.home / "sweep.json").unlink(missing_ok=True)
                self.two_runs(forget, reads=reads)
                self.assertEqual(reads, expected)
        self.assertIn(str(run), self.state()["failed_asked"]["alpha"])

    def test_board_close_asks_ko_723(self):
        self.a_failed_run_with_pr("alpha", 8)
        for forget, expected in ((False, 1), (True, 2)):
            with self.subTest(forget=forget):
                provider = CountingProvider("team-alpha")
                (self.home / "sweep.json").unlink(missing_ok=True)
                self.two_runs(forget, provider=provider)
                self.assertEqual(provider.closed_asks, expected)

    def test_a_low_github_budget_outlives_the_run_that_read_it(self):
        self.a_failed_run_with_pr("alpha", 9)
        low = PullStatus(merged=False, closed=False, rate_remaining=10,
                         rate_reset="2999-01-01T00:00:00Z")
        reads = []
        self.run_once(T0, reads=reads, status=low)
        # A fresh process forgets the reading, and the pull request is due
        # a read again: only `sweep.json` can stop the second one.
        GITHUB_BUDGET.remaining = GITHUB_BUDGET.reset_at = None
        state = self.state()
        state["failed_asked"] = {}
        (self.home / "sweep.json").write_text(json.dumps(state))
        _, printed = self.run_once(T0 + MINUTE, reads=reads)

        self.assertEqual(reads, [9])
        self.assertEqual(self.state()["github_budget"],
                         {"remaining": 10, "reset_at": "2999-01-01T00:00:00Z"})
        self.assertIn("budget is down to 10", printed)


class DeadlineTests(HostSweepFixture):
    NAMES = ("alpha", "beta")

    def test_a_slow_read_cuts_the_project_at_its_next_call_and_holds_the_cursor(
            self):
        """`sweep_sec = 2` leaves the reconcile one second, half each: the
        first read takes longer than that, so the second is never made,
        beta gets nothing, and the next run starts at alpha."""
        with open(self.home / "host.toml", "a") as registry:
            registry.write("\n[supervisor]\nsweep_sec = 2\n")
        self.a_failed_run_with_pr("alpha", 1)
        self.a_failed_run_with_pr("alpha", 2)
        reads = []

        def slow(_target, pull):
            reads.append(pull.number)
            time.sleep(1.1)
            return PullStatus(merged=False, closed=False)

        code, printed = self.run_once_with(T0, slow)

        self.assertEqual(code, 0)
        self.assertEqual(reads, [1])
        self.assertIn("[holo2] alpha: reconcile cut, share spent before", printed)
        self.assertEqual(self.state()["reconcile_cursor"], "alpha")


class RegisteredProjectTests(HostSweepFixture):
    """A registered project is the host sweep's: the loop spawns no
    supervisor for it and a hand `PROJECT --supervise` is refused."""

    NAMES = ("alpha",)

    def test_the_loop_spawns_nothing_for_a_registered_project(self):
        spawned = []
        loose = self.repo("loose")
        with patch.object(holophyte.cli, "SPAWN",
                          lambda argv, **_: spawned.append(argv) or
                          type("Child", (), {"pid": 1})()):
            out = io.StringIO()
            holophyte.cli.start_supervisor(self.target("alpha"), out=out)
            holophyte.cli.start_supervisor(Project.locate(loose), out=out)

        self.assertIn(f"the host sweep watches {self.paths['alpha']}",
                      out.getvalue())
        self.assertEqual([argv[-1] for argv in spawned], [str(loose)])

    def test_the_sweep_writes_no_projects_row_for_a_team_it_lacks(self):
        """The board fallback asks only for a team with a row: a store the
        loop never opened under this board's team is left as it is."""
        provider = CountingProvider("a-team-with-no-row")
        code, _ = self.run_once(T0, provider=provider)
        self.assertEqual(code, 0)
        self.assertEqual(self.conn("alpha").execute(
            "SELECT linearTeamId FROM projects").fetchall(), [("team-alpha",)])
        self.assertEqual(self.started_loops, [])

    def test_a_hand_supervise_of_a_registered_project_is_refused(self):
        with patch.object(holophyte.cli, "supervise",
                          lambda *a: self.fail("supervised a registered"
                                               " project")), \
                self.assertRaises(SystemExit) as refused:
            self.cli(str(self.paths["alpha"]), "--supervise")
        self.assertIn(str(self.home / "host.toml"), str(refused.exception))
        self.assertFalse(supervisor_lock.supervisor_lock_path(
            self.target("alpha")).exists())


if __name__ == "__main__":
    import unittest
    unittest.main()

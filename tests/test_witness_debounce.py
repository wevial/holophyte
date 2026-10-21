"""A loop's witness child waits for main to be quiet before it starts, and a
triggering commit cancels a running child, at most twice before a pass
completes, so its set restarts on the newest tip."""
from __future__ import annotations

import io
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
from loop_fixture import ROOT, VALID_BODY, LoopFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.pool  # noqa: E402
import holophyte.story.witness  # noqa: E402
import holophyte.story.witness_trigger  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.config.checks import check_config  # noqa: E402
from holophyte.host.supervisor_lock import pid_alive  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.story.witness import witness_step  # noqa: E402
from provider import board_for  # noqa: E402
from store.stories import approve_story, file_story  # noqa: E402
from tests.test_native_loop import NATIVE, no_linear  # noqa: E402
from tests.test_witness_runner import PASSES, PYTHON  # noqa: E402

POOL = "[loop]\nworkers = 2\n"
FIXTURE = "fixture/"
MARKER = "fixture/marker"
LANDED = PASSES + "\n# landed on main\n"
REAL_ROWS = ("SELECT witnessKey, mainSha, verdict FROM witnessResults"
             " WHERE carriedFrom IS NULL ORDER BY id")


class _Ticked(BaseException):
    """The scheduler reached its first wait: the end of its first tick."""


class _Stop(BaseException):
    """The scripted ticks ran out."""


class _DebounceFixture(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.use("")

    def use(self, story_table):
        self.configure(NATIVE + POOL + story_table)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)

    def approved_story(self, witnesses=None, witness_paths=()):
        parent, child = (
            self.conn.execute(
                "SELECT id FROM tickets WHERE linearIdentifier = ?",
                (store.board.file_ticket(self.conn, self.project_id, "NAT",
                                         VALID_BODY, column="backlog"),),
            ).fetchone()[0]
            for _ in range(2))
        witnesses = witnesses or [("W1", f"{PYTHON} -m unittest tests.test_w1")]
        file_story(self.conn, parent, [{
            "key": key, "criterion": f"{key} holds",
            "file": f"tests/test_{key.lower()}.py", "command": command,
            "source": PASSES} for key, command in witnesses],
            [(child, "completes", [key for key, _ in witnesses])],
            witness_paths=witness_paths)
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")

    def first_tick(self):
        """The command lines the scheduler spawned before its first wait."""
        spawned = []

        def spawn(argv, **kwargs):
            spawned.append(argv)
            return type("Child", (), {"pid": 5001})()

        def stop(*args, **kwargs):
            raise _Ticked

        with patch.object(holophyte.loop.pool, "SPAWN", spawn), \
                patch.object(holophyte.loop.pool, "WAIT", stop), \
                patch.object(holophyte.loop.pool, "sleep", stop), \
                patch.object(sys, "orig_argv",
                             ["python3", "factory.py", str(self.target)]), \
                patch.object(sys, "stdout", io.StringIO()):
            try:
                holophyte.cli.operator.main(self.project, self.board)
            except _Ticked:
                return [argv[-1] for argv in spawned]
        self.fail(f"the scheduler returned on its first tick after {spawned}")

    def commit_at(self, when, message, write=(), remove=()):
        """Commit with committer and author time `when`, epoch seconds."""
        for path, text in write:
            (self.target / path).parent.mkdir(parents=True, exist_ok=True)
            (self.target / path).write_text(text)
        for path in remove:
            self.git("rm", "-q", path)
        self.git("add", "-A")
        stamp = f"@{when} +0000"
        subprocess.run(["git", "commit", "-q", "-m", message],
                       cwd=self.target, check=True, capture_output=True,
                       env=dict(os.environ, GIT_COMMITTER_DATE=stamp,
                                GIT_AUTHOR_DATE=stamp))
        return self.git("rev-parse", "main").strip()



class QuietWindowTests(_DebounceFixture):
    def test_a_tip_younger_than_the_default_window_starts_no_witness_child(self):
        self.approved_story()
        age = time.time() - int(self.git("log", "-1", "--format=%ct", "main"))
        self.assertLess(age, 300)

        self.assertEqual(self.first_tick(), [])

    def test_a_configured_window_starts_a_child_on_a_tip_older_than_it(self):
        self.approved_story()
        self.commit_at(int(time.time()) - 31, "a tip 31 s old",
                       write=[("README.md", "older\n")])

        self.assertEqual(self.first_tick(), [])
        self.use("[story]\nwitness_quiet_sec = 30\n")
        self.assertEqual(self.first_tick(), ["--witness-step"])

    def test_a_zero_window_starts_a_child_on_a_tip_a_second_old(self):
        self.use("[story]\nwitness_quiet_sec = 0\n")
        self.approved_story()
        self.commit_at(int(time.time()) - 1, "a tip a second old",
                       write=[("README.md", "newer\n")])

        self.assertEqual(self.first_tick(), ["--witness-step"])

    def test_a_window_that_is_not_whole_seconds_is_a_startup_error(self):
        for value in ("-1", "true", '"5m"'):
            with self.subTest(value=value):
                self.configure(NATIVE + f"[story]\nwitness_quiet_sec = {value}\n")
                with self.assertRaises(SystemExit) as raised:
                    check_config(self.project)
                self.assertIn("[story] witness_quiet_sec", str(raised.exception))

    def test_a_git_timeout_reading_the_tip_time_counts_as_quiet(self):
        tip = self.git("rev-parse", "main").strip()
        quiet = holophyte.story.witness_trigger.quiet
        self.assertFalse(quiet(self.project, tip))

        with patch.object(holophyte.story.witness_trigger, "GIT_TIMEOUT", 1e-6):
            self.assertTrue(quiet(self.project, tip))


class _Scheduler:
    """The real pool scheduler spawning real `--witness-step` children, its
    waits and sleeps each answered by the next scripted step."""

    def __init__(self, test):
        self.test = test
        self.children = []
        self.spawned_before = []
        self.out = io.StringIO()

    def spawn(self, argv, **kwargs):
        log = open(self.test.target.parent / f"child-{len(self.children)}.log",
                   "w")
        self.test.addCleanup(log.close)
        child = subprocess.Popen(argv, cwd=ROOT, stdout=log,
                                 stderr=subprocess.STDOUT, **kwargs)
        self.test.addCleanup(_kill, child)
        self.children.append(child)
        return child

    def run(self, *steps):
        script = list(steps)

        def answer(*args, **kwargs):
            self.spawned_before.append(len(self.children))
            if not script:
                raise _Stop
            return script.pop(0)() or (None, None)

        with patch.object(holophyte.loop.pool, "SPAWN", self.spawn), \
                patch.object(holophyte.loop.pool, "WAIT", answer), \
                patch.object(holophyte.loop.pool, "sleep", answer), \
                patch.object(sys, "orig_argv", [sys.executable,
                                                str(ROOT / "factory.py"),
                                                str(self.test.target)]), \
                patch.object(sys, "stdout", self.out):
            try:
                return holophyte.cli.operator.main(self.test.project,
                                                   self.test.board)
            except _Stop:
                return None


def _kill(child):
    if child.poll() is None:
        child.kill()
        child.wait()


class WitnessDebounceTests(_DebounceFixture):
    def setUp(self):
        super().setUp()
        self.now = float(int(time.time()) - 100000)
        clock = patch.object(holophyte.story.witness_trigger, "time",
                             lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.w2_log = self.target.parent / "w2.log"
        self.when = int(self.now)

    def land(self, message, write=(), remove=()):
        """A commit 1000 s after the last one; the clock just after it."""
        self.when += 1000
        sha = self.commit_at(self.when, message, write, remove)
        self.now = self.when + 1
        return sha

    def quiet(self):
        self.now = self.when + 300

    def blocking_story(self):
        w2 = (f'echo "$$ $(git rev-parse HEAD) $(pwd)" >>'
              f" {shlex.quote(str(self.w2_log))};"
              f" while [ -e {MARKER} ]; do sleep 0.1; done;"
              f" {PYTHON} -m unittest tests.test_w2")
        self.approved_story([("W1", f"{PYTHON} -m unittest tests.test_w1"),
                             ("W2", w2)], witness_paths=[FIXTURE])
        return self.land("witnesses and a marker", write=[
            ("tests/test_w1.py", LANDED), ("tests/test_w2.py", LANDED),
            (MARKER, "block\n"), (f"{FIXTURE}page.txt", "0\n")])

    def w2_runs(self):
        if not self.w2_log.exists():
            return []
        return [line.split(" ", 2) for line in self.w2_log.read_text().splitlines()]

    def w2_blocking(self, count):
        """W2's `count`th run has started, so W1 has finished before it."""
        deadline = time.monotonic() + 120
        while len(self.w2_runs()) < count:
            self.assertLess(time.monotonic(), deadline, "W2 never started")
            time.sleep(0.1)
        pid, sha, cwd = self.w2_runs()[count - 1]
        return int(pid), sha, Path(cwd)

    def real_rows(self):
        return self.read(REAL_ROWS)

    def test_a_burst_of_merges_gets_one_child_once_its_last_commit_is_quiet(self):
        self.approved_story(witness_paths=[FIXTURE])
        base = self.land("w1 and a fixture", write=[
            ("tests/test_w1.py", LANDED), (f"{FIXTURE}page.txt", "0\n")])
        with patch.object(sys, "stdout", io.StringIO()):
            witness_step(self.project, self.conn, self.project_id)
        self.assertEqual(self.real_rows(), [("W1", base, "green")])
        self.land("a", write=[(f"{FIXTURE}page.txt", "a\n")])
        tips = []
        scheduler = _Scheduler(self)

        def b_lands():
            self.when += 60
            tips.append(self.commit_at(self.when, "b",
                                       write=[(f"{FIXTURE}page.txt", "b\n")]))
            self.now = self.when + 1

        def nearly_quiet():
            self.now = self.when + 299

        def child_exits():
            (child,) = scheduler.children
            return child.pid, child.wait(timeout=120)

        rc = scheduler.run(b_lands, nearly_quiet, self.quiet, child_exits)

        self.assertEqual(rc, 0, scheduler.out.getvalue())
        self.assertEqual(scheduler.spawned_before, [0, 0, 0, 1])
        self.assertEqual(scheduler.children[0].returncode, 0)
        self.assertEqual(self.real_rows(), [("W1", base, "green"),
                                            ("W1", tips[0], "green")])

    def test_a_trigger_cancels_the_running_child_and_its_set_reruns_at_the_tip(self):
        a = self.blocking_story()
        self.quiet()
        scheduler = _Scheduler(self)
        tips, killed = [], []

        def b_lands_unblocking_w2():
            killed.append(self.w2_blocking(1)[0])
            tips.append(self.land("b", remove=[MARKER]))

        def child_exits_at_once():
            child = scheduler.children[0]
            started = time.monotonic()
            code = child.wait(timeout=10)
            while pid_alive(killed[0]) and time.monotonic() - started < 10:
                time.sleep(0.05)
            self.assertFalse(pid_alive(killed[0]))
            self.assertEqual(self.read("SELECT mainSha FROM witnessResults"
                                       f" WHERE mainSha = '{a}'"), [])
            return child.pid, code

        def restarted_child_exits():
            child = scheduler.children[1]
            return child.pid, child.wait(timeout=120)

        rc = scheduler.run(b_lands_unblocking_w2, child_exits_at_once,
                           self.quiet, restarted_child_exits)

        self.assertEqual(rc, 0, scheduler.out.getvalue())
        self.assertEqual(scheduler.spawned_before, [1, 1, 1, 2])
        self.assertEqual(self.read("SELECT witnessKey, mainSha, verdict,"
                                   " carriedFrom FROM witnessResults ORDER BY id"),
                         [("W1", tips[0], "green", None),
                          ("W2", tips[0], "green", None)])

    def test_a_commit_outside_the_witness_paths_leaves_the_running_child(self):
        a = self.blocking_story()
        self.quiet()
        scheduler = _Scheduler(self)

        def docs_land():
            self.w2_blocking(1)
            self.land("docs", write=[("README.md", "more\n")])

        def child_runs_on():
            child = scheduler.children[0]
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=2)
            (self.w2_blocking(1)[2] / MARKER).unlink()
            return child.pid, child.wait(timeout=120)

        scheduler.run(docs_land, child_runs_on)

        self.assertEqual(scheduler.children[0].returncode, 0)
        self.assertEqual(self.real_rows(), [("W1", a, "green"),
                                            ("W2", a, "green")])

    def test_a_third_trigger_lets_the_run_finish_until_a_pass_completes(self):
        self.blocking_story()
        self.quiet()
        scheduler = _Scheduler(self)
        tips = {}

        def trigger(run, name):
            def step():
                self.w2_blocking(run)
                tips[name] = self.land(name, write=[(f"{FIXTURE}page.txt",
                                                     f"{name}\n")])
            return step

        def cancelled(run):
            def step():
                child = scheduler.children[run - 1]
                return child.pid, child.wait(timeout=10)
            return step

        def let_run():
            child = scheduler.children[2]
            with self.assertRaises(subprocess.TimeoutExpired):
                child.wait(timeout=2)
            (self.w2_blocking(3)[2] / MARKER).unlink()
            return child.pid, child.wait(timeout=120)

        scheduler.run(trigger(1, "b"), cancelled(1), self.quiet,
                      trigger(2, "c"), cancelled(2), self.quiet,
                      trigger(3, "d"), let_run, self.quiet,
                      trigger(4, "e"), cancelled(4))

        out = scheduler.out.getvalue()
        self.assertEqual([child.returncode for child in scheduler.children],
                         [143, 143, 0, 143], out)
        self.assertEqual([sha for _, sha, _ in self.w2_runs()],
                         [self.git("rev-parse", "main~4").strip(), tips["b"],
                          tips["c"], tips["d"]])
        self.assertEqual(self.real_rows(), [("W1", tips["c"], "green"),
                                            ("W2", tips["c"], "green")])
        self.assertEqual(out.count("cancelled the witness pass"), 3, out)
        self.assertEqual(out.count("letting the witness pass"), 1, out)

    def test_a_scheduler_that_cancels_an_inherited_child_restarts_the_pass(self):
        self.blocking_story()
        self.quiet()
        inherited = subprocess.Popen(
            [sys.executable, str(ROOT / "factory.py"), str(self.target),
             "--witness-step"], cwd=ROOT, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        self.addCleanup(_kill, inherited)
        self.w2_blocking(1)
        b = self.land("b", remove=[MARKER])
        scheduler = _Scheduler(self)

        def inherited_exits():
            self.assertEqual(inherited.wait(timeout=10), 143)

        def restarted_child_exits():
            (child,) = scheduler.children
            return child.pid, child.wait(timeout=120)

        rc = scheduler.run(inherited_exits, self.quiet, restarted_child_exits)

        self.assertEqual(rc, 0, scheduler.out.getvalue())
        self.assertEqual(scheduler.spawned_before, [0, 0, 1])
        self.assertEqual(self.real_rows(), [("W1", b, "green"),
                                            ("W2", b, "green")])

    def test_an_inherited_child_at_the_restart_cap_is_waited_for(self):
        self.blocking_story()
        self.quiet()
        before = _Scheduler(self)
        tips = {}

        def trigger(run, name):
            def step():
                self.w2_blocking(run)
                tips[name] = self.land(name, write=[(f"{FIXTURE}page.txt",
                                                     f"{name}\n")])
            return step

        def cancelled(run):
            def step():
                child = before.children[run - 1]
                return child.pid, child.wait(timeout=10)
            return step

        before.run(trigger(1, "b"), cancelled(1), self.quiet,
                   trigger(2, "c"), cancelled(2), self.quiet)
        inherited = before.children[2]
        self.w2_blocking(3)
        d = self.land("d", write=[(f"{FIXTURE}page.txt", "d\n")],
                      remove=[MARKER])
        after = _Scheduler(self)

        def inherited_finishes():
            with self.assertRaises(subprocess.TimeoutExpired):
                inherited.wait(timeout=2)
            (self.w2_blocking(3)[2] / MARKER).unlink()
            self.assertEqual(inherited.wait(timeout=120), 0)

        def next_child_exits():
            (child,) = after.children
            return child.pid, child.wait(timeout=120)

        rc = after.run(inherited_finishes, self.quiet, next_child_exits)

        self.assertEqual(rc, 0, after.out.getvalue())
        self.assertEqual(after.spawned_before, [0, 0, 1])
        self.assertEqual(self.real_rows(), [
            ("W1", tips["c"], "green"), ("W2", tips["c"], "green"),
            ("W1", d, "green"), ("W2", d, "green")])

    def test_a_cancel_as_the_witness_command_starts_kills_it_before_unwinding(self):
        self.blocking_story()
        real = holophyte.story.witness._verify_command
        commands = []

        def cancel_as_it_starts(project, command, cwd, timeout, on_start=None):
            def start(proc):
                if MARKER in command:
                    commands.append(proc)
                    os.kill(os.getpid(), signal.SIGTERM)
                on_start(proc)
            return real(project, command, cwd, timeout, on_start=start)

        marker = Path(self.project.holo_dir) / "witness-tree" / MARKER
        release = threading.Timer(15, marker.unlink, kwargs={"missing_ok": True})
        release.start()
        self.addCleanup(release.cancel)
        started = time.monotonic()
        with patch.object(holophyte.story.witness, "_verify_command",
                          cancel_as_it_starts), \
                patch.object(sys, "stdout", io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            holophyte.loop.pool.witness_child(self.project, self.board)

        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        (command,) = commands
        self.assertEqual(command.returncode, -signal.SIGKILL)
        self.assertEqual(self.real_rows(), [])

    def test_a_cancel_during_a_checkout_waits_for_the_step_and_its_timeout(self):
        self.use("[story]\nwitness_sec = 2\n")
        self.blocking_story()
        flag = self.target.parent / "hook.pid"
        hook = self.target / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\necho $$ > {shlex.quote(str(flag))}\n"
                        "sleep 30\n")
        hook.chmod(0o755)

        def cancel_in_the_hook():
            deadline = time.monotonic() + 30
            while not flag.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            os.kill(os.getpid(), signal.SIGTERM)

        canceller = threading.Thread(target=cancel_in_the_hook)
        started = time.monotonic()
        canceller.start()
        with patch.object(sys, "stdout", io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            holophyte.loop.pool.witness_child(self.project, self.board)
        canceller.join()

        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(raised.exception.code, 128 + signal.SIGTERM)
        self.assertTrue(flag.exists())
        self.assertEqual(self.real_rows(), [])

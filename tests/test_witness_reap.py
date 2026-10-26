"""A witness command's processes do not outlive the pass that ran it.

Run: python3 -m unittest discover -s tests -p 'test_witness_reap.py' -v
"""
import contextlib
import io
import os
import random
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import store
from holophyte.story import witness_reap
from holophyte.story.witness import main_tip, run_witnesses
from holophyte.story.witness_reap import reap_stale
from tests.test_witness_runner import (
    PASSES,
    PYTHON,
    WitnessRunnerFixture,
    commit_file,
    git,
)

FACTORY = Path(__file__).resolve().parent.parent / "factory.py"
NATIVE = '[board]\nkind = "native"\nkey = "KO"\n'

PROC = Path("/proc")


def carrying(token):
    pids = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if token.encode() in argv:
            pids.append(int(entry.name))
    return pids


def kill_carrying(token):
    for pid in carrying(token):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def wait_for(ready, limit=30.0):
    until = time.monotonic() + limit
    while not ready() and time.monotonic() < until:
        time.sleep(0.1)
    return ready()


def alive_carrying(token, settle=2.0):
    until = time.monotonic() + settle
    while carrying(token) and time.monotonic() < until:
        time.sleep(0.1)
    return carrying(token)


class TokenFixture(WitnessRunnerFixture):
    def setUp(self):
        super().setUp()
        self.token = f"300.{random.randrange(10**9):09d}"
        self.addCleanup(kill_carrying, self.token)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        tempdir = patch.object(tempfile, "tempdir", str(self.scratch))
        tempdir.start()
        self.addCleanup(tempdir.stop)


@unittest.skipUnless(PROC.is_dir(), "reading survivors needs /proc")
class WitnessCommandTreeTests(TokenFixture, unittest.TestCase):
    def test_a_green_command_leaves_no_descendant_running(self):
        token = shlex.quote(self.token)
        ready = shlex.quote(str(self.root / "detached"))
        detached = shlex.quote(
            "import os, sys, time; os.setsid();"
            " open(sys.argv[1], 'w').close(); time.sleep(300)")
        command = (f"sleep {token} >/dev/null 2>&1 &"
                   f" {PYTHON} -c {detached} {ready} {token} >/dev/null 2>&1 &"
                   f" while [ ! -e {ready} ]; do sleep 0.1; done; exit 0")
        target = self.project()
        self.approve([("W1", "tests/test_w1.py", command, PASSES)])

        rows = run_witnesses(target, self.conn, self.story_id,
                             main_tip(target), "operator", copy_files=True)

        self.assertEqual([row.verdict for row in rows], ["green"])
        self.assertEqual(list(self.scratch.glob("witness-*")), [])
        self.assertEqual(
            len(git(self.repo, "worktree", "list").splitlines()), 1)
        self.assertEqual(alive_carrying(self.token), [])

    def test_a_command_past_the_budget_leaves_no_descendant_running(self):
        token = shlex.quote(self.token)
        target = self.project("[story]\nwitness_sec = 3\n")
        self.approve([("W1", "tests/test_w1.py",
                       f"sh -c 'sleep {token} & sleep {token}'", PASSES)])

        rows = run_witnesses(target, self.conn, self.story_id,
                             main_tip(target), "operator", copy_files=True)

        self.assertEqual([row.verdict for row in rows], ["error"])
        self.assertIn("timed out", Path(rows[0].evidencePath).read_text())
        self.assertEqual(alive_carrying(self.token), [])

    def test_a_chain_spawned_as_the_tree_is_stopped_is_stopped_to_its_end(self):
        tree = self.root / "tree"
        tree.mkdir()
        ready, chain = self.root / "ready", self.root / "chain.py"
        chain.write_text(
            "import os, signal, subprocess, sys, time\n"
            "def successor(*_):\n"
            "    if int(sys.argv[3]):\n"
            "        subprocess.Popen([sys.executable, *sys.argv[:3],"
            " str(int(sys.argv[3]) - 1)], start_new_session=True)\n"
            "    os._exit(0)\n"
            "signal.signal(signal.SIGTERM, successor)\n"
            "open(sys.argv[1], 'a').close()\n"
            "time.sleep(300)\n")
        first = subprocess.Popen(
            [sys.executable, str(chain), str(ready), self.token, "8"],
            cwd=tree, start_new_session=True)
        self.addCleanup(first.wait)
        self.assertTrue(wait_for(ready.exists))

        witness_reap.stop(None, tree)

        self.assertEqual(alive_carrying(self.token), [])

    def test_a_process_beside_the_tree_is_left_running(self):
        tree = self.root / "tree"
        tree.mkdir()
        beside = Path(f"{self.root / 'backup'}{tree}")
        beside.mkdir(parents=True)
        sleeper = subprocess.Popen(["sleep", self.token], cwd=beside)
        self.addCleanup(sleeper.wait)
        self.addCleanup(lambda: sleeper.poll() is None and sleeper.kill())
        self.assertTrue(wait_for(lambda: carrying(self.token)))

        witness_reap.stop(None, tree)

        self.assertIsNone(sleeper.poll())


@unittest.skipUnless(PROC.is_dir(), "reading survivors needs /proc")
class SignalledPassTests(TokenFixture, unittest.TestCase):
    def operator_pass(self, command):
        """`factory.py --witness-pass` as a real child, its store the
        project's own and its temporary directory the test's scratch."""
        target = self.project(NATIVE)
        self.conn.close()
        self.conn = store.open(target.store_path)
        self.addCleanup(self.conn.close)
        commit_file(self.repo, "tests/test_w1.py", PASSES, "w1 lands")
        self.approve([("W1", "tests/test_w1.py", command, PASSES)])
        env = {key: value for key, value in os.environ.items()
               if key != "LINEAR_API_KEY"}
        child = subprocess.Popen(
            [sys.executable, str(FACTORY), str(self.repo), "--witness-pass",
             "KO-1"], env=dict(env, TMPDIR=str(self.scratch)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child

    def assert_signal_stops_the_pass(self, signum):
        token = shlex.quote(self.token)
        child = self.operator_pass(f"sh -c 'sleep {token} & sleep {token}'")
        self.assertTrue(wait_for(lambda: carrying(self.token)))

        child.send_signal(signum)

        child.wait(timeout=witness_reap.STOP_GRACE + 10)
        self.assertEqual(alive_carrying(self.token), [])
        self.assertEqual(list(self.scratch.glob("witness-*")), [])
        self.assertEqual(
            len(git(self.repo, "worktree", "list").splitlines()), 1)

    def test_sighup_to_the_runner_stops_the_command_and_removes_its_tree(self):
        self.assert_signal_stops_the_pass(signal.SIGHUP)

    def test_sigterm_to_the_runner_stops_the_command_and_removes_its_tree(self):
        self.assert_signal_stops_the_pass(signal.SIGTERM)

    def test_a_stopped_command_s_descendant_gets_sigterm_first(self):
        ready, termed = self.root / "ready", self.root / "termed"
        descendant = shlex.quote(
            f"trap 'touch {termed}; exit 0' TERM; touch {ready};"
            " while :; do sleep 0.1; done")
        child = self.operator_pass(f"sh -c {descendant} & wait")
        self.assertTrue(wait_for(ready.exists))

        child.send_signal(signal.SIGTERM)

        child.wait(timeout=witness_reap.STOP_GRACE + 10)
        self.assertTrue(termed.exists())


@unittest.skipUnless(PROC.is_dir(), "finding a tree's processes needs /proc")
class ReapStaleTests(TokenFixture, unittest.TestCase):
    def scratch_dir(self, name, owner=None):
        path = self.scratch / name
        path.mkdir()
        if owner is not None:
            (path / "owner").write_text(f"{owner}\n")
        return path

    def sleeper(self, cwd):
        proc = subprocess.Popen(["sleep", self.token], cwd=cwd)
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def test_a_dead_owner_s_tree_is_reaped_and_a_live_or_young_one_kept(self):
        target = self.project()
        exited = subprocess.Popen(["true"])
        exited.wait()
        dead = self.scratch_dir("witness-dead", owner=exited.pid)
        git(self.repo, "worktree", "add", "-q", "--detach", str(dead / "tree"))
        live = self.scratch_dir("witness-live", owner=os.getpid())
        (live / "tree").mkdir()
        young = self.scratch_dir("witness-young")
        git(self.repo, "worktree", "add", "-q", "--detach", str(young / "tree"))
        dead_sleep = self.sleeper(dead / "tree")
        live_sleep = self.sleeper(live / "tree")
        self.assertTrue(wait_for(lambda: len(carrying(self.token)) == 2))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            reap_stale(target)

        self.assertFalse(dead.exists())
        self.assertEqual(dead_sleep.wait(timeout=5), -signal.SIGTERM)
        self.assertNotIn(str(dead / "tree"), git(self.repo, "worktree", "list"))
        self.assertEqual(out.getvalue().splitlines(), [
            f"[holo2] reaped the leftover witness tree {dead}: 1 process"
            " stopped"])
        self.assertIsNone(live_sleep.poll())
        self.assertTrue((live / "tree").is_dir())
        self.assertTrue(young.is_dir())

        aged = time.time() - 601
        os.utime(young, (aged, aged))
        with contextlib.redirect_stdout(io.StringIO()):
            reap_stale(target)

        self.assertFalse(young.exists())
        self.assertIsNone(live_sleep.poll())
        self.assertTrue((live / "tree").is_dir())

    def test_a_pass_reaps_only_a_stale_tree_of_its_own_repository(self):
        target = self.project()
        self.approve([("W1", "tests/test_w1.py", "true", PASSES)])
        exited = subprocess.Popen(["true"])
        exited.wait()
        own = self.scratch_dir("witness-own", owner=exited.pid)
        git(self.repo, "worktree", "add", "-q", "--detach", str(own / "tree"))
        notes = self.scratch_dir("witness-notes")
        (notes / "tree").mkdir()
        (notes / "tree" / "notes.txt").write_text("kept\n")
        aged = time.time() - 601
        os.utime(notes, (aged, aged))
        other_repo = self.root / "other"
        git(self.root, "init", "-q", str(other_repo))
        commit_file(other_repo, "a.txt", "a\n", "initial")
        other = self.scratch_dir("witness-other", owner=exited.pid)
        git(other_repo, "worktree", "add", "-q", "--detach",
            str(other / "tree"))

        with contextlib.redirect_stdout(io.StringIO()):
            rows = run_witnesses(target, self.conn, self.story_id,
                                 main_tip(target), "operator", copy_files=True)

        self.assertEqual([row.verdict for row in rows], ["green"])
        self.assertFalse(own.exists())
        self.assertNotIn(str(own / "tree"), git(self.repo, "worktree", "list"))
        self.assertEqual((notes / "tree" / "notes.txt").read_text(), "kept\n")
        self.assertTrue((other / "tree" / "a.txt").is_file())
        self.assertIn(str(other / "tree"), git(other_repo, "worktree", "list"))

    def test_a_loop_pass_stops_what_runs_in_the_warm_tree_before_its_witness(self):
        target = self.project()
        warm = Path(target.holo_dir) / "witness-tree"
        pid_file, snapshot = self.root / "sleep-pid", self.root / "sleep-stat"
        self.approve([("W1", "tests/test_w1.py",
                       f"cat /proc/$(cat {pid_file})/stat > {snapshot} 2>&1;"
                       " true", PASSES)])
        with contextlib.redirect_stdout(io.StringIO()):
            run_witnesses(target, self.conn, self.story_id, main_tip(target),
                          "loop", copy_files=True)
        leftover = self.sleeper(warm)
        self.assertTrue(wait_for(lambda: carrying(self.token)))
        pid_file.write_text(f"{leftover.pid}\n")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rows = run_witnesses(target, self.conn, self.story_id,
                                 main_tip(target), "loop", copy_files=True)

        self.assertEqual([row.verdict for row in rows], ["green"])
        state = snapshot.read_text().rpartition(")")[2].split()[:1]
        self.assertIn(state, ([], ["Z"]))
        self.assertIn(f"[holo2] the warm witness tree {warm} had 1 process"
                      " left running: stopped", out.getvalue().splitlines())


if __name__ == "__main__":
    unittest.main()

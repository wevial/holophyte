"""Mechanical baseline verification and the merge lock."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
from tests.fake_agent import APPROVE, Commit  # noqa: E402
from tests.loop_fixture import LoopFixture  # noqa: E402


class VerifyBlockTests(unittest.TestCase):
    def test_compound_block_preserves_errexit(self):
        with tempfile.TemporaryDirectory() as cwd:
            ok, out = holophyte.gates.run_verify(
                "set -e; false; touch should-not-run\ntrue", cwd)
            self.assertFalse((Path(cwd) / "should-not-run").exists())
            self.assertFalse(ok, out)
            self.assertEqual(out.failure["exit_status"], 1)

    def test_block_stops_at_first_failure_and_names_its_status(self):
        with tempfile.TemporaryDirectory() as cwd:
            ok, out = holophyte.gates.run_verify(
                "false\ntouch should-not-run && true", cwd)
            self.assertFalse(ok, out)
            self.assertIn("clause 1 of 2 exited 1", out)
            self.assertIn("failing clause: false", out)
            self.assertEqual(out.failure["command_index"], 1)
            self.assertEqual(out.failure["exit_status"], 1)
            self.assertFalse((Path(cwd) / "should-not-run").exists())

    def test_passing_blocks_preserve_shell_state_and_explicit_tolerance(self):
        with tempfile.TemporaryDirectory() as cwd:
            (Path(cwd) / "sub").mkdir()
            (Path(cwd) / "sub" / "value").write_text("carried")
            commands = (
                'export VERIFY_VALUE=carried && cd sub\n'
                'test "$(cat value)" = "$VERIFY_VALUE"',
                "false || true\nprintf tolerated",
                "printf one\nprintf two\nprintf three",
                "true && printf chained",
                "cat <<'EOF'\nheredoc\nEOF",
                "printf single",
            )
            for command, expected in zip(
                    commands, ("", "tolerated", "one\ntwo\nthree",
                               "chained", "heredoc", "single")):
                with self.subTest(command=command):
                    ok, out = holophyte.gates.run_verify(command, cwd)
                    self.assertTrue(ok, out)
                    self.assertEqual(out, expected)

    def test_baseline_blocks_stop_at_first_failure_in_both_tiers(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as cwd:
            for tier in ("always", "before_merge"):
                with self.subTest(tier=tier):
                    config = {"verify": {tier: [
                        "false\ntouch should-not-run && true"]}}
                    target = SimpleNamespace(config=lambda: config)
                    ok, out = holophyte.gates.with_baseline(
                        target, cwd, "true", True, "", before_merge=True)
                    self.assertFalse(ok, out)
                    self.assertIn("clause 1 of 2 exited 1", out)
                    self.assertEqual(out.results[-1]["tier"], tier)
                    self.assertEqual(out.results[-1]["exitCode"], 1)
                    self.assertFalse((Path(cwd) / "should-not-run").exists())


class RepeatedPassTests(unittest.TestCase):
    """A verify that passed earlier in the same run, on the same head with
    the same `main`, over a clean worktree, is cited instead of run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wt = Path(tmp.name, "wt")
        self.wt.mkdir()
        self.count = Path(tmp.name, "count")
        self.cmd = f"echo ran >> {shlex.quote(str(self.count))}"
        self.git("init", "-q", "-b", "main")
        self.commit("base")
        self.git("checkout", "-q", "-b", "task")
        self.commit("candidate")
        record = patch.object(holophyte.gates, "_PASSES", set())
        record.start()
        self.addCleanup(record.stop)

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
             *args], cwd=self.wt, check=True, capture_output=True,
            text=True).stdout.strip()

    def commit(self, message):
        self.git("commit", "-q", "--allow-empty", "-m", message)

    def runs(self):
        return len(self.count.read_text().splitlines())

    def verify(self, run_id=7, cmd=None):
        return holophyte.gates.run_verify(cmd or self.cmd, self.wt,
                                          run_id=run_id)

    def test_a_repeat_on_the_same_tree_is_cited_not_run(self):
        self.assertTrue(self.verify()[0])
        ok, out = self.verify()
        self.assertTrue(ok, out)
        self.assertEqual(self.runs(), 1)
        self.assertIn(self.git("rev-parse", "HEAD")[:12], out)
        self.assertIn(self.git("rev-parse", "main")[:12], out)
        self.assertIn("not run again", out)

    def test_a_changed_key_or_dirty_tree_runs_the_command_again(self):
        def new_commit():
            self.commit("fix")

        def main_moves():
            self.git("checkout", "-q", "main")
            self.commit("elsewhere")
            self.git("checkout", "-q", "task")

        def uncommitted():
            (self.wt / "scratch").write_text("x")

        cases = (("a new commit", new_commit, 7), ("main moved", main_moves, 7),
                 ("an uncommitted change", uncommitted, 7),
                 ("another run", lambda: None, 8), ("no run", lambda: None, None))
        for name, change, run_id in cases:
            with self.subTest(name), patch.object(holophyte.gates, "_PASSES", set()):
                self.count.write_text("")
                self.assertTrue(self.verify()[0])
                change()
                ok, out = self.verify(run_id)
                self.assertTrue(ok, out)
                self.assertEqual(self.runs(), 2)
                (self.wt / "scratch").unlink(missing_ok=True)

    def test_a_failure_is_not_recorded(self):
        cmd = self.cmd + "; exit 1"
        self.assertFalse(self.verify(cmd=cmd)[0])
        self.assertFalse(self.verify(cmd=cmd)[0])
        self.assertEqual(self.runs(), 2)


class MergeLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "repo").mkdir()
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        holophyte.project.state_dir(root / "repo").mkdir(parents=True)
        self.tgt = holophyte.project.Project.locate(root / "repo")

    def test_the_second_gate_waits_for_the_first_to_release(self):
        """Two runs reach the gate together: one holds the lock while the
        other polls, and the second enters only after the first has left."""
        first_in = threading.Event()
        spans = {}

        def gate(run_id, hold):
            with holophyte.gates.merge_lock(self.tgt, run_id, wait=10,
                                            poll=0.01):
                entered = time.monotonic()
                if run_id == 1:
                    first_in.set()
                time.sleep(hold)
                spans[run_id] = (entered, time.monotonic())

        one = threading.Thread(target=gate, args=(1, 0.3))
        two = threading.Thread(target=gate, args=(2, 0.0))
        one.start()
        self.assertTrue(first_in.wait(5))
        two.start()
        one.join(5)
        two.join(5)

        self.assertEqual(sorted(spans), [1, 2])
        self.assertGreaterEqual(spans[2][0], spans[1][1])
        self.assertFalse(holophyte.gates.merge_lock_path(self.tgt).exists())

    def test_a_lock_held_past_the_bound_names_its_holder(self):
        path = holophyte.gates.merge_lock_path(self.tgt)
        path.write_text(f"7 {time.time():.3f}\n")

        with self.assertRaises(holophyte.gates.MergeLockHeld) as caught:
            with holophyte.gates.merge_lock(self.tgt, 8, wait=0.05, poll=0.01):
                self.fail("the gate entered under another run's lock")

        self.assertIn("run 7", str(caught.exception))
        self.assertIsInstance(caught.exception, holophyte.gates.InfraFailure)
        self.assertEqual(holophyte.gates.read_merge_lock(path)[0], 7)


class BaselineTests(LoopFixture):
    def test_failed_baseline_records_both_sources_before_review(self):
        self.configure('[verify]\nalways = ["echo baseline-broke; exit 7"]\n')
        output = self.main_output(Commit("candidate"), APPROVE, Commit("fix"), APPROVE,
                                  Commit("last fix"))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn("baseline-broke", output)
        rows = json.loads(self.read(
            "SELECT verificationResults FROM reviewRounds ORDER BY id LIMIT 1")[0][0])
        self.assertEqual([(r["source"], r["exitCode"]) for r in rows],
                         [("ticket", 0), ("baseline", 1)])
        self.assertEqual(rows[1]["tier"], "always")

    def test_verify_config_document_rejects_bad_shapes(self):
        from holophyte.config import check_document
        for setting in ('always = "true"', 'before_merge = [1]',
                        'always = [" "]', 'timeout_sec = 0',
                        'timeout_sec = true', 'timeout_sec = inf',
                        'timeot_sec = 30'):
            with self.subTest(setting=setting):
                self.configure('[verify]\n' + setting + '\n')
                with self.assertRaisesRegex(SystemExit, r"\[verify\]"):
                    check_document(self.tgt)
        self.configure('[verify]\nalways = ["missing-program"]\n'
                       'before_merge = ["exit 1"]\ntimeout_sec = 12\n')
        check_document(self.tgt)


class BaselineBriefTests(unittest.TestCase):
    def test_baseline_only_success_and_failure_are_visible_to_reviewer(self):
        from holophyte.loop import _verify_brief
        with tempfile.TemporaryDirectory() as wt:
            target = type("Project", (), {"config": lambda self: {
                "verify": {"always": ["echo baseline-detail"]}}})()
            for command, ok_expected in (("echo baseline-detail", True),
                                         ("echo baseline-detail; exit 1", False)):
                with self.subTest(command=command):
                    with patch.object(target, "config", return_value={
                            "verify": {"always": [command]}}):
                        ok, out = holophyte.gates.with_baseline(
                            target, wt, "", True, "")
                    self.assertEqual(ok, ok_expected)
                    brief = _verify_brief("", ok, out)
                    self.assertIn("(1 commands)", brief)
                    self.assertIn("PASSED" if ok else "FAILED", brief)
                    self.assertIn("baseline-detail", brief)
            self.assertEqual(_verify_brief("", True, ""), "")


class IsolatedVerifyTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace

        from holophyte.isolation_git import git

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.enterContext(patch.dict(os.environ,
                                     HOLOPHYTE_HOME=str(self.root / 'state')))
        main = self.root / 'main'
        main.mkdir()
        git(main, 'init', '-q', '-b', 'main')
        git(main, 'config', 'user.name', 'Configured Author')
        git(main, 'config', 'user.email', 'author@example.test')
        git(main, 'commit', '--allow-empty', '-qm', 'base')
        self.wt = self.root / 'worktree'
        git(main, 'worktree', 'add', '-qb', 'task', str(self.wt))
        self.outside = self.root / 'host-only'
        self.outside.write_text('host secret')
        self.config = {'agents': {'implementer_isolation': 'container'}}
        self.target = SimpleNamespace(path=main, config=lambda: self.config)

    def test_verify_and_baseline_cannot_read_host_file(self):
        from holophyte import gates, isolation
        command = f'cat {self.outside}'
        self.config['verify'] = {'always': [command]}

        def filesystem_runner(argv, cwd, timeout, *, env):
            mounts = [argv[i + 1] for i, v in enumerate(argv) if v == '--volume']
            self.assertEqual(len(mounts), 1)
            source, destination, mode = mounts[0].split(':')
            workspace = Path(source).resolve()
            self.assertTrue(workspace.is_dir())
            self.assertTrue(workspace.is_relative_to(
                holophyte.project.state_dir(self.target.path).resolve()))
            self.assertNotEqual(workspace, self.wt.resolve())
            self.assertEqual(Path(cwd).resolve(), workspace)
            self.assertEqual((destination, mode), ('/workspace', 'rw'))
            self.assertFalse(workspace.is_relative_to(Path.home().resolve()))
            self.assertFalse(self.outside.resolve().is_relative_to(workspace))
            self.assertIn('--workdir=/workspace', argv)
            self.assertEqual(argv[-3:], ['/bin/sh', '-c', command])
            self.assertNotIn('HOST_SECRET', env)
            # The fake container resolves absolute paths only through its mounts.
            path = Path(argv[-1].removeprefix('cat '))
            for mount in mounts:
                source, destination, _ = mount.split(':')
                if path.is_relative_to(destination):
                    return 0, (Path(source) / path.relative_to(destination)).read_text()
            return 1, f'cat: {path}: No such file or directory\n'

        with (patch.object(isolation, 'image_ready'),
              patch.object(isolation.review_runner, '_remove_container'),
              patch.dict(os.environ, HOST_SECRET='private'),
              patch.object(isolation, 'run_capped',
                           side_effect=filesystem_runner) as run):
            ok, out = gates.run_verify(command, self.wt, target=self.target)
            self.assertFalse(ok)
            ok, recorded = gates.with_baseline(self.target, self.wt, command, ok, out)
            self.assertIn('No such file', recorded.results[0]['output'])
            self.assertEqual(recorded.results[0]['exitCode'], 1)
            ok, baseline = gates.run_baseline(self.target, self.wt, 'always')
            self.assertFalse(ok)
            self.assertIn('No such file', baseline.results[0]['output'])
            self.assertEqual(run.call_count, 2)

    def test_timeout_removes_named_container_and_preserves_output(self):
        import subprocess

        from holophyte import gates, isolation
        expired = subprocess.TimeoutExpired('docker', 3, output='started\n')
        with (patch.object(isolation, 'image_ready'),
              patch.object(isolation.review_runner, '_remove_container') as remove,
              patch.object(isolation, 'run_capped', side_effect=expired) as run):
            ok, out = gates.run_verify('sleep 20', self.wt, timeout=3,
                                       target=self.target)
        self.assertFalse(ok)
        self.assertIn('started', out)
        self.assertIn('timed out', out.lower())
        argv = run.call_args.args[0]
        remove.assert_called_once_with(argv[argv.index('--name') + 1],
                                       env={'PATH': os.defpath})

    def test_none_preserves_runner_call(self):
        from holophyte import gates
        for agents in ({}, {'implementer_isolation': 'none'}):
            self.config['agents'] = agents
            with patch.object(gates, 'run_capped', return_value=(0, 'done')) as run:
                self.assertEqual(gates.run_verify('echo done', self.wt, timeout=17,
                                                 target=self.target), (True, 'done'))
            run.assert_called_once_with('echo done', self.wt, 17)

    @unittest.skipUnless(os.environ.get('HOLOPHYTE_TEST_DOCKER') == '1',
                         'set HOLOPHYTE_TEST_DOCKER=1 for container integration')
    def test_real_container_cannot_read_host_file(self):
        import shutil
        import subprocess

        from holophyte import gates
        if not shutil.which('docker') or subprocess.run(
                ['docker', 'info'], capture_output=True).returncode:
            self.skipTest('Docker unavailable')
        ok, out = gates.run_verify(f'cat {self.outside}', self.wt, target=self.target)
        self.assertFalse(ok)
        self.assertIn('No such file', out)
        self.assertNotIn('host secret', out)


if __name__ == "__main__":
    unittest.main()

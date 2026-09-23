"""The critic seat's startup probe: absent without `[agents.critic]`, run in
a throwaway detached checkout of `main` when configured, and never able to
stop the loop.

Run: python3 -m unittest tests.test_critic -v
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.agents
import holophyte.harness
import holophyte.pool
import holophyte.project
from holophyte.agent_routes import reset, routes

# The fake codex: records its cwd, the HEAD there and whether HEAD is
# detached, then answers the probe, or exits 1 when told to.
FAKE_CODEX = """
import json, os, subprocess, sys
git = lambda *args: subprocess.run(["git", *args], capture_output=True, text=True)
with open(os.environ["FAKE_CRITIC_CALLS"], "a") as calls:
    calls.write(json.dumps({
        "cwd": os.getcwd(), "head": git("rev-parse", "HEAD").stdout.strip(),
        "detached": git("symbolic-ref", "-q", "HEAD").returncode != 0}) + "\\n")
if os.environ.get("FAKE_CRITIC_FAIL"):
    print("error: the critic is down")
    sys.exit(1)
print("ready")
"""


class CriticProbeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("commit", "--allow-empty", "-qm", "base")
        self.main = self.git("rev-parse", "main")
        self.holo = root / "holo"
        self.holo.mkdir()
        self.fake = root / "codex"
        self.fake.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
        self.fake.chmod(0o755)
        self.calls = root / "calls.jsonl"
        env = patch.dict("os.environ", {"FAKE_CRITIC_CALLS": str(self.calls)})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             *args], cwd=self.repo, text=True).strip()

    def target(self, config):
        (self.holo / "config.toml").write_text(config)
        project = holophyte.project.Project(
            path=self.repo, holo_dir=self.holo, store_path=self.holo / "store.db",
            config_path=self.holo / "config.toml",
            worktrees=self.repo.parent / "repo.worktrees")
        self.addCleanup(reset, project)
        return project

    def start(self, project, **options):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            started = holophyte.agents.startup_routes(
                project, SimpleNamespace(team="test"), **options)
        return started, printed.getvalue()

    def critic_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def critic(self):
        return self.target(f'[agents.critic]\n[harnesses]\ncodex = "{self.fake}"\n')

    def test_no_critic_table_means_no_seat_and_no_probe(self):
        project = self.target("")
        self.assertIsNone(holophyte.harness.critic_seat(project))
        with patch.object(holophyte.agents, "critic_workspace") as workspace:
            started, printed = self.start(project)
        self.assertTrue(started)
        workspace.assert_not_called()
        self.assertFalse(self.calls.exists())
        self.assertNotIn("critic", printed)

    def test_probe_runs_in_a_detached_checkout_of_main_then_removes_it(self):
        started, printed = self.start(self.critic())
        self.assertTrue(started)
        self.assertIn("[holo2] critic probe passed", printed)
        [call] = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(call["head"], self.main)
        self.assertTrue(call["detached"])
        self.assertNotEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        self.assertFalse(Path(call["cwd"]).exists())
        self.assertNotIn(call["cwd"], self.git("worktree", "list"))

    def test_a_failed_probe_turns_the_critic_off_but_starts_the_loop(self):
        project = self.critic()
        with patch.dict("os.environ", {"FAKE_CRITIC_FAIL": "1"}):
            started, printed = self.start(project)
        self.assertTrue(started)
        self.assertIn("critic probe failed (exit 1)", printed)
        self.assertIn(
            "[holo2] critic route down; claims skip the relevance check", printed)
        self.assertTrue(routes(project).critic_failed)
        [call] = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(Path(call["cwd"]).exists())

    def test_a_scheduler_hands_its_failed_probe_to_the_workers_it_spawns(self):
        project = self.critic()
        with patch.dict("os.environ", {"FAKE_CRITIC_FAIL": "1"}):
            started, _ = self.start(project, activate=False)
        self.assertTrue(started)
        spawned = []

        def spawn(argv, env, **_):
            spawned.append(env)
            return SimpleNamespace(pid=1)
        with patch.object(holophyte.pool, "SPAWN", spawn), \
                contextlib.redirect_stdout(io.StringIO()):
            holophyte.pool._spawn_worker(project, 1)
        self.assertEqual(spawned[0].get(holophyte.pool.CRITIC_DOWN_ENV), "1")

    def test_a_worker_with_a_writer_keeps_the_critic_off_without_a_probe(self):
        writer = self.repo.parent / "writer.sh"
        writer.write_text("#!/bin/sh\necho ready\n")
        writer.chmod(0o755)
        project = self.target(f'[agents]\nwriter = "{writer}"\n[agents.critic]\n'
                              f'[harnesses]\ncodex = "{self.fake}"\n')
        seen = []

        def claim(target, _):
            seen.append(routes(target).critic_failed)
        with patch.dict("os.environ", {holophyte.pool.CRITIC_DOWN_ENV: "1"}), \
                patch.object(holophyte.pool, "_worker", claim), \
                contextlib.redirect_stdout(io.StringIO()):
            holophyte.pool.worker(project, SimpleNamespace(team="test"))
        self.assertEqual(seen, [True])
        self.assertEqual(self.critic_calls(), [])


if __name__ == "__main__":
    unittest.main()

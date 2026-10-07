"""The `[agents] trimmer` seat: a trim turn on its own route, real git, real scripts.

Run: python3 -m unittest discover -s tests -p 'test_trimmer_seat.py' -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, FakeAgent  # noqa: E402
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
from holophyte.agents.roles import agent, effective_role  # noqa: E402
from holophyte.loop.trim import trim  # noqa: E402
from tests.test_harness import CONFIG, FAKE_CLAUDE  # noqa: E402

TRIMMED = "[trim]\nbudget_min = 1\n"
PROBE_ANSWER = ('case "$last" in "Reply with the single word"*) '
                'echo ready; exit 0;; esac\n')


def lines(count):
    return "".join(f"line {n}\n" for n in range(count))


def script(path, body):
    path.write_text('#!/bin/sh\nfor last; do :; done\n' + body)
    path.chmod(0o755)
    return path


class TrimmerTurnTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "main"],
                     ["config", "user.name", "Test"],
                     ["config", "user.email", "test@example.invalid"],
                     ["commit", "--allow-empty", "-qm", "base"],
                     ["checkout", "-qb", "task"]):
            self.git(*args)
        (self.repo / "work.txt").write_text(lines(60))
        self.git("add", "work.txt")
        self.git("commit", "-qm", "work")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
        claude.chmod(0o755)
        self.implementer_calls = self.root / "calls.jsonl"
        env = patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_HARNESS_CALLS": str(self.implementer_calls)})
        env.start()
        self.addCleanup(env.stop)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def project(self, agents):
        holo = self.root / "holo"
        holo.mkdir()
        (holo / "config.toml").write_text(CONFIG + agents + TRIMMED)
        target = holophyte.config.project.Project(
            path=self.repo, holo_dir=holo, store_path=holo / "store.db",
            config_path=holo / "config.toml", worktrees=self.root / "wt")
        self.conn = store.open(target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "test", self.repo)
        ticket = store.mirror_ticket(self.conn, project, "KO-1", "KO-1", "trim",
                                     acceptance_criteria=["trimmed"],
                                     verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket)
        return target

    def payloads(self, kind):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = ? ORDER BY seq", (kind,))]

    def test_a_configured_trimmer_runs_in_the_worktree_and_its_commit_is_the_head(self):
        cwd, goal = self.root / "cwd", self.root / "goal"
        trimmer = script(self.root / "trimmer", (
            f'pwd > {cwd}\nprintf %s "$last" > {goal}\n'
            'echo "trimmed" >> work.txt\n'
            'git commit -qam "trim: comments"\n'))
        target = self.project(f'[agents]\ntrimmer = "{trimmer}"\n')
        head = trim(target, self.conn, self.run, 60, self.repo,
                    self.git("rev-parse", "main"), self.git("rev-parse", "HEAD"),
                    "true", [])
        self.assertEqual(head, self.git("rev-parse", "HEAD"))
        self.assertEqual(self.git("log", "-1", "--format=%s"), "trim: comments")
        self.assertEqual(Path(cwd.read_text().strip()).resolve(),
                         self.repo.resolve())
        self.assertTrue(goal.read_text().startswith("Trim the change"))
        self.assertFalse(self.implementer_calls.exists())
        self.assertEqual([p["role"] for p in self.payloads("agent_turn")],
                         ["trim"])
        [result] = self.payloads("trim_result")
        self.assertEqual(result["outcome"], "kept")


class TrimmerOnly(FakeAgent):
    """Scripted turns, except a trim turn, which runs the configured trimmer."""

    def __call__(self, target, role, goal, cwd, **kwargs):
        if effective_role(target, role) == "trim":
            return agent(target, role, goal, cwd, **kwargs)
        return super().__call__(target, role, goal, cwd, **kwargs)


class TrimmerRouteLoopTests(LoopFixture):
    WORK = Commit("work", path="work.txt", body=lines(60))

    def run_loop(self, agents, fake):
        self.configure(f"[agents]\n{agents}{TRIMMED}")
        fake, _ = self.loop(fake=fake, provider=StubProvider(a_task()))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        [(payload,)] = self.read(
            "SELECT payload FROM runEvents WHERE kind = 'trim_result'")
        return fake, json.loads(payload)

    def scripts(self):
        bin_dir = self.target.parent / "bin"
        bin_dir.mkdir(exist_ok=True)
        return bin_dir

    def test_a_trimmer_down_at_startup_skips_trim_and_the_run_goes_on(self):
        bin_dir = self.scripts()
        down = script(bin_dir / "trimmer", "exit 1\n")
        spare = script(bin_dir / "spare-trimmer", "echo unavailable\n")
        fake, result = self.run_loop(
            f'trimmer = "{down}"\ntrimmer_fallback = "{spare}"\n',
            FakeAgent(self.WORK, APPROVE))
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual((result["outcome"], result["reason"]),
                         ("skipped", "trimmer route down"))

    def test_an_outage_with_no_fallback_reverts_the_trim_and_the_run_goes_on(self):
        trimmer = script(self.scripts() / "claude-trim", PROBE_ANSWER + (
            'echo "trimmed" >> work.txt\n'
            'git commit -qam "trim: comments"\n'
            "echo \"You've hit your limit · resets 3am\"\n"
            "exit 1\n"))
        fake, result = self.run_loop(f'trimmer = "{trimmer}"\n',
                                     TrimmerOnly(self.WORK, APPROVE))
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn("the route failed", result["reason"])
        self.assertIn("You've hit your limit", result["reason"])
        work = self.git("rev-parse", "main^2").strip()
        self.assertEqual(fake.turns[1].candidate_sha, work)
        self.assertNotIn("trim: comments", self.subjects())


if __name__ == "__main__":
    unittest.main()

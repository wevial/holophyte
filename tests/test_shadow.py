"""The shadow implementer: one implementation of a run's ticket on its own
branch and worktree, recorded as a `shadow_result` event on the run.

Run: python3 -m unittest discover -s tests -p 'test_shadow.py' -v
"""
import json
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.agents.agent_routes
import holophyte.agents.harness
import holophyte.config.checks
import holophyte.config.project
import review_runner
import store
from holophyte.agents.probes import PROBE_GOAL
from holophyte.loop.shadow import ShadowBrief, run_shadow

DOCS = Path(__file__).resolve().parents[1] / "docs"
IDENTITY = ["-c", "user.name=Shadow", "-c", "user.email=shadow@example.invalid"]
RESULT = {"type": "result", "result": "done", "num_turns": 2,
          "total_cost_usd": 0.5,
          "usage": {"input_tokens": 11, "output_tokens": 5}}

HARNESS = """
import json, subprocess, sys
if sys.argv[-1] == {probe!r}:
    {probe_reply}
{turn}
print({result!r})
"""
ANSWERS_PROBE = 'print(json.dumps({"type": "result", "result": "ready"})); sys.exit(0)'


def commits(text):
    return (f"open('done.txt', 'w').write({text!r})\n"
            f"subprocess.run(['git', *{IDENTITY!r}, 'add', 'done.txt'], check=True)\n"
            f"subprocess.run(['git', *{IDENTITY!r}, 'commit', '-qm', 'done'],"
            " check=True)\n")


class ShadowRun:
    REVIEW = "VERDICT: APPROVE"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", *IDENTITY, "commit", "--allow-empty", "-qm", "base"],
                       cwd=self.repo, check=True)
        self.base = self.git("rev-parse", "HEAD")
        holo = self.root / "holo"
        holo.mkdir()
        self.target = holophyte.config.project.Project(
            path=self.repo, holo_dir=holo, store_path=holo / "store.db",
            config_path=holo / "config.toml", worktrees=self.root / "repo.worktrees")
        self.conn = store.open(self.target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "test", self.repo)
        ticket = store.mirror_ticket(self.conn, project, "KO-7", "KO-7", "shadow",
                                     acceptance_criteria=["done"],
                                     verification_commands=["true"])
        self.run_id = store.claim(self.conn, project, ticket)
        self.wt = self.root / "repo.worktrees" / "ko-7-thing.shadow"
        self.review = self.enterContext(patch.object(
            review_runner, "run_review", return_value=self.REVIEW))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def configure(self, turn="", probe_reply=ANSWERS_PROBE, extra=""):
        binary = self.root / "claude-shadow"
        binary.write_text(f"#!{sys.executable}\n" + HARNESS.format(
            probe=PROBE_GOAL, probe_reply=probe_reply, turn=turn,
            result=json.dumps(RESULT)))
        binary.chmod(0o755)
        self.target.config_path.write_text(
            '[agents.implementer_shadow]\nharness = "claude"\nmodel = "sonnet"\n'
            f'effort = "high"\n{extra}[harnesses]\nclaude = "{binary}"\n')

    def shadow(self, seconds=60, verify="grep -qx ok done.txt"):
        return run_shadow(self.target, self.conn, self.run_id, ShadowBrief(
            goal="Create done.txt saying ok", ticket=self.TICKET,
            criteria=self.CRITERIA, task_id="KO-7",
            verify=verify, contracts=None, base_sha=self.base,
            branch="task/ko-7-thing", seconds=seconds))

    def events(self, kind):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?",
            (self.run_id, kind))]

    def run_state(self):
        return (self.conn.execute(
            "SELECT phase, workingMs, verifyMs FROM runs WHERE id = ?",
            (self.run_id,)).fetchone(), self.events("agent_turn"))


class ShadowTests(ShadowRun, unittest.TestCase):
    TICKET, CRITERIA = "# Create done.txt\n", ["done"]

    def test_a_verified_shadow_keeps_its_commit_on_the_shadow_branch(self):
        self.configure(turn=commits("ok\n"))
        self.shadow()
        self.assertEqual(self.git("rev-parse", "shadow/ko-7-thing~1"), self.base)
        self.assertEqual(self.git("show", "shadow/ko-7-thing:done.txt"), "ok")
        self.assertFalse(self.wt.exists())
        self.assertNotIn(str(self.wt.resolve()), self.git("worktree", "list"))
        [result] = self.events("shadow_result")
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["verify"], {"ok": True, "failed_command": None})
        self.assertEqual(result["commits"], 1)
        self.assertEqual(result["lines_changed"], 1)
        self.assertEqual(result["route"], "claude sonnet high")
        self.assertEqual(result["head_sha"], self.git("rev-parse", "shadow/ko-7-thing"))
        self.assertEqual(result["usage"]["output_tokens"], 5)

    def test_a_shadow_leaves_the_runs_phase_time_and_turns_alone(self):
        self.configure(turn=commits("ok\n"))
        before = self.run_state()
        self.shadow()
        self.assertEqual(self.run_state(), before)

    def test_a_shadow_route_down_cuts_nothing_and_switches_no_route(self):
        fallback = self.root / "fallback"
        fallback.write_text("#!/bin/sh\necho ready\n")
        fallback.chmod(0o755)
        self.configure(
            turn=commits("ok\n"),
            probe_reply="print(\"You've hit your limit\"); sys.exit(1)",
            extra=f'[agents]\nimplementer_fallback = "{fallback}"\n')
        self.shadow()
        self.assertEqual(self.git("branch", "--list", "shadow/*"), "")
        self.assertFalse(self.root.joinpath("repo.worktrees").exists())
        [result] = self.events("shadow_result")
        self.assertEqual(result["outcome"], "route_down")
        self.assertIn("implementer_shadow probe failed (exit 1)", result["detail"])
        self.assertIn("You've hit your limit", result["detail"])
        self.assertIsNone(result["verify"])
        routes = holophyte.agents.agent_routes
        state = routes.routes(self.target)
        self.assertEqual((state.commands, state.pending, state.failed), ({}, {}, False))
        self.assertEqual(routes.active_fallbacks(self.target), {})

    def test_a_shadow_commit_the_verify_rejects_is_verify_failed(self):
        self.configure(turn=commits("wrong\n"))
        self.shadow()
        [result] = self.events("shadow_result")
        self.assertEqual(result["outcome"], "verify_failed")
        self.assertEqual(result["verify"], {"ok": False, "failed_command": 1})

    def test_a_shadow_that_commits_nothing_is_no_commits(self):
        self.configure()
        self.shadow()
        [result] = self.events("shadow_result")
        self.assertEqual(result["outcome"], "no_commits")
        self.assertEqual(result["commits"], 0)

    def test_a_shadow_turn_past_its_cap_records_the_timeout_in_detail(self):
        self.configure(turn="print('partial work', flush=True)\n"
                            "import time; time.sleep(30)\n")
        self.shadow(seconds=1)
        [result] = self.events("shadow_result")
        self.assertEqual((result["outcome"], result["timed_out"]), ("timed_out", True))
        self.assertIn("timed out after 1s", result["detail"])
        self.assertIn("partial work", result["detail"])

    def test_an_existing_shadow_branch_is_left_at_its_tip(self):
        subprocess.run(["git", *IDENTITY, "commit", "--allow-empty", "-qm", "kept"],
                       cwd=self.repo, check=True)
        kept = self.git("rev-parse", "HEAD")
        self.git("branch", "shadow/ko-7-thing", kept)
        self.configure(turn=commits("ok\n"))
        self.shadow()
        self.assertEqual(self.git("rev-parse", "shadow/ko-7-thing"), kept)
        self.assertFalse(self.wt.exists())
        [result] = self.events("shadow_result")
        self.assertEqual(result["outcome"], "branch_exists")


class ShadowDocsTests(unittest.TestCase):
    def test_the_documented_shadow_table_parses_and_validates(self):
        document = (DOCS / "config.md").read_text()
        [example] = re.findall(
            r"```toml\n(\[agents\.implementer_shadow\]\n.*?)```", document, re.S)
        self.assertEqual(tomllib.loads(example)["agents"]["implementer_shadow"],
                         {"harness": "claude", "model": "sonnet", "effort": "high"})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = holophyte.config.project.Project(
                path=root, holo_dir=root, store_path=root / "store.db",
                config_path=root / "config.toml", worktrees=root / "worktrees")
            target.config_path.write_text(example)
            holophyte.config.checks.check_config(target)
            seat = holophyte.agents.harness.shadow_seat(target)
        self.assertEqual(seat.turn("goal")[4:], [
            "--model", "sonnet", "--effort", "high", "--output-format", "json",
            "goal"])
        self.assertIn("git branch --list 'shadow/*'", document)
        self.assertIn("git branch -D shadow/SLUG", document)


if __name__ == "__main__":
    unittest.main()

"""Execution-contract tests for Holophyte's named agent routes and the review
loop's control flow.

Run: python3 -m unittest discover -s tests -p 'test_factory_agents*' -v
"""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)


class AgentRouteTests(unittest.TestCase):
    def setUp(self):
        self.worktree = Path("/tmp/holophyte-agent-contract")

    @patch.object(factory.subprocess, "run")
    def test_implementer_uses_claude_opus_at_high_effort(self, run):
        run.return_value.stdout = "implemented"
        run.return_value.stderr = ""

        result = factory.agent("implement", "make the focused change", self.worktree)

        self.assertEqual(result, "implemented")
        run.assert_called_once_with(
            [
                "claude", "-p", "make the focused change",
                "--model", "opus", "--effort", "high",
            ],
            cwd=self.worktree, capture_output=True, text=True, timeout=1800,
        )

    @patch.object(factory.review_runner, "run_review")
    def test_reviewer_uses_containerized_codex_sol(self, run_review):
        run_review.return_value = "VERDICT: APPROVE"
        base = "1" * 40
        candidate = "2" * 40

        result = factory.agent(
            "review",
            "review the candidate",
            self.worktree,
            base_sha=base,
            candidate_sha=candidate,
        )

        self.assertEqual(result, "VERDICT: APPROVE")
        run_review.assert_called_once_with(
            repo=self.worktree,
            base_sha=base,
            candidate_sha=candidate,
            prompt="review the candidate",
            profile="codex-sol-medium",
            timeout=1800,
            verdicts=factory.review_runner.REVIEW_VERDICTS,
        )

    @patch.object(factory.review_runner, "run_review")
    def test_adjudicator_shares_the_reviewer_route_without_verdict_enforcement(
        self, run_review
    ):
        # A malformed terminal reply has to come back as text so the loop can
        # record it and read it as FAIL, not raise at the review boundary.
        run_review.return_value = "no verdict here"

        result = factory.agent(
            "adjudicate",
            "adjudicate the candidate",
            self.worktree,
            base_sha="1" * 40,
            candidate_sha="2" * 40,
        )

        self.assertEqual(result, "no verdict here")
        self.assertEqual(run_review.call_args.kwargs["profile"], "codex-sol-medium")
        self.assertIsNone(run_review.call_args.kwargs["verdicts"])


class FakeLinear:
    """Stand-in for the provider module `run_task` imports at call time."""

    def __init__(self):
        self.completed = []
        self.comments = []

    def complete(self, task_id):
        self.completed.append(task_id)

    def comment(self, task_id, body):
        self.comments.append((task_id, body))


class ReviewLoopTests(unittest.TestCase):
    """End-to-end control flow of `run_task` over a real throwaway repo, with
    only the agent turns faked: the loop's own git, worktree, verify and merge
    steps run for real, so a preserved branch really is a preserved branch."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.target.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.target / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")

        self.worktrees = root / "repo.worktrees"
        self.branch = "task/add-a-thing"
        self.wt = self.worktrees / "add-a-thing"
        for name, value in (("TARGET", self.target), ("WORKTREES", self.worktrees)):
            patcher = patch.object(factory, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.linear = FakeLinear()
        patcher = patch.dict(sys.modules, {"linear_provider": self.linear})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.events = []
        real_verify = factory.run_verify

        def spy(*args, **kwargs):
            self.events.append("verify")
            return real_verify(*args, **kwargs)

        patcher = patch.object(factory, "run_verify", spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def run_task(self, *replies):
        """Drive one task, answering each review/adjudicate turn in order."""
        replies = list(replies)

        def fake_agent(role, goal, cwd, *, base_sha=None, candidate_sha=None):
            self.events.append(role)
            if role != "implement":
                return replies.pop(0)
            n = sum(1 for event in self.events if event == "implement")
            (Path(cwd) / f"change{n}.txt").write_text(f"work {n}\n")
            self.git("add", "-A", cwd=cwd)
            self.git("commit", "-q", "-m", f"work {n}", cwd=cwd)
            return f"committed work {n}"

        with patch.object(factory, "agent", fake_agent):
            return factory.run_task({
                "id": "KO-116", "title": "add a thing",
                "verify": "echo ok", "budget_min": 1, "contracts": [],
            })

    def findings(self):
        return (self.target / "FINDINGS.md").read_text()

    def test_round_two_findings_get_a_fix_round_then_adjudication(self):
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "Small and complete.\nVERDICT: PASS")

        self.assertTrue(merged)
        # Round 2's findings buy a third implementer turn, and the verify gate
        # runs over that fix commit before the adjudicator is dispatched.
        self.assertEqual(self.events, [
            "implement",
            "verify", "review", "implement",
            "verify", "review", "implement",
            "verify", "adjudicate",
            "verify",  # pre-merge
        ])

    def test_terminal_pass_merges_and_completes_the_ticket(self):
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "VERDICT: PASS")

        self.assertTrue(merged)
        self.assertIn(f"Merge {self.branch}", self.git("log", "--format=%s", "main"))
        self.assertNotIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.linear.completed, ["KO-116"])

    def test_terminal_fail_preserves_the_branch_and_stops(self):
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "Broken.\nVERDICT: FAIL")

        self.assertFalse(merged)
        self.assertNotIn("Merge ", self.git("log", "--format=%s", "main"))
        self.assertIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertTrue(self.wt.exists())
        # No round-3 fix: the last turn dispatched was the adjudicator.
        self.assertEqual(self.events[-1], "adjudicate")
        self.assertIn("Terminal adjudication", self.findings())
        self.assertIn("VERDICT: FAIL", self.findings())

    def test_malformed_terminal_verdict_is_a_preserved_fail(self):
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "1. tests are thin\n2. rename the helper")

        self.assertFalse(merged)
        self.assertNotIn("Merge ", self.git("log", "--format=%s", "main"))
        self.assertIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.events[-1], "adjudicate")
        self.assertIn("MALFORMED", self.findings())
        self.assertIn("2. rename the helper", self.findings())


if __name__ == "__main__":
    unittest.main()

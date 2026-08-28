"""Execution-contract tests for Holophyte's named agent routes.

Run: python3 -m unittest discover -s tests -p 'test_factory_agents*' -v
"""
import importlib.util
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
        )


if __name__ == "__main__":
    unittest.main()

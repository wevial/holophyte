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

    @patch.object(factory.subprocess, "run")
    def test_reviewer_uses_read_only_codex_sol(self, run):
        run.return_value.stdout = "VERDICT: APPROVE"
        run.return_value.stderr = ""

        result = factory.agent("review", "review the candidate", self.worktree)

        self.assertEqual(result, "VERDICT: APPROVE")
        run.assert_called_once_with(
            [
                "codex", "exec", "-C", str(self.worktree),
                "-m", "gpt-5.6-sol",
                "-c", 'model_reasoning_effort="medium"',
                "-s", "read-only", "--ephemeral", "review the candidate",
            ],
            cwd=self.worktree, capture_output=True, text=True, timeout=1800,
        )


if __name__ == "__main__":
    unittest.main()

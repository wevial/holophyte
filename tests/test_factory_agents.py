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
    def test_a_reviewer_runner_failure_is_an_infra_failure(self, run_review):
        # The container did not start, so no candidate was judged: the run
        # fails, and fails as the factory's own failure rather than one the
        # ticket is charged for.
        run_review.side_effect = factory.review_runner.ReviewBoundaryError(
            "Codex CLI is not installed")

        with self.assertRaises(factory.InfraFailure) as raised:
            factory.agent("review", "review the candidate", self.worktree,
                          base_sha="1" * 40, candidate_sha="2" * 40)

        self.assertIn("Codex CLI is not installed", str(raised.exception))

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
        self.states = []
        self.comments = []

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

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
        self.branch = "task/ko-116-add-a-thing"
        self.wt = self.worktrees / "ko-116-add-a-thing"
        for name, value in (("TARGET", self.target), ("WORKTREES", self.worktrees)):
            patcher = patch.object(factory, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.linear = FakeLinear()
        patcher = patch.dict(sys.modules, {"linear_provider": self.linear})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.events = []
        self.goals = []
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

    def run_task(self, *replies, budget_min=1, **task):
        """Drive one task, answering each review/adjudicate turn in order.

        Extra keyword arguments override fields of the task dict, so a test
        can hand the loop a ticket whose body carries its own contract.
        """
        replies = list(replies)

        def fake_agent(role, goal, cwd, *, base_sha=None, candidate_sha=None):
            self.events.append(role)
            self.goals.append((role, goal))
            if role != "implement":
                return replies.pop(0)
            n = sum(1 for event in self.events if event == "implement")
            (Path(cwd) / f"change{n}.txt").write_text(f"work {n}\n")
            self.git("add", "-A", cwd=cwd)
            self.git("commit", "-q", "-m", f"work {n}", cwd=cwd)
            return f"committed work {n}"

        with patch.object(factory, "agent", fake_agent):
            try:
                return factory.run_task({
                    "id": "KO-116", "title": "add a thing",
                    "verify": "echo ok", "budget_min": budget_min,
                    "contracts": [], **task,
                })
            except factory.RunFailure:
                # run_task's failure exits raise so their reasons reach the
                # close-out; a direct call answers False the way main() does.
                return False

    def records(self):
        """The run's records as the ticket archive holds them.

        Linear, not FINDINGS.md: that file is rendered from the store's rows
        at close-out (`tests/test_wiring_findings.py`), and these tests drive
        `run_task()` without a store.
        """
        return "\n".join(body for _, body in self.linear.comments)

    def test_implementer_goal_carries_the_ticket_body_and_verify_commands(self):
        """The implementer works from the approved body, not from the title.

        The phrase asserted on lives only in the body, so a goal built from
        the title alone cannot contain it — the reviewer holds the candidate
        to criteria the implementer would never have seen.
        """
        body = ("## Acceptance criteria\n\n"
                "- [ ] The word used is `unparseable`, never `unreadable`.\n")

        merged = self.run_task("VERDICT: APPROVE", body=body)

        self.assertTrue(merged)
        goal = next(g for role, g in self.goals if role == "implement")
        self.assertIn("The word used is `unparseable`, never `unreadable`.",
                      goal)
        self.assertIn("add a thing", goal)
        self.assertIn("echo ok", goal)

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

    def test_terminal_pass_merges_and_leaves_the_linear_state_alone(self):
        """The merge is `run_task()`'s; the ticket's Linear state is not.

        Ticket status lives in the store and is projected onto Linear by
        `main()` through `mirror_push()`, so a run that merges makes no state
        call of its own — a direct one here would be a second writer of the
        same fact, from a frame with no store to be right about."""
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "VERDICT: PASS")

        self.assertTrue(merged)
        self.assertIn(f"Merge {self.branch}", self.git("log", "--format=%s", "main"))
        self.assertNotIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertFalse(self.wt.exists())
        self.assertEqual(self.linear.states, [])

    def test_close_out_records_actual_duration_estimate_and_rounds(self):
        # Claim at t=100 s, close-out 42.7 s later: 0.711 min, reported to one
        # decimal, against a 20 min estimate and a single review round.
        with patch.object(factory, "monotonic", side_effect=[100.0, 142.7]):
            merged = self.run_task("VERDICT: APPROVE", budget_min=20)

        self.assertTrue(merged)
        timing = "actual: 0.7 min · estimate: 20 min · rounds: 1"
        self.assertIn(timing, self.records())

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
        self.assertIn("Terminal adjudication", self.records())
        self.assertIn("VERDICT: FAIL", self.records())

    def test_malformed_terminal_verdict_is_a_preserved_fail(self):
        merged = self.run_task("VERDICT: REQUEST_CHANGES",
                               "VERDICT: REQUEST_CHANGES",
                               "1. tests are thin\n2. rename the helper")

        self.assertFalse(merged)
        self.assertNotIn("Merge ", self.git("log", "--format=%s", "main"))
        self.assertIn(self.branch, self.git("branch", "--list", self.branch))
        self.assertTrue(self.wt.exists())
        self.assertEqual(self.events[-1], "adjudicate")
        self.assertIn("MALFORMED", self.records())
        self.assertIn("2. rename the helper", self.records())


class RowWriteSanitizationTests(unittest.TestCase):
    """Agent text is sanitized where a row is written, not where a file is
    appended: FINDINGS.md is rendered from those rows now, so an escape
    sequence or a heading that reached one would come back on every render.
    Asserted over the message `raw_finding()` stores rather than over the
    helper, since the helper is only useful if the write sites go through it —
    `parse_findings()` builds its messages through the same one."""

    def stored(self, entry):
        return factory.raw_finding(entry)["message"]

    def test_ansi_escapes_and_control_bytes_are_stripped(self):
        # A coloured tool trace of the shape that reached the KO-107 entry.
        written = self.stored("\x1b[0m\x1b[32m$ \x1b[0mgit status\x07\r\n"
                              "\x1bOn branch main\n"
                              "VERDICT: APPROVE")

        self.assertNotIn("\x1b", written)
        self.assertNotIn("\x07", written)
        self.assertNotIn("\r", written)
        self.assertIn("$ git status\n", written)
        self.assertIn("On branch main\n", written)
        self.assertIn("VERDICT: APPROVE", written)

    def test_embedded_headings_are_demoted_out_of_the_files_outline(self):
        written = self.stored("## Blockers\n\n1. the migration is missing\n\n"
                              "### Detail\n\nVERDICT: REQUEST_CHANGES")

        # A stored message contributes nothing to the rendered file's outline.
        self.assertEqual([ln for ln in written.splitlines()
                          if ln.startswith("#")], [])
        self.assertIn("**Blockers**", written)
        self.assertIn("**Detail**", written)

    def test_an_oversize_block_is_truncated_with_a_visible_marker(self):
        entry = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        self.assertGreater(len(entry), 10_000)

        written = self.stored(entry)

        self.assertIn("[… truncated]", written)
        self.assertIn("line 0 ", written)
        self.assertNotIn("line 199 ", written)
        self.assertLessEqual(len(written), factory.MAX_FINDING_CHARS)

    def test_c1_escape_sequences_are_stripped_with_their_payload(self):
        # A CSI introduced by the single C1 byte, not by ESC-[: dropping only
        # the introducer would leave `31m` printing as literal text.
        written = self.stored("\x9b31mred\x9b0m\x85 tail\nVERDICT: APPROVE")

        self.assertNotIn("\x9b", written)
        self.assertNotIn("31m", written)
        self.assertNotIn("\x85", written)
        self.assertIn("red tail\n", written)

    def test_indented_and_setext_headings_are_demoted_too(self):
        written = self.stored("   ## Indented blocker\n\n"
                              "Setext blocker\n==============\n\n"
                              "Second one\n---\n\nVERDICT: REQUEST_CHANGES")

        outline = [ln for ln in written.splitlines()
                   if ln.lstrip().startswith("#") or set(ln.strip()) in ({"="}, {"-"})]
        self.assertEqual(outline, [])
        self.assertIn("**Indented blocker**", written)
        self.assertIn("**Setext blocker**", written)
        self.assertIn("**Second one**", written)

    def test_truncation_keeps_the_trailing_verdict_line(self):
        # The verdict is the outcome the entry is evidence for, and it sits at
        # the end — exactly where a head-only truncation would drop it.
        entry = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        entry += "\nVERDICT: REQUEST_CHANGES"

        written = self.stored(entry)

        self.assertIn("[… truncated]", written)
        self.assertNotIn("line 199 ", written)
        self.assertEqual(written.splitlines()[-1], "VERDICT: REQUEST_CHANGES")

    def test_truncation_stays_within_budget_when_it_keeps_a_verdict(self):
        entry = "x" * 10_000 + "\nVERDICT: APPROVE"

        body = self.stored(entry)

        self.assertLessEqual(len(body), factory.MAX_FINDING_CHARS)

    def test_an_oversize_verdict_line_cannot_escape_the_budget(self):
        # A malformed adjudicator reply is persisted verbatim, so the trailing
        # line the truncation branch must keep is agent-written and unbounded.
        entry = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
        entry += "\nVERDICT: " + "y" * 10_000

        body = self.stored(entry)

        self.assertLessEqual(len(body), factory.MAX_FINDING_CHARS)
        self.assertIn("[… truncated]", body)
        self.assertNotIn("line 199 ", body)
        # The verdict is still recorded, cut rather than dropped.
        self.assertTrue(body.splitlines()[-1].startswith("VERDICT: yyy"))

    def test_clean_text_is_written_through_unchanged(self):
        entry = ("Round 1: REQUEST_CHANGES -> fix round\n"
                 "- `store.py:99`: no migration, so init() leaves #42 broken\n"
                 "\nVERDICT: REQUEST_CHANGES")

        self.assertEqual(self.stored(entry), entry)


if __name__ == "__main__":
    unittest.main()

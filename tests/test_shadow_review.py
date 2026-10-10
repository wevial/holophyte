"""The shadow's one blind review: the primary's reviewer route judges the
shadow's candidate with round 1's prompt, and its verdict joins `shadow_result`.

Run: python3 -m unittest discover -s tests -p 'test_shadow_review.py' -v
"""
import unittest
from unittest.mock import patch

import review_runner
from holophyte.loop import review_round
from tests.test_shadow import ShadowRun, commits

TICKET = """# Write the done marker

## Acceptance criteria

- [ ] done.txt holds the word ok
- [ ] done.txt is a single line
"""
CRITERIA = ["done.txt holds the word ok", "done.txt is a single line"]
FINDINGS = """- [P1] done.txt:1 the marker is written without a trailing check
- [P1] done.txt:1 the marker is not tested
- [P2] done.txt:1 the marker's name is unclear

VERDICT: REQUEST_CHANGES
"""


class ShadowReviewTests(ShadowRun, unittest.TestCase):
    TICKET, CRITERIA, REVIEW = TICKET, CRITERIA, FINDINGS

    def rows(self, table):
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE runId = ?", (self.run_id,)).fetchall()

    def review_of(self):
        [result] = self.events("shadow_result")
        return result

    def test_a_request_changes_reply_joins_the_result_and_not_the_runs_rounds(self):
        self.configure(turn=commits("ok\n"))
        before = self.rows("reviewRounds"), self.rows("ledger")
        self.shadow()
        result = self.review_of()
        review = result["review"]
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(review["verdict"], "REQUEST_CHANGES")
        self.assertEqual(review["findings"], {"p1": 2, "p2": 1})
        self.assertEqual(review["unwitnessed"], 2)
        self.assertEqual(review["reviewer"], "codex-astra-high")
        self.assertEqual((self.rows("reviewRounds"), self.rows("ledger")), before)

    def test_the_prompt_is_round_ones_and_names_no_implementer(self):
        self.configure(turn=commits("ok\n"))
        self.shadow()
        [call] = self.review.call_args_list
        prompt = call.kwargs["prompt"]
        self.assertIn("Write the done marker", prompt)
        for criterion in CRITERIA:
            self.assertIn(criterion, prompt)
        self.assertIn("VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES", prompt)
        self.assertNotIn("sonnet", prompt)
        self.assertNotIn("shadow", prompt.lower())
        self.assertEqual(prompt, self.primary_prompt(call.kwargs["candidate_sha"]))

    def test_verify_output_shows_the_reviewer_the_primarys_path_and_branch(self):
        self.configure(turn=commits("ok\n"))
        self.shadow(verify="pwd && git branch --show-current && grep -qx ok done.txt")
        prompt = self.review.call_args.kwargs["prompt"]
        self.assertIn(str(self.root / "repo.worktrees" / "ko-7-thing"), prompt)
        self.assertIn("task/ko-7-thing", prompt)
        self.assertNotIn("shadow", prompt.lower())

    def primary_prompt(self, sha):
        class Captured(Exception):
            pass

        prompts = []

        def capture(target, role, goal, *args, **kwargs):
            prompts.append(goal)
            raise Captured

        self.git("checkout", "-q", sha)
        with (patch.object(review_round, "agent", side_effect=capture),
              patch.object(review_round, "set_phase"),
              self.assertRaises(Captured)):
            review_round._review_rounds(
                project=self.target, conn=None, run_id=self.run_id,
                provider=None, task_id="KO-7", branch="task/ko-7-thing",
                wt=self.repo, beat_s=1, base_sha=self.base, sha=sha,
                ticket=TICKET, verify_cmd="grep -qx ok done.txt",
                contracts=None, criteria=CRITERIA, budget_min=10, cap=1)
        return prompts[0]

    def test_a_ui_candidate_on_a_pr_project_is_judged_without_evidence(self):
        self.configure(turn=commits("ok\n"),
                       extra='[merge]\nmode = "pr"\nui_capture = "true"\n'
                       'ui_paths = ["done.txt"]\n')
        self.shadow()
        review = self.review_of()["review"]
        self.assertEqual((review["verdict"], review["error"]),
                         ("REQUEST_CHANGES", None))
        self.assertNotIn("Evidence", self.review.call_args.kwargs["prompt"])

    def test_adversary_on_still_runs_exactly_one_plain_review(self):
        self.configure(turn=commits("ok\n"), extra="[review]\nadversary = true\n")
        self.shadow()
        [call] = self.review.call_args_list
        self.assertNotIn("multi_agent", call.kwargs)
        self.assertEqual(self.events("adversary_round"), [])
        self.assertEqual([turn for turn in self.events("agent_turn")
                          if turn.get("role") == "adversary"], [])
        self.assertEqual(self.review_of()["review"]["verdict"], "REQUEST_CHANGES")

    def test_a_shadow_without_commits_is_not_reviewed(self):
        self.configure()
        self.shadow()
        self.review.assert_not_called()
        result = self.review_of()
        self.assertEqual(result["outcome"], "no_commits")
        self.assertIsNone(result["review"])

    def test_a_failed_review_records_error_and_keeps_the_outcome(self):
        self.review.side_effect = review_runner.ReviewBoundaryError("route down")
        self.configure(turn=commits("wrong\n"))
        self.shadow()
        result = self.review_of()
        self.assertEqual(result["outcome"], "verify_failed")
        self.assertEqual(result["review"]["verdict"], "error")
        self.assertIn("route down", result["review"]["error"])
        self.assertFalse(self.wt.exists())

    def test_a_reply_without_a_verdict_line_is_malformed(self):
        self.review.return_value = "Looks fine to me."
        self.configure(turn=commits("ok\n"))
        self.shadow()
        result = self.review_of()
        self.assertEqual(result["outcome"], "verified")
        self.assertEqual(result["review"]["verdict"], "MALFORMED")
        self.assertIn("VERDICT: APPROVE", result["review"]["error"])

    def test_a_configured_reviewer_command_skips_the_review(self):
        self.configure(turn=commits("ok\n"), extra='[agents]\nreviewer = "true"\n')
        self.shadow()
        self.review.assert_not_called()
        self.assertEqual(self.review_of()["review"],
                         {"verdict": "skipped", "reason": "configured reviewer"})


if __name__ == "__main__":
    unittest.main()

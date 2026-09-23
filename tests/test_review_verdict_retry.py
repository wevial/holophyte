"""A malformed review gets one reminder, never an implementer fix turn."""
from tests.loop_fixture import BRANCH, Commit, Idle, LoopFixture  # isort: skip

import json
import sqlite3
from unittest.mock import patch

from fake_agent import APPROVE, REQUEST_CHANGES, Reply  # noqa: E402

import holophyte.loop
import holophyte.runs
import holophyte.supervisor
import review_runner
import store


class ReviewVerdictRetryTests(LoopFixture):
    def test_missing_verdict_then_approval_reaches_merge_gate(self):
        first = Reply("CRITERION 1: not met — stale first assessment\n"
                      + "review detail " * 1100)
        fake, _ = self.loop(Commit(), first, APPROVE)
        reviews = [t for t in fake.turns if t.role == "review"]
        self.assertEqual(len(reviews), 2)
        self.assertTrue(reviews[1].goal.startswith(reviews[0].goal))
        self.assertIn("nothing after it", reviews[1].goal)
        ((verdict, findings),) = self.read(
            "SELECT verdict, findings FROM reviewRounds")
        self.assertEqual(verdict, "pass")
        self.assertIn("first reply (no verdict):", findings)
        self.assertIn("characters cut", findings)
        self.assertIn("reviewing -> merge_gate", self.transitions())

    def test_two_missing_verdicts_fail_as_infra_without_fix(self):
        first = Reply("CRITERION 1: met — tests/test_thing.py::test_it_works\n"
                      "The change looks complete.")
        output = self.main_output(Commit(), first, Reply("Still no verdict."))
        fake = self.last_fake
        self.assertEqual([t.role for t in fake.turns],
                         ["implement", "review", "review"])
        self.assertNotIn("reviewing -> addressing", self.transitions())
        ((verdict, findings),) = self.read(
            "SELECT verdict, findings FROM reviewRounds")
        self.assertEqual(verdict, "error")
        self.assertIn("first reply (no verdict):", findings)
        self.assertIn("Still no verdict.", findings)
        sha = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read(
            "SELECT outcome, outcomeClass, outcomeReason FROM runs"),
            [("failed", "infra", "reviewer returned no verdict line twice; "
              f"candidate preserved at {sha}")])
        self.assertIn("[holo2] round 1: reviewer returned no verdict line twice",
                      output)
        self.assertNotIn("Traceback", output)

    def test_request_changes_calls_reviewer_once_and_addresses(self):
        fake, _ = self.loop(Commit(), REQUEST_CHANGES, Idle())
        self.assertEqual([t.role for t in fake.turns],
                         ["implement", "review", "implement"])
        self.assertIn("reviewing -> addressing", self.transitions())

    def test_default_route_retries_final_messages(self):
        for second, decision in (("VERDICT: APPROVE", "APPROVE"),
                                 ("Still no verdict.", "MALFORMED")):
            with self.subTest(second=second):
                replies = iter(("The change looks complete.", second))

                def runner(**kwargs):
                    events = [
                        {"type": "item.completed", "item": {
                            "type": "command_execution", "exit_code": 0}},
                        {"type": "item.completed", "item": {
                            "type": "agent_message", "text": next(replies)}}]
                    return review_runner.parse_codex_output(
                        "\n".join(json.dumps(e) for e in events),
                        kwargs["verdicts"])[0]

                with patch.object(review_runner, "run_review",
                                  side_effect=runner) as run:
                    reply, actual, evidence = holophyte.loop._review_reply(
                        self.project, "Review this candidate.", self.target,
                        self.base, self.base, None, None)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(actual, decision)
                self.assertEqual(reply, second)
                self.assertIn("first reply (no verdict):", evidence)

    def test_retry_evidence_does_not_change_convergence(self):
        self.loop(Commit(), Reply("Boilerplate without a verdict"), APPROVE)
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        run_id = conn.execute("SELECT id FROM runs").fetchone()[0]
        self.assertEqual(conn.execute(
            "SELECT findingsFingerprint FROM reviewRounds").fetchone()[0],
            store.EMPTY_FINGERPRINT)
        for rnd, path, prior in ((2, "src/a.py", "same boilerplate"),
                                 (3, "src/b.py", "same boilerplate"),
                                 (4, "src/b.py", "different boilerplate")):
            holophyte.runs.record_round(
                self.project, conn, run_id, rnd, "review",
                f"- [P1] {path}:12 — Broken boundary\nVERDICT: REQUEST_CHANGES",
                "echo ok", True, "ok", prior_reply=prior)
            if rnd == 2:
                self.assertIsNone(holophyte.supervisor.review_overlap(conn, run_id))
            else:
                self.assertEqual(holophyte.supervisor.review_overlap(conn, run_id),
                                 (rnd - 1, rnd, 0.0 if rnd == 3 else 1.0))
        fingerprints = conn.execute(
            "SELECT findingsFingerprint FROM reviewRounds ORDER BY round").fetchall()
        self.assertNotEqual(fingerprints[1], fingerprints[2])
        self.assertEqual(fingerprints[2], fingerprints[3])

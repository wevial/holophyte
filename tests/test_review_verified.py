"""`[agents] review_mode`: the verified brief, and the refuted section kept
out of the stored findings, the fingerprint and the fix turn."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, PASS, Commit, Reply  # noqa: E402
from loop_fixture import LoopFixture  # noqa: E402

from holophyte.config.checks import check_config  # noqa: E402
from holophyte.review.reply_parsing import parse_findings  # noqa: E402

WITNESS = "CRITERION 1: met — tests/test_thing.py::test_it_works\n"
CONFIRMED = "- src/app.py:3 load() returns None and the caller indexes it.\n"
REFUTED = ("REFUTED FINDINGS (non-blocking)\n"
           "- src/other.py:9 the handle leaks on error; refuted, a finally "
           "block closes it.\n"
           "END REFUTED FINDINGS\n")
WITH_REFUTED = (f"Reviewed the diff.\n\n{CONFIRMED}\n{REFUTED}{WITNESS}"
                "VERDICT: REQUEST_CHANGES")
WITHOUT_REFUTED = (f"Reviewed the diff.\n\n{CONFIRMED}\n{WITNESS}"
                   "VERDICT: REQUEST_CHANGES")


class VerifiedReplyParsingTests(unittest.TestCase):
    def test_a_marked_refuted_item_is_not_a_finding(self):
        findings = parse_findings(WITH_REFUTED)
        self.assertEqual([(f["path"], f.get("line")) for f in findings],
                         [("src/app.py", 3)])

    def test_an_unclosed_refuted_section_still_blocks(self):
        reply = (f"Reviewed the diff.\n\nREFUTED FINDINGS (non-blocking)\n"
                 "- src/other.py:9 the handle leaks on error.\n"
                 f"{WITNESS}VERDICT: REQUEST_CHANGES")
        findings = parse_findings(reply)
        self.assertEqual([(f["path"], f.get("line")) for f in findings],
                         [("src/other.py", 9)])


class VerifiedReviewLoopTests(LoopFixture):
    def review_goal(self, config=None):
        if config is not None:
            self.configure(config)
        fake, _ = self.loop(Commit(), APPROVE)
        return next(turn.goal for turn in fake.turns if turn.role == "review")

    def two_rounds(self, first, second, config=None):
        if config is not None:
            self.configure(config)
        fake, _ = self.loop(Commit(), Reply(first), Commit(), Reply(second),
                            Commit(), PASS)
        return fake

    def mode_events(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'review_mode' "
            "ORDER BY seq")]

    def stored_rounds(self):
        return self.read("SELECT round, findings, findingsFingerprint "
                         "FROM reviewRounds ORDER BY round")

    def test_verified_mode_prompt_asks_for_angles_verifier_and_markers(self):
        goal = self.review_goal('[agents]\nreview_mode = "verified"\n')
        for phrase in ("correctness against the ticket",
                       "the tests and how well they witness",
                       "scope and regressions",
                       "independent verifier check each finding",
                       "\nREFUTED FINDINGS (non-blocking)\n",
                       "\nEND REFUTED FINDINGS\n"):
            self.assertIn(phrase, goal)

    def test_absent_review_mode_prompt_has_no_verified_instructions(self):
        goal = self.review_goal()
        for phrase in ("REFUTED FINDINGS (non-blocking)",
                       "END REFUTED FINDINGS", "independent verifier"):
            self.assertNotIn(phrase, goal)

    def test_single_review_mode_prompt_has_no_verified_instructions(self):
        goal = self.review_goal('[agents]\nreview_mode = "single"\n')
        for phrase in ("REFUTED FINDINGS (non-blocking)",
                       "END REFUTED FINDINGS", "independent verifier"):
            self.assertNotIn(phrase, goal)

    def test_refuted_section_leaves_the_stored_fingerprint_unchanged(self):
        self.two_rounds(WITH_REFUTED, WITHOUT_REFUTED,
                        '[agents]\nreview_mode = "verified"\n')
        (_, first, first_print), (_, second, second_print) = \
            self.stored_rounds()[:2]
        self.assertEqual([(f["path"], f.get("line"))
                          for f in json.loads(first)], [("src/app.py", 3)])
        self.assertEqual(first_print, second_print)

    def test_refuted_only_items_are_in_no_stored_finding(self):
        self.configure('[agents]\nreview_mode = "verified"\n')
        self.loop(Commit(), Reply(f"Reviewed the diff.\n\n{REFUTED}{WITNESS}"
                                  "VERDICT: REQUEST_CHANGES"),
                  Commit(), APPROVE)
        (_, findings, _), *_ = self.stored_rounds()
        messages = [f["message"] for f in json.loads(findings)]
        self.assertTrue(messages)
        for message in messages:
            self.assertNotIn("the handle leaks on error", message)

    def test_fix_turn_gets_the_confirmed_finding_and_not_the_refuted(self):
        fake = self.two_rounds(WITH_REFUTED, WITHOUT_REFUTED,
                               '[agents]\nreview_mode = "verified"\n')
        fix_goal = [turn.goal for turn in fake.turns
                    if turn.role == "implement"][1]
        self.assertIn("load() returns None and the caller indexes it", fix_goal)
        self.assertNotIn("the handle leaks on error", fix_goal)

    def test_each_round_records_the_verified_mode(self):
        self.two_rounds(WITH_REFUTED, WITHOUT_REFUTED,
                        '[agents]\nreview_mode = "verified"\n')
        self.assertEqual(self.mode_events(), [{"mode": "verified", "round": 1},
                                              {"mode": "verified", "round": 2}])

    def test_each_round_records_the_single_mode_by_default(self):
        self.two_rounds(WITHOUT_REFUTED, WITHOUT_REFUTED)
        self.assertEqual(self.mode_events(), [{"mode": "single", "round": 1},
                                              {"mode": "single", "round": 2}])

    def test_startup_refuses_an_unknown_review_mode(self):
        for value in ('"double"', "true"):
            with self.subTest(value=value):
                self.configure(f"[agents]\nreview_mode = {value}\n")
                with self.assertRaisesRegex(SystemExit,
                                            r"\[agents\] review_mode"):
                    check_config(self.project)
        for value in ('"verified"', '"single"'):
            with self.subTest(value=value):
                self.configure(f"[agents]\nreview_mode = {value}\n")
                check_config(self.project)


if __name__ == "__main__":
    unittest.main()

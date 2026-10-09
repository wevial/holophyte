"""One findings list for the fix turn: exact merge, consolidator pass, concern cap."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402
    IMPLEMENT,
    REQUEST_CHANGES,
    Commit,
    Consolidate,
    Reply,
)
from loop_fixture import LoopFixture  # noqa: E402
from test_adversary import Change, attack, finding  # noqa: E402

from holophyte.review import adversary, consolidate  # noqa: E402
from holophyte.review.reply_parsing import parse_findings  # noqa: E402

ON = "[review]\nadversary = true\n"
HEADING = "Adversarial review findings (reproduced or traced):"
MET = "CRITERION 1: met — tests/test_thing.py::test_it_works\n"
APPROVE = Reply(f"Reviewed the diff; no blockers.\n{MET}VERDICT: APPROVE")


def asked(*findings):
    return Reply("".join(findings) + f"\n{MET}VERDICT: REQUEST_CHANGES")


class PassOneTests(unittest.TestCase):
    def test_one_finding_from_both_reviewers_keeps_the_stronger_claim(self):
        primary = parse_findings("- src/app.py:3 [p2] load() returns None\n\n"
                                 "VERDICT: REQUEST_CHANGES")
        found = adversary.parse("- src/app.py:3 [p1] load() returns None\n"
                                "EVIDENCE: reproduced\nADVERSARY: DONE")
        [item] = consolidate.exact(consolidate.items(primary, found))
        self.assertEqual(
            (item["path"], item["line"], item["severity"], item["evidence"],
             item["found_by"], item["messages"]),
            ("src/app.py", 3, "p1", "reproduced", ["primary", "adversary"],
             ["load() returns None"]))

    def test_different_findings_at_one_location_stay_side_by_side(self):
        primary = parse_findings("- src/app.py:3 [p2] load() returns None\n"
                                 "- src/cli.py:1 [p2] the flag is ignored\n")
        found = adversary.parse("- src/app.py:3 [p2] save() drops the file\n"
                                "EVIDENCE: traced\nADVERSARY: DONE")
        merged = consolidate.exact(consolidate.items(primary, found))
        self.assertEqual([(item["path"], item["messages"][0]) for item in merged],
                         [("src/app.py", "load() returns None"),
                          ("src/app.py", "save() drops the file"),
                          ("src/cli.py", "the flag is ignored")])


def pass_one():
    return consolidate.items(
        [{"path": "src/app.py", "line": 3, "severity": "p2",
          "message": "- src/app.py:3 [p2] load() returns None"}],
        [{"path": "src/app.py", "line": 9, "severity": "p1",
          "message": "- src/app.py:9 [p1] the caller indexes a None",
          "evidence": "traced"},
         {"path": "src/cli.py", "line": 1, "severity": "p2",
          "message": "- src/cli.py:1 [p2] an empty argv crashes",
          "evidence": "reproduced"}])


class PassTwoTests(unittest.TestCase):
    def test_a_merge_and_an_order_are_applied_by_the_factory(self):
        final, record = consolidate.apply(
            pass_one(), "MERGE: F2 INTO F1 — same null return\n"
            "ORDER: F3, F1\nCONSOLIDATED")
        self.assertEqual([item["path"] for item in final],
                         ["src/cli.py", "src/app.py"])
        merged = final[1]
        self.assertEqual(
            (merged["line"], merged["severity"], merged["evidence"],
             merged["found_by"], merged["messages"]),
            (3, "p1", "traced", ["primary", "adversary"],
             ["load() returns None", "the caller indexes a None"]))
        self.assertEqual(record["merges"], [
            {"from": "F2", "into": "F1", "reason": "same null return"}])
        self.assertEqual(record["pass2"], "merged")

    def test_unknown_and_self_merges_are_set_aside_and_nothing_weakens(self):
        before = pass_one()
        final, record = consolidate.apply(
            before, "MERGE: F9 INTO F1 — x\nMERGE: F1 INTO F1 — y\n"
            "ORDER: F3\nCONSOLIDATED")
        self.assertEqual([item["messages"] for item in final],
                         [before[2]["messages"], before[0]["messages"],
                          before[1]["messages"]])
        self.assertEqual([(i["severity"], i["evidence"]) for i in final],
                         [("p2", "reproduced"), ("p2", "review"),
                          ("p1", "traced")])
        self.assertEqual(record["ignored"],
                         ["MERGE: F9 INTO F1 — x", "MERGE: F1 INTO F1 — y"])
        self.assertEqual((record["pass2"], record["merges"]), ("unchanged", []))


class ConsolidationFixture(LoopFixture):
    def events(self, kind="consolidation"):
        return [json.loads(payload) for (payload,) in self.read(
            f"SELECT payload FROM runEvents WHERE kind = '{kind}' ORDER BY seq")]

    def fix_goal(self, fake):
        return [turn for turn in fake.turns if turn.role == IMPLEMENT][1].goal

    def notes(self, start):
        return [text for (text,) in self.read(
            "SELECT text FROM ledger WHERE kind = 'note'") if text.startswith(start)]


class RoundTests(ConsolidationFixture):
    def test_one_item_left_by_pass_one_runs_no_consolidator(self):
        self.configure(ON)
        fake, _ = self.loop(
            Change("poetry.lock"),
            asked("- src/app.py:3 [p2] load() returns None\n"),
            attack(finding("src/app.py", 3, "load() returns None", "traced")),
            Change("src/app.py"), APPROVE)
        self.assertNotIn("consolidate", fake.roles)
        [event] = self.events()
        self.assertEqual((event["pass1_in"], event["pass1_out"], event["pass2"]),
                         (2, 1, "skipped"))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def a_failed_consolidator(self, step, outcome):
        self.configure(ON)
        fake, _ = self.loop(
            Change("poetry.lock"),
            asked("- src/app.py:3 [p2] load() returns None\n"),
            attack(finding("src/db.py", 7, "the lock is skipped", "traced")),
            step, Change("src/app.py"), APPROVE)
        goal = self.fix_goal(fake)
        self.assertLess(goal.index("1. src/db.py:7 [p1]"),
                        goal.index("2. src/app.py:3 [p2]"))
        [event] = self.events()
        self.assertEqual((event["pass2"], event["merges"]), (outcome, []))
        self.assertEqual(
            self.read("SELECT round, verdict FROM reviewRounds ORDER BY round"),
            [(1, "changes_requested"), (2, "pass")])

    def test_a_consolidator_route_failure_hands_on_the_exact_merge(self):
        self.a_failed_consolidator(Consolidate(fails=True), "unavailable")

    def test_a_reply_without_the_closing_line_is_not_applied(self):
        self.a_failed_consolidator(
            Consolidate("MERGE: F2 INTO F1 — same\nORDER: F2, F1"), "malformed")

    def test_the_fix_turn_gets_one_numbered_list_after_an_adversary_pass(self):
        self.configure(ON)
        fake, _ = self.loop(
            Change("poetry.lock"),
            asked("- src/app.py:3 [p2] load() returns None\n"),
            attack(finding("src/db.py", 7, "the lock is skipped", "traced")),
            Consolidate(), Change("src/app.py"), APPROVE)
        goal = self.fix_goal(fake)
        self.assertIn("1. src/db.py:7 [p1] evidence traced, found by adversary\n"
                      "    the lock is skipped\n"
                      "2. src/app.py:3 [p2] evidence review, found by primary\n"
                      "    load() returns None", goal)
        self.assertNotIn(HEADING, goal)
        self.assertEqual(self.events()[0]["pass2"], "unchanged")

    def test_without_an_adversary_the_fix_turn_gets_the_primary_verdict(self):
        fake, _ = self.loop(Commit("work"), REQUEST_CHANGES, Commit("fix"),
                            APPROVE)
        goal = self.fix_goal(fake)
        self.assertIn("Blocker: the scripted change is incomplete.\n", goal)
        self.assertIn("VERDICT: REQUEST_CHANGES", goal)
        self.assertNotIn("merged into one list", goal)
        self.assertEqual(self.events(), [])


class ConcernTests(ConsolidationFixture):
    def test_three_concerns_go_to_the_fix_turn_and_the_rest_are_held(self):
        self.configure(ON)
        concerns = [finding(f"src/c{n}.py", n, f"concern number {n}", "concern")
                    for n in range(1, 6)]
        fake, _ = self.loop(
            Change("poetry.lock"),
            asked("- src/app.py:3 [p1] load() returns None\n"),
            attack(*concerns), Consolidate("ORDER: F6, F5, F1\nCONSOLIDATED"),
            Change("src/app.py"), APPROVE)
        goal = self.fix_goal(fake)
        for sent in ("concern number 5", "concern number 4",
                     "load() returns None", "concern number 1"):
            self.assertIn(sent, goal)
        for held in ("concern number 2", "concern number 3"):
            self.assertNotIn(held, goal)
        self.assertIn("never blocks", goal)
        [event] = self.events()
        self.assertEqual([c["path"] for c in event["sent_concerns"]],
                         ["src/c5.py", "src/c4.py", "src/c1.py"])
        self.assertEqual([c["path"] for c in event["held_concerns"]],
                         ["src/c2.py", "src/c3.py"])
        [note] = self.notes("Round 1: 2 concerns held")
        self.assertIn("src/c2.py:2 [p1] concern number 2 (round 1)", note)
        self.assertIn("src/c3.py:3 [p1] concern number 3 (round 1)", note)

    def test_a_concern_on_a_high_path_is_raised_once_per_run(self):
        self.configure(ON)
        lock = finding("poetry.lock", 1, "the lock may pin a yanked release",
                       "concern")
        app = finding("src/app.py", 4, "a symlink may slip by", "concern")
        self.loop(Change("poetry.lock"),
                  asked("- src/app.py:3 [p1] load() returns None\n"),
                  attack(lock, app), Consolidate(),
                  Change("poetry.lock", "relocked\n"), APPROVE, attack(lock))
        self.assertEqual([e["round"] for e in self.events()], [1, 2])
        [raised] = self.events("concern_raised")
        self.assertEqual((raised["path"], raised["round"]), ("poetry.lock", 1))
        [note] = self.notes(consolidate.RAISED)
        self.assertIn("poetry.lock", note)
        self.assertEqual(
            [n for n in self.notes(consolidate.RAISED) if "src/app.py" in n], [])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


if __name__ == "__main__":
    unittest.main()

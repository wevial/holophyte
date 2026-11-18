"""A DROP ruling on a repeated finding keeps it out of the consolidated fix list."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import IMPLEMENT, PASS, Consolidate, Reply  # noqa: E402
from test_adversary import Change, attack, finding  # noqa: E402
from test_consolidate import (  # noqa: E402
    ON,
    ConsolidationFixture,
    asked,
)

KEPT = "- src/app.py:3 [p1] load() returns None\n"
DROPPED = "- src/db.py:7 [p1] the cache is never warmed\n"
FRESH = finding("src/cli.py", 5, "an empty argv crashes", "reproduced")
ADVERSARY_DROP = finding("src/db.py", 7, "the cache is never warmed", "traced")
RULED = Reply("THREAD 1: FIX -- load() still breaks the criterion\n"
              "THREAD 2: DROP -- warming the cache is out of scope")


class DroppedFindingTests(ConsolidationFixture):
    def run_repeated_review(self):
        self.configure(ON)
        fake, _ = self.loop(
            Change("poetry.lock"), asked(KEPT, DROPPED), attack(),
            Consolidate(), Change("poetry.lock", "relocked\n"),
            asked(KEPT, DROPPED), attack(FRESH), RULED, Consolidate(),
            Change("src/app.py"), PASS)
        return fake

    def test_the_fix_turn_gets_the_fix_ruling_and_the_fresh_finding_not_the_drop(
            self):
        fake = self.run_repeated_review()
        goal = [turn for turn in fake.turns if turn.role == IMPLEMENT][2].goal
        for sent in ("load() returns None",
                     "load() still breaks the criterion",
                     "an empty argv crashes"):
            self.assertIn(sent, goal)
        self.assertNotIn("the cache is never warmed", goal)

    def test_the_consolidator_is_not_shown_a_dropped_finding(self):
        fake = self.run_repeated_review()
        first, second = [turn.goal for turn in fake.turns
                         if turn.role == "consolidate"]
        self.assertIn("the cache is never warmed", first)
        self.assertIn("an empty argv crashes", second)
        self.assertNotIn("the cache is never warmed", second)


    def test_a_dropped_adversary_finding_is_kept_from_the_fix_turn(self):
        self.configure(ON)
        fake, _ = self.loop(
            Change("poetry.lock"), asked(KEPT), attack(ADVERSARY_DROP),
            Consolidate(), Change("poetry.lock", "relocked\n"), asked(KEPT),
            attack(ADVERSARY_DROP, FRESH), RULED, Consolidate(),
            Change("src/app.py"), PASS)
        goal = [turn for turn in fake.turns if turn.role == IMPLEMENT][2].goal
        self.assertIn("load() still breaks the criterion", goal)
        self.assertIn("an empty argv crashes", goal)
        self.assertNotIn("the cache is never warmed", goal)

if __name__ == "__main__":
    unittest.main()

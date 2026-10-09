"""The opening pull request body lists the run's held concerns after `Linear:`."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Consolidate, Idle, Reply  # noqa: E402
from loop_fixture import MergeModeFixture  # noqa: E402
from test_adversary import Change, attack, finding  # noqa: E402

from holophyte.pr import github  # noqa: E402

PR = '[merge]\nmode = "pr"\napprove = "human"\n'
MET = "CRITERION 1: met — tests/test_thing.py::test_it_works\n"


def concern(n, message):
    return finding(f"src/c{n}.py", n, message, "concern")


class OtherConcernsTests(MergeModeFixture):
    def test_held_concerns_follow_the_linear_line_and_survive_new_prose(self):
        self.configure(PR + "[review]\nadversary = true\n")
        self.fake_route()
        self.loop(
            Change("poetry.lock"),
            Reply(f"- src/app.py:3 [p1] load() returns None\n\n{MET}"
                  "VERDICT: REQUEST_CHANGES"),
            attack(concern(1, "one"), concern(2, "two"), concern(3, "three"),
                   concern(4, "a cache may go stale")),
            Consolidate(), Change("poetry.lock", "relocked\n"),
            APPROVE, attack(concern(5, "a retry may double-send")),
            Idle(""), provider=self.provider())
        body = self.pr_body.read_text()
        section = ("## Other concerns (2)\n"
                   "- src/c4.py:4 [p1] a cache may go stale (round 1)\n"
                   "- src/c5.py:5 [p1] a retry may double-send (round 2)")
        self.assertIn(section, body)
        self.assertLess(body.index("Linear: KO-131"), body.index(section))
        rewritten = github.replace_pr_text(body, "New prose for the change.")
        self.assertTrue(rewritten.startswith("New prose for the change."))
        self.assertIn(section, rewritten)

    def test_a_run_that_held_no_concern_gets_no_section(self):
        self.configure(PR)
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        body = self.pr_body.read_text()
        self.assertIn("Linear: KO-131", body)
        self.assertNotIn("## Other concerns", body)


if __name__ == "__main__":
    unittest.main()

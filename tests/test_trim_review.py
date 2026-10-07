"""The first review round's prompt names the run's trim commits.

Run: python3 -m unittest discover -s tests -p 'test_trim_review.py' -v
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Idle  # noqa: E402
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402

from tests.test_trim import TRIMMED, WORK, Commits, lines  # noqa: E402

TRADE_OFFS = (
    "Trade-off: parse() returning None on a blank file survives -- guarded by"
    " test_parse_reads_lines",
    "  Trade-off: the second retry branch survives -- guarded by"
    " test_retry_once",
)
PROOF = "Proof: test_parse_twice -- test_parse_reads_lines hits the same branch"
BLOCKER = ("A trade-off on a trust boundary, an auth check, a data-loss path"
           " or a money path is a blocker.")


class TrimReviewTests(LoopFixture):
    def first_review_prompt(self, *script):
        self.configure(TRIMMED)
        fake, _ = self.loop(*script, provider=StubProvider(a_task()))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        return next(turn.goal for turn in fake.turns if turn.role == "review")

    def test_round_one_names_each_trim_commit_and_quotes_its_trade_offs(self):
        tests_message = "trim: tests\n\n" + "\n".join((*TRADE_OFFS, PROOF))
        prompt = self.first_review_prompt(
            WORK,
            Commits(Commit("trim: delete", "work.txt", lines(40)),
                    Commit(tests_message, "work.txt", lines(30))),
            APPROVE)
        shas = dict(line.split(" ", 1)[::-1] for line in self.git(
            "log", "main", "--format=%H %s").splitlines())
        for subject in ("trim: delete", "trim: tests"):
            listed = re.search(rf"^- ([0-9a-f]{{7,}}) {subject}$", prompt, re.M)
            self.assertIsNotNone(listed, subject)
            self.assertTrue(shas[subject].startswith(listed.group(1)))
        for line in TRADE_OFFS:
            self.assertIn(f"> {line}\n", prompt)
        self.assertIn(BLOCKER, prompt)

    def test_a_range_without_trim_pass_commits_has_no_trim_section(self):
        prompt = self.first_review_prompt(
            Commit("trim whitespace in parser", "work.txt", lines(60)),
            Idle("nothing worth trimming"), APPROVE)
        self.assertNotIn("Trim commits", prompt)
        self.assertNotIn(BLOCKER, prompt)

"""The first review round's prompt names the run's trim commits.

Run: python3 -m unittest discover -s tests -p 'test_trim_review.py' -v
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, Idle  # noqa: E402

from holophyte.review.briefs import trim_brief  # noqa: E402
from tests.test_trim import WORK, Commits, TrimFixture, lines  # noqa: E402

TRADE_OFFS = (
    "Trade-off: parse() returning None on a blank file survives -- guarded by"
    " test_parse_reads_lines",
    "  Trade-off: the second retry branch survives -- guarded by"
    " test_retry_once",
)
PROOF = "Proof: test_parse_blank duplicated test_parse_reads_lines"
FINAL_STATE = ("Judge behavior at the range's final state: a change a later"
               " commit in the range already undid is not a finding.")
NO_REWRITE = ("a finding asks for a new commit and never asks to squash, amend,"
              " rebase or rework an existing commit.")
BLOCKER = ("A trade-off on a trust boundary, an auth check, a data-loss path"
           " or a money path is a blocker while what it gave up is still"
           " missing at the range's final state.")
PROOF_AT_HEAD = ("A test a `trim: tests` commit deleted that is still absent at"
                 " the range's final state needs a `Proof:` line")
RESTORED_NEEDS_NONE = "a deleted test a later commit restored needs none."


class TrimReviewTests(TrimFixture):
    def first_review_prompt(self, *script):
        fake = self.trimmed(*script)
        self.assert_merged()
        return next(turn.goal for turn in fake.turns if turn.role == "review")

    def test_round_one_names_each_trim_commit_and_quotes_its_trade_offs(self):
        tests_message = "trim: tests\n\n" + "\n".join(TRADE_OFFS + (PROOF,))
        prompt = self.first_review_prompt(
            WORK,
            Commits(Commit("trim: delete", "work.txt", lines(40)),
                    Commit(tests_message, "work.txt", lines(30))),
            APPROVE)
        shas = self.shas()
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


class TrimBriefTests(unittest.TestCase):
    def restored_trim_brief(self, subject):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "user.name=Test",
                     "-c", "user.email=test@example.invalid", *args],
                    cwd=root, capture_output=True, text=True,
                    check=True).stdout.strip()

            git("init", "-q")
            work = root / "work.txt"
            work.write_text(lines(60))
            git("add", "work.txt")
            git("commit", "-qm", "work")
            base = git("rev-parse", "HEAD")
            work.write_text(lines(40))
            git("commit", "-qam", subject)
            work.write_text(lines(60))
            git("commit", "-qam", "restore what the trim removed")
            head = git("rev-parse", "HEAD")
            self.assertEqual(git("diff", base, head), "")
            brief = " ".join(trim_brief(root, base, head).split())
        self.assertRegex(brief, rf"- [0-9a-f]{{7,}} {subject} ")
        return brief

    def test_a_restored_trim_brief_judges_at_head_and_never_asks_a_rewrite(self):
        brief = self.restored_trim_brief("trim: delete")
        self.assertIn(FINAL_STATE, brief)
        self.assertIn(NO_REWRITE, brief)

    def test_a_restored_test_deletion_needs_no_proof_line(self):
        brief = self.restored_trim_brief("trim: tests")
        self.assertIn(PROOF_AT_HEAD, brief)
        self.assertIn(RESTORED_NEEDS_NONE, brief)

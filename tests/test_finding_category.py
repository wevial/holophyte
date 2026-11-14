"""A reviewer's finding names its kind of problem from a closed list."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from holophyte.loop.review_round import round_prompt  # noqa: E402
from holophyte.review.adversary import Family, Pass, brief  # noqa: E402
from holophyte.review.briefs import (  # noqa: E402
    CORNER_CASE_RULE,
    ONE_PASS_RULE,
    earlier_rounds_text,
)
from holophyte.review.reply_parsing import parse_findings  # noqa: E402

CATEGORIES = ("correctness", "security", "tests", "performance",
              "maintainability", "docs")


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


class ParsedCategoryTests(unittest.TestCase):
    def test_the_tag_after_the_severity_is_the_category_and_leaves_the_message(self):
        (finding,) = parse_findings(
            "- app/auth.py:12 [p1] [security] token compared with ==\n\n"
            "VERDICT: REQUEST_CHANGES")
        self.assertEqual((finding["path"], finding["line"], finding["severity"],
                          finding["category"]),
                         ("app/auth.py", 12, "p1", "security"))
        self.assertIn("token compared with ==", finding["message"])
        self.assertNotIn("[p1]", finding["message"])
        self.assertNotIn("[security]", finding["message"])

    def test_a_heading_finding_keeps_the_category_it_strips(self):
        (finding,) = parse_findings(
            "# app/auth.py:12 [p1] [security] token compared with ==\n\n"
            "VERDICT: REQUEST_CHANGES")
        self.assertEqual(finding["category"], "security")
        self.assertNotIn("[security]", finding["message"])

    def test_a_tag_outside_the_list_or_no_tag_stores_no_category(self):
        findings = parse_findings(
            "- a/one.py:1 [p2] [Tests] the new branch has no test\n"
            "- a/two.py:2 [p2] [style] the name is long\n"
            "- a/three.py:3 [p2] the loop never ends\n")
        self.assertEqual([finding.get("category") for finding in findings],
                         ["tests", None, None])
        self.assertNotIn("category", findings[1])
        self.assertIn("[style]", findings[1]["message"])

    def test_a_listed_word_in_brackets_later_in_the_sentence_is_not_the_tag(self):
        reply = ("- a/one.py:1 [p1] the [docs] section is stale\n"
                 "- a/two.py:2 [p1] `x[tests]` indexes past the end\n"
                 "- a/three.py:3 the [docs] section is stale\n")
        findings = parse_findings(reply)
        self.assertEqual([finding.get("category") for finding in findings],
                         [None, None, None])
        self.assertEqual([finding["message"] for finding in findings],
                         reply.splitlines())


class BriefTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wt = Path(tmp.name)
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "-c", "user.name=T", "-c", "user.email=t@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "base")
        self.sha = git(self.wt, "rev-parse", "HEAD")

    def briefs(self):
        project = SimpleNamespace(path=self.wt, config=lambda: {},
                                  config_path=self.wt / "config.toml")
        plan = Pass(1, "high", "full", "candidate", self.sha, self.sha,
                    Family("codex"))
        earlier = earlier_rounds_text(
            [(1, ["- a/one.py:1 [p1] the loop never ends"])], self.sha,
            [(self.sha, "fix: end the loop\n\nADDRESS: a/one.py:1")],
            "the VERDICT line")
        return {
            "round_prompt": self.prompt(project),
            "round_2_prompt": self.prompt(project, earlier),
            "adversary": brief(plan, "# A ticket\n", 7)}

    def prompt(self, project, earlier=""):
        return round_prompt(project, 7, "KO-1", self.wt, self.sha, self.sha,
                            "# A ticket\n", [], [], "single", True, "",
                            earlier=earlier)

    def test_each_reviewer_brief_asks_for_one_category_after_the_severity(self):
        for name, text in self.briefs().items():
            with self.subTest(brief=name):
                self.assertEqual(text.count("[p0|p1|p2] [CATEGORY]"), 1)
                self.assertEqual(text.count(", ".join(CATEGORIES)), 1)

    def test_each_reviewer_brief_carries_the_corner_case_rule_once(self):
        for name, text in self.briefs().items():
            with self.subTest(brief=name):
                self.assertEqual(text.count(CORNER_CASE_RULE), 1)

    def test_each_primary_prompt_carries_the_one_pass_rule_once(self):
        briefs = self.briefs()
        for name in ("round_prompt", "round_2_prompt"):
            with self.subTest(brief=name):
                self.assertEqual(briefs[name].count(ONE_PASS_RULE), 1)


if __name__ == "__main__":
    unittest.main()

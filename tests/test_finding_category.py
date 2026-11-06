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

    def test_a_tag_outside_the_list_or_no_tag_stores_no_category(self):
        findings = parse_findings(
            "- a/one.py:1 [p2] [Tests] the new branch has no test\n"
            "- a/two.py:2 [p2] [style] the name is long\n"
            "- a/three.py:3 [p2] the loop never ends\n")
        self.assertEqual([finding.get("category") for finding in findings],
                         ["tests", None, None])
        self.assertNotIn("category", findings[1])
        self.assertIn("[style]", findings[1]["message"])


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
        return {
            "round_prompt": round_prompt(project, 7, "KO-1", self.wt, self.sha,
                                         self.sha, "# A ticket\n", [], [],
                                         "single", True, ""),
            "adversary": brief(plan, "# A ticket\n", 7)}

    def test_each_reviewer_brief_asks_for_one_category_after_the_severity(self):
        for name, text in self.briefs().items():
            with self.subTest(brief=name):
                self.assertEqual(text.count("[p0|p1|p2] [CATEGORY]"), 1)
                self.assertEqual(text.count(", ".join(CATEGORIES)), 1)


if __name__ == "__main__":
    unittest.main()

"""A covering citation may name a test by its quoted title in any script file."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from holophyte.review import reply_parsing

TITLE = ("the README pictures: a section thread hermes answers, "
         "the decisions answered, a passage comment")


class ApprovedRepoCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test reviewer"),
                     ("config", "user.email", "reviewer@example.test")):
            self.git(*args)

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def approve(self):
        self.approved = self.commit("approved")
        self.write("README.md", "# Pictures\n")
        self.candidate = self.commit("candidate")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, subject):
        self.git("add", ".")
        self.git("commit", "-qm", subject)
        return self.git("rev-parse", "HEAD")

    def findings(self, reference):
        reply = (f"CRITERION 1: met — approval at {self.approved[:7]}; "
                 f"{reference}")
        return reply_parsing.criteria_findings(
            reply, ["c1"], self.root,
            approved_range=(self.approved, self.candidate))


class ScriptTitleApprovalCitationTests(ApprovedRepoCase):
    def setUp(self):
        super().setUp()
        self.write("e2e/smoke/readme.capture.ts",
                   f"test('{TITLE}', async () => {{}});\n")
        self.write("src/lib/format.ts",
                   "export function formatDate(d: Date) { return ''; }\n")
        self.approve()

    def test_double_quoted_title_in_capture_file_witnesses_criterion(self):
        self.assertEqual(self.findings(
            f'e2e/smoke/readme.capture.ts::"{TITLE}"'), [])

    def test_single_quoted_title_in_capture_file_witnesses_criterion(self):
        self.assertEqual(self.findings(
            f"e2e/smoke/readme.capture.ts::'{TITLE}'"), [])

    def test_absent_title_in_capture_file_leaves_criterion_unwitnessed(self):
        (finding,) = self.findings(
            'e2e/smoke/readme.capture.ts::"the README pictures: the inbox"')
        self.assertIn("CRITERION 1: unwitnessed", finding["message"])
        self.assertIn("e2e/smoke/readme.capture.ts", finding["message"])
        self.assertIn("the README pictures: the inbox", finding["message"])
        self.assertIn("no test named", finding["message"])

    def test_bare_name_in_ordinary_source_file_is_not_a_test(self):
        (finding,) = self.findings("src/lib/format.ts::formatDate")
        self.assertEqual(
            finding["message"].splitlines()[0],
            "CRITERION 1: unwitnessed — named test not found: "
            "prior approval must name a test")


ROUTE_TEST = "app/api/(transaction)/document/[documentId]/update/test/route.test.ts"
ROUTE_TITLE = "gives $label document\\'s receipt the document\\'s visibility"


class RouteSegmentPathCitationTests(ApprovedRepoCase):
    def setUp(self):
        super().setUp()
        self.write(ROUTE_TEST, f"it('{ROUTE_TITLE}', async () => {{}});\n")
        self.approve()

    def test_title_under_route_group_and_dynamic_segment_witnesses_criterion(self):
        self.assertEqual(self.findings(f"{ROUTE_TEST}::'{ROUTE_TITLE}'"), [])

    def test_citation_in_prose_parentheses_reads_path_from_its_first_word(self):
        for prose in (f"(see {ROUTE_TEST}::'{ROUTE_TITLE}')",
                      f"({ROUTE_TEST}::'{ROUTE_TITLE}')"):
            with self.subTest(prose=prose):
                self.assertEqual(self.findings(prose), [])

    def test_absent_title_under_route_segments_names_full_path_as_not_found(self):
        (finding,) = self.findings(f"{ROUTE_TEST}::'gives no receipt'")
        self.assertIn(f"{ROUTE_TEST}::gives no receipt", finding["message"])
        self.assertIn('no test named "gives no receipt"', finding["message"])
        self.assertNotIn("outside the worktree", finding["message"])


if __name__ == "__main__":
    unittest.main()

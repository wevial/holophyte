"""A covering citation may name a test by its quoted title in any script file."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from holophyte.review import reply_parsing

TITLE = ("the README pictures: a section thread hermes answers, "
         "the decisions answered, a passage comment")


class ScriptTitleApprovalCitationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test reviewer"),
                     ("config", "user.email", "reviewer@example.test")):
            self.git(*args)
        test = self.root / "e2e/smoke/readme.capture.ts"
        test.parent.mkdir(parents=True)
        test.write_text(f"test('{TITLE}', async () => {{}});\n")
        self.approved = self.commit("approved")
        (self.root / "README.md").write_text("# Pictures\n")
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

    def test_double_quoted_title_in_capture_file_witnesses_criterion(self):
        self.assertEqual(self.findings(
            f'e2e/smoke/readme.capture.ts::"{TITLE}"'), [])

    def test_single_quoted_title_in_capture_file_witnesses_criterion(self):
        self.assertEqual(self.findings(
            f"e2e/smoke/readme.capture.ts::'{TITLE}'"), [])


if __name__ == "__main__":
    unittest.main()

"""The criteria brief lists the exact test names the named files define.

Run: python3 -m unittest discover -s tests -p 'test_criteria_brief_names.py' -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holophyte.review.briefs import criteria_brief  # noqa: E402
from holophyte.review.reply_parsing import criteria_findings  # noqa: E402

PYTHON = "tests/test_alpha.py"
SPEC = "console/tests/app/Form.test.tsx"
CRITERIA = [
    f"Given a ticket, then it files. (`{PYTHON}` witnesses it.)",
    f"Given a form, then it saves. (`{SPEC}` witnesses it.)",
    "Given no file, then nothing. (`tests/test_absent.py` witnesses it.)",
    "Given a pattern, then nothing. "
    "(A test module matching `tests/test_alpha*.py` witnesses it.)",
]


class CriteriaBriefNamesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "tests").mkdir()
        (self.root / PYTHON).write_text(
            "import unittest\n\n\n"
            "class AlphaTests(unittest.TestCase):\n"
            "    def test_files_its_link_and_boards(self):\n        pass\n\n"
            "    def helper(self):\n        pass\n\n\n"
            "def test_removal_clears_the_dependency():\n    pass\n")
        (self.root / SPEC).parent.mkdir(parents=True)
        (self.root / SPEC).write_text(
            'describe("form", () => {\n'
            '  it("saves the \\"draft\\" title", () => {});\n'
            "  test('keeps the board', () => {});\n"
            "});\n")
        self.brief = criteria_brief(CRITERIA, wt=self.root)

    def test_each_present_file_lists_its_exact_names_under_its_path(self):
        self.assertIn(
            f"`{PYTHON}`:\n"
            f"- {PYTHON}::test_files_its_link_and_boards\n"
            f"- {PYTHON}::test_removal_clears_the_dependency\n", self.brief)
        self.assertIn(
            f"`{SPEC}`:\n"
            f'- {SPEC}::"keeps the board"\n'
            f'- {SPEC}::"saves the \\"draft\\" title"\n', self.brief)
        self.assertIn("copy a test name from this list verbatim", self.brief)
        self.assertNotIn("helper", self.brief)

    def test_a_missing_file_or_a_pattern_lists_no_names(self):
        self.assertNotIn("test_absent.py`:", self.brief)
        self.assertNotIn("test_alpha*.py`:", self.brief)

    def test_every_listed_name_passes_the_witness_check(self):
        listed = [line[2:] for line in self.brief.splitlines()
                  if line.startswith("- ")]
        self.assertEqual(len(listed), 4)
        for name in listed:
            with self.subTest(name=name):
                self.assertEqual(criteria_findings(
                    f"CRITERION C1: met — {name}\n", CRITERIA[:1],
                    self.root), [])


if __name__ == "__main__":
    unittest.main()

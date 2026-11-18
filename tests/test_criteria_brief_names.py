"""The criteria brief lists the exact test names the named files define.

Run: python3 -m unittest discover -s tests -p 'test_criteria_brief_names.py' -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from holophyte.review.briefs import criteria_brief  # noqa: E402
from holophyte.review.reply_parsing import (  # noqa: E402
    criteria_findings,
    test_references,
)

PYTHON = "tests/test_alpha.py"
SPEC = "console/tests/app/Form.test.tsx"
ROUTE = "console/app/(admin)/[documentId]/Route.test.tsx"
NESTED = "console/python/tests/test_async.py"
GO = "server/witness_test.go"
CRITERIA = [
    f"Given a ticket, then it files. (`{PYTHON}` witnesses it.)",
    f"Given a form, then it saves. (`{SPEC}` witnesses it.)",
    "Given no file, then nothing. (`tests/test_absent.py` witnesses it.)",
    "Given a pattern, then nothing. "
    "(A test module matching `tests/test_alpha*.py` witnesses it.)",
    "Given `tests/test_legacy.py` is gone, then it stays gone. "
    f"(`{PYTHON}` witnesses it.)",
    f"Given a route, then it renders. (`{ROUTE}` witnesses it.)",
    f"Given a nested module, then it runs. (`{NESTED}` witnesses it.)",
    f"Given a Go route, then it renders. (`{GO}` witnesses it.)",
]


class CriteriaBriefNamesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.root / "tests").mkdir()
        (self.root / PYTHON).write_text(
            "import unittest\n\n\n"
            "class AlphaTests(unittest.IsolatedAsyncioTestCase):\n"
            "    def test_files_its_link_and_boards(self):\n        pass\n\n"
            "    async def test_waits_for_the_board(self):\n        pass\n\n"
            "    def helper(self):\n        pass\n\n\n"
            "class BetaTests(unittest.TestCase):\n"
            "    def test_files_its_link_and_boards(self):\n        pass\n\n\n"
            "def test_removal_clears_the_dependency():\n    pass\n\n\n"
            "async def test_saves():\n    pass\n")
        (self.root / "tests" / "test_legacy.py").write_text(
            "def test_old_path():\n    pass\n")
        (self.root / SPEC).parent.mkdir(parents=True)
        (self.root / SPEC).write_text(
            'describe("form", () => {\n'
            '  it("saves the \\"draft\\" title", () => {});\n'
            "  test('keeps the board', () => {});\n"
            '  it("saves \\u2603", () => {});\n'
            '  it("renders \\u2603 in a \\"draft\\"", () => {});\n'
            "});\n"
            '// test.each([1])("retired title", () => {});\n'
            'test.each([[true, "a"], [false, `)`]] as const)'
            '("merges with readiness %s", async (ready) => {});\n'
            'test.each([/[)]/])("matches a bracket %s", (re) => {});\n'
            'const phantom = /test.each\\(\\[1\\]\\)\\("phantom"\\)/;\n'
            'test.concurrent("loads the search", async () => {});\n')
        (self.root / ROUTE).parent.mkdir(parents=True)
        (self.root / ROUTE).write_text('it("routes the document", () => {});\n')
        (self.root / NESTED).parent.mkdir(parents=True)
        (self.root / NESTED).write_text(
            "import unittest\n\n\n"
            "class NestedTests(unittest.IsolatedAsyncioTestCase):\n"
            "    def test_runs(self):\n        pass\n\n"
            "    async def test_awaits(self):\n        pass\n")
        (self.root / GO).parent.mkdir(parents=True)
        (self.root / GO).write_text(
            'package server\n\nimport "testing"\n\n'
            "func TestRoute(t *testing.T) {\n"
            '\tt.Run("[draft] renders form", func(t *testing.T) {})\n}\n')
        self.brief = criteria_brief(CRITERIA, wt=self.root)

    def test_each_present_file_lists_its_exact_names_under_its_path(self):
        self.assertIn(
            f"`{PYTHON}`:\n"
            f"- {PYTHON}::AlphaTests::test_files_its_link_and_boards\n"
            f"- {PYTHON}::AlphaTests::test_waits_for_the_board\n"
            f"- {PYTHON}::BetaTests::test_files_its_link_and_boards\n"
            f"- {PYTHON}::test_removal_clears_the_dependency\n", self.brief)
        self.assertIn(
            f"`{SPEC}`:\n"
            f'- {SPEC}::"keeps the board"\n'
            f'- {SPEC}::"loads the search"\n'
            f'- {SPEC}::"matches a bracket %s"\n'
            f'- {SPEC}::"merges with readiness %s"\n'
            rf'- {SPEC}::"renders \\u2603 in a \\\"draft\\\""' "\n"
            f'- {SPEC}::"saves \\\\u2603"\n'
            f'- {SPEC}::"saves the \\"draft\\" title"\n', self.brief)
        self.assertIn("copy a test name from this list verbatim", self.brief)
        self.assertIn(f'`{ROUTE}`:\n- {ROUTE}::"routes the document"\n',
                      self.brief)
        self.assertNotIn("helper", self.brief)
        self.assertNotIn("retired title", self.brief)
        self.assertNotIn("phantom", self.brief.split(f"`{SPEC}`:")[1])
        self.assertIn(f"`{NESTED}`:\n- {NESTED}::NestedTests::test_runs\n",
                      self.brief)
        self.assertIn(f'`{GO}`:\n- {GO}::"[draft] renders form"\n'
                      f"- {GO}::TestRoute\n", self.brief)
        self.assertNotIn("::test_saves", self.brief)

    def test_a_missing_file_or_a_pattern_lists_no_names(self):
        self.assertNotIn("test_absent.py`:", self.brief)
        self.assertNotIn("test_alpha*.py`:", self.brief)

    def test_a_file_named_outside_the_witness_clause_lists_no_names(self):
        self.assertNotIn("test_legacy.py`:", self.brief)

    def test_every_listed_name_passes_the_witness_check(self):
        listed = [line[2:] for line in self.brief.splitlines()
                  if line.startswith("- ")]
        self.assertEqual(len(listed), 15)
        for name in listed:
            with self.subTest(name=name):
                self.assertEqual(len(test_references(name)), 1)
                self.assertEqual(criteria_findings(
                    f"CRITERION C1: met — {name}\n", CRITERIA[:1],
                    self.root), [])


if __name__ == "__main__":
    unittest.main()

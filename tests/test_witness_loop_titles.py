"""A met criterion may cite a script test whose title a loop builds."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from holophyte.review import reply_parsing
from tests.test_capture_playwright import MODULES, _suites

LOOP_SPEC = (
    "import { test } from '@playwright/test';\n"
    "\n"
    "for (const width of [1280, 360]) {\n"
    "  test(`images keep their reserved boxes and the column at ${width} "
    "pixels wide`, async ({ page }) => {\n"
    "    await page.goto('/');\n"
    "  });\n"
    "}\n"
)

TEMPLATE_SPEC = (
    "import { test } from '@playwright/test';\n"
    "\n"
    "const sizes = [320, 640];\n"
    "test(`renders ${sizes.map((s) => `${s}px`).join(\", \")} in one row`, "
    "async () => {});\n"
    "for (const width of [360]) {\n"
    "  test(`keeps the \\`tag\\` and the \\${literal} marker at ${width}`, "
    "async () => {});\n"
    "}\n"
    "test(`a plain backtick title`, async () => {});\n"
)

SPEC = "e2e/checks/images.spec.ts"
TEMPLATES = "e2e/checks/templates.spec.ts"
TITLE = "images keep their reserved boxes and the column at {} pixels wide"

PLAYWRIGHT_CONFIG = """\
import { defineConfig } from '@playwright/test';

export default defineConfig({ testDir: './e2e', testMatch: '**/*.spec.ts' });
"""


def _met(*citations):
    return "CRITERION 1: met — " + " and ".join(
        f'{path}::"{title}"' for path, title in citations)


class LoopTitleWitnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        spec = self.root / SPEC
        spec.parent.mkdir(parents=True)
        spec.write_text(LOOP_SPEC)
        (self.root / TEMPLATES).write_text(TEMPLATE_SPEC)

    def findings(self, path, title):
        return reply_parsing.criteria_findings(
            _met((path, title)), ["c1"], self.root)

    def assertUnwitnessed(self, path, title):
        findings = self.findings(path, title)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("unwitnessed", findings[0]["message"])
        self.assertIn(f'no test named "{title}"', findings[0]["message"])

    def test_each_title_the_loop_builds_witnesses_the_criterion(self):
        reply = _met(*((SPEC, TITLE.format(width)) for width in (1280, 360)))
        self.assertEqual(
            reply_parsing.criteria_findings(reply, ["c1"], self.root), [])

    def test_differing_text_or_an_empty_placeholder_stays_unwitnessed(self):
        self.assertUnwitnessed(SPEC, "images keep their boxes at 360 pixels wide")
        self.assertUnwitnessed(SPEC, TITLE.format(""))

    def test_a_backtick_title_without_a_placeholder_needs_its_exact_text(self):
        self.assertEqual(self.findings(TEMPLATES, "a plain backtick title"), [])
        self.assertUnwitnessed(TEMPLATES, "a plain backtick title, reworded")

    def test_a_template_of_placeholders_alone_spells_any_one_line_title(self):
        (self.root / "e2e/each.spec.ts").write_text(
            "for (const s of ['alpha']) test(`${s.toUpperCase()}`, f);\n")
        self.assertEqual(self.findings("e2e/each.spec.ts", "ALPHA"), [])

    def test_a_quoted_backtick_opens_no_template(self):
        (self.root / "e2e/quoted.spec.ts").write_text(
            "const marker = \"`\";\n" + LOOP_SPEC)
        self.assertEqual(self.findings("e2e/quoted.spec.ts", TITLE.format(360)), [])

    def test_a_backtick_in_a_regex_literal_opens_no_template(self):
        (self.root / "e2e/regex.spec.ts").write_text(
            "const marker = /`/;\nconst half = 4 / 2;\n" + LOOP_SPEC)
        self.assertEqual(self.findings("e2e/regex.spec.ts", TITLE.format(360)), [])

    def test_a_comment_brace_inside_a_placeholder_does_not_close_it(self):
        (self.root / "e2e/comment.spec.ts").write_text(
            "test(`images at ${width /* } */} pixels wide`, f);\n")
        self.assertEqual(
            self.findings("e2e/comment.spec.ts", "images at 360 pixels wide"), [])

    def test_a_title_template_nested_in_a_placeholder_is_scanned(self):
        (self.root / "e2e/nested.spec.ts").write_text(
            "const r = `registered ${test(`images nested at ${width}`, f)} tests`;\n")
        self.assertEqual(
            self.findings("e2e/nested.spec.ts", "images nested at 360"), [])

    def test_a_go_raw_string_spells_no_test_name(self):
        (self.root / "sample_test.go").write_text(
            "package sample\n\nconst fixture = `Test${name}`\n")
        findings = reply_parsing.criteria_findings(
            "CRITERION 1: met — sample_test.go::TestMissing", ["c1"], self.root)
        self.assertEqual(len(findings), 1, findings)
        self.assertIn("unwitnessed", findings[0]["message"])

    def test_nested_templates_and_escapes_spell_the_runner_title(self):
        self.assertEqual(
            self.findings(TEMPLATES, "renders 320px, 640px in one row"), [])
        self.assertEqual(self.findings(
            TEMPLATES, "keeps the `tag` and the ${literal} marker at 360"), [])
        self.assertUnwitnessed(
            TEMPLATES, "keeps the `tag` and the 42 marker at 360")


class RealPlaywrightListTests(unittest.TestCase):
    def setUp(self):
        modules = os.environ.get(MODULES, "")
        if not (Path(modules) / "@playwright" / "test").is_dir():
            self.skipTest(f"{MODULES} does not name a node_modules directory "
                          "holding @playwright/test")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "node_modules").symlink_to(Path(modules).resolve())
        (self.root / "package.json").write_text('{"type": "module"}\n')
        (self.root / "playwright.config.ts").write_text(PLAYWRIGHT_CONFIG)
        (self.root / SPEC).parent.mkdir(parents=True)
        (self.root / SPEC).write_text(LOOP_SPEC)
        (self.root / TEMPLATES).write_text(TEMPLATE_SPEC)

    def listed_titles(self):
        result = subprocess.run(
            ["npx", "playwright", "test", "--list", "--reporter=json"],
            cwd=self.root, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        listing = json.loads(result.stdout)
        return [(spec["file"], spec["title"])
                for suite in _suites(listing["suites"])
                for spec in suite.get("specs", [])]

    def test_every_title_the_runner_lists_is_witnessed(self):
        listed = self.listed_titles()
        self.assertEqual(
            {(Path(file).name, title) for file, title in listed
             if Path(file).name == Path(SPEC).name},
            {(Path(SPEC).name, TITLE.format(width)) for width in (1280, 360)})
        references = [(str(Path("e2e") / file), None, title)
                      for file, title in listed]
        self.assertEqual(len(references), 5, listed)
        self.assertEqual(
            reply_parsing.missing_witnesses(references, self.root), [])


if __name__ == "__main__":
    unittest.main()

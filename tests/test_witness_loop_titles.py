"""A met criterion may cite a script test whose title a loop builds."""

import tempfile
import unittest
from pathlib import Path

from holophyte.review import reply_parsing

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

SPEC = "e2e/checks/images.spec.ts"
TITLE = "images keep their reserved boxes and the column at {} pixels wide"


class LoopTitleWitnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        spec = self.root / SPEC
        spec.parent.mkdir(parents=True)
        spec.write_text(LOOP_SPEC)

    def test_each_title_the_loop_builds_witnesses_the_criterion(self):
        reply = ("CRITERION 1: met — "
                 + " and ".join(f'{SPEC}::"{TITLE.format(width)}"'
                                for width in (1280, 360)))
        self.assertEqual(
            reply_parsing.criteria_findings(reply, ["c1"], self.root), [])


if __name__ == "__main__":
    unittest.main()

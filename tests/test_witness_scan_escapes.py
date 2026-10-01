import tempfile
import unittest
from pathlib import Path

from holophyte.review.reply_parsing import missing_witnesses

SPEC = "e2e/checks/live.spec.ts"
SPEC_SOURCE = r"""import { test, expect } from '@playwright/test';

test("folded behind \"Add to your comment\" until \"Reply\"", async ({ page }) => {
  await expect(page.getByText('Reply')).toBeVisible();
});

test('a thread\'s composer', async ({ page }) => {
  await expect(page.getByRole('textbox')).toBeVisible();
});
"""


class EscapedWitnessNameTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        spec = self.root / SPEC
        spec.parent.mkdir(parents=True)
        spec.write_text(SPEC_SOURCE)

    def missing(self, name):
        return missing_witnesses([(SPEC, None, name)], self.root)

    def test_name_with_escaped_double_quotes_is_found(self):
        self.assertEqual(
            self.missing('folded behind "Add to your comment" until "Reply"'), [])

    def test_name_with_escaped_apostrophe_in_single_quotes_is_found(self):
        self.assertEqual(self.missing("a thread's composer"), [])

    def test_name_absent_from_the_file_is_reported_missing(self):
        (note,) = self.missing('folded behind "Add a reply"')
        self.assertIn('no test named "folded behind "Add a reply""', note)


if __name__ == "__main__":
    unittest.main()

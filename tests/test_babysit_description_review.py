"""Which covering-review replies count as faulting only the PR description."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from holophyte.babysit.description_review import (  # noqa: E402 - after the sys.path insert
    description_findings,
)

TAGGED = '- Blocker (path: "PR description" -- it promises a handshake).'
CODE = "- Blocker: src/app.py:3 drops the tab target."
TAIL = ("\nCRITERION 1: met — tests/test_thing.py::test_it_works"
        "\nVERDICT: REQUEST_CHANGES")


class DescriptionFindingsTests(unittest.TestCase):
    def test_a_heading_above_a_tagged_blocker_is_not_a_finding(self):
        for heading in ("Findings:", "**Blockers**", "## Findings", "### Blockers:"):
            with self.subTest(heading=heading):
                self.assertEqual(description_findings(
                    f"{heading}\n{TAGGED}{TAIL}", "REQUEST_CHANGES", []), [TAGGED])
                self.assertEqual(description_findings(
                    f"{heading}\n\n{TAGGED}{TAIL}", "REQUEST_CHANGES", []), [TAGGED])

    def test_a_heading_does_not_hide_an_untagged_code_blocker(self):
        self.assertEqual(description_findings(
            f"Findings:\n{TAGGED}\n{CODE}{TAIL}", "REQUEST_CHANGES", []), [])

    def test_an_unlisted_code_blocker_after_a_tagged_item_is_not_hidden(self):
        for code in ("**P1 — load() crashes on empty input**",
                     "P1: load() crashes on empty input"):
            with self.subTest(code=code):
                self.assertEqual(description_findings(
                    f"{TAGGED}\n{code}{TAIL}", "REQUEST_CHANGES", []), [])

    def test_a_code_blocker_mentioning_the_tag_is_not_a_description_finding(self):
        mention = CODE[:-1] + ' (also noted under path: "PR description").'
        self.assertEqual(description_findings(
            f"{mention}{TAIL}", "REQUEST_CHANGES", []), [])


if __name__ == "__main__":
    unittest.main()

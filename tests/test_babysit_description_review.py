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


if __name__ == "__main__":
    unittest.main()

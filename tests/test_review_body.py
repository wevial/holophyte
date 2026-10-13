"""Outside-diff findings parsed out of a bot's review body."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from holophyte.babysit.review_body import findings  # noqa: E402

REVIEW = (HERE / "fixtures" / "pullrequest"
          / "coderabbit_outside_diff_review.md").read_text()


class OutsideDiffParseTests(unittest.TestCase):
    def test_fixture_yields_its_one_outside_diff_finding(self):
        (finding,) = findings(REVIEW)
        self.assertEqual((finding.path, finding.start, finding.end,
                          finding.severity, finding.title),
                         ("src/app.py", 40, 52, "Major",
                          "Retry loop never resets its backoff after a success."))
        self.assertIn("In src/app.py around lines 40 to 52, reset the delay",
                      finding.text)
        self.assertNotIn("named constant", finding.text)
        self.assertNotIn("12-14", finding.text)

    def test_a_short_count_also_yields_the_whole_section(self):
        torn = "> `60`: _🟡 Minor_\n>\n> The second finding lost its title.\n>\n"
        body = REVIEW.replace("Outside diff range comments (1)",
                              "Outside diff range comments (2)").replace(
            "> </blockquote></details>\n>\n> </blockquote></details>",
            torn + "> </blockquote></details>\n>\n> </blockquote></details>")
        parsed = findings(body)
        self.assertEqual(len(parsed), 2)
        whole, unsplit = parsed
        self.assertEqual((whole.path, whole.start), ("src/app.py", 40))
        self.assertEqual(unsplit.path, "")
        self.assertIn("Retry loop never resets", unsplit.text)
        self.assertIn("The second finding lost its title.", unsplit.text)

    def test_a_details_tag_quoted_in_a_fence_is_text(self):
        body = REVIEW.replace(
            "after a successful call.\n",
            "after a successful call.\n"
            "> Preserve the literal <details> tag in the generated HTML.\n")
        (finding,) = findings(body)
        self.assertEqual((finding.path, finding.start), ("src/app.py", 40))
        self.assertIn("Preserve the literal <details> tag", finding.text)

    def test_a_location_line_quoted_in_a_fence_stays_in_its_finding(self):
        body = REVIEW.replace(
            "after a successful call.\n",
            "after a successful call.\n> `60`: example output to preserve\n"
            "> Keep the cap at five.\n")
        (finding,) = findings(body)
        self.assertEqual((finding.start, finding.end), (40, 52))
        self.assertIn("Keep the cap at five.", finding.text)

    def test_a_details_tag_in_prose_is_text(self):
        body = REVIEW.replace(
            "> After one failed call",
            "> Preserve the literal <details> tag in the generated HTML.\n"
            ">\n> After one failed call")
        (finding,) = findings(body)
        self.assertEqual((finding.path, finding.start), ("src/app.py", 40))
        self.assertIn("Preserve the literal <details> tag", finding.text)
        self.assertIn("reset the delay to its initial value", finding.text)

    def test_a_location_line_in_prose_stays_in_its_finding(self):
        body = REVIEW.replace(
            "> After one failed call",
            "> `60`: The delay must reset here.\n>\n> After one failed call")
        (finding,) = findings(body)
        self.assertEqual((finding.start, finding.end), (40, 52))
        self.assertIn("`60`: The delay must reset here.", finding.text)
        self.assertIn("After one failed call", finding.text)

    def test_a_section_that_never_closes_yields_its_whole_text(self):
        body = REVIEW.replace("> </blockquote></details>\n", "")
        (unsplit,) = findings(body)
        self.assertEqual(unsplit.path, "")
        self.assertIn("Retry loop never resets", unsplit.text)
        self.assertNotIn("named constant", unsplit.text)

    def test_a_fenced_section_example_in_review_details_is_not_a_finding(self):
        body = ("<details>\n<summary>📜 Review details</summary>\n\n"
                "```\n<details>\n"
                "<summary>⚠️ Outside diff range comments (1)</summary>"
                "<blockquote>\n```\n\n</details>\n")
        self.assertEqual(findings(body), [])


if __name__ == "__main__":
    unittest.main()

"""Fallback pull request descriptions."""
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.loop
import holophyte.pullrequest
from holophyte import pr


class PrBodyStubTests(unittest.TestCase):
    def test_stub_uses_only_the_first_summary_paragraph(self):
        body = pr.pr_body_stub(
            {"id": "KO-441", "body": "# Contract\n\n## Summary\n\n"
             "Describe the change\non two lines.\n  \nSecond paragraph.\n\n"
             "## Acceptance criteria\n\nMust pass."},
            "the turn ran out of time", "https://linear.app/example/KO-441")
        self.assertEqual(
            body, "Describe the change on two lines.\n\n"
            "The description could not be written: the turn ran out of time"
            "\n\nLinear: KO-441 (https://linear.app/example/KO-441)")

    def test_stub_caps_summary_and_handles_a_missing_summary(self):
        body = pr.pr_body_stub(
            {"id": "KO-441", "body": "## Summary\n" + "Long text. " * 80},
            "empty reply", None)
        self.assertEqual(len(body.split("\n\n")[0]), 600)
        self.assertNotIn("## Summary", body)
        self.assertEqual(
            pr.pr_body_stub({"id": "KO-441"}, "empty reply", None),
            "The description could not be written: empty reply\n\nLinear: KO-441")

    def test_a_timeout_or_empty_body_uses_the_stub(self):
        target = SimpleNamespace(config=lambda: {})
        for reply, timed_out, reason in (
                ("TITLE: A title\n\nPartial text", True, "ran out of time"),
                ("TITLE: A title\n", False, "empty body")):
            with self.subTest(timed_out=timed_out), patch(
                    "holophyte.pullrequest.sh", return_value=""), patch(
                    "holophyte.babysitter.conventions", return_value=[]), patch.object(
                    holophyte.loop, "_timed", return_value=(reply, timed_out)):
                title, body = holophyte.pullrequest._written_pr_text(
                    target, None, None, "KO-131", "add a thing", "task/ko-131",
                    "## Summary\nThe thing, added.", 60, Path("/unused"),
                    monotonic(), 5, None)
            self.assertEqual(title, "KO-131: add a thing")
            self.assertTrue(body.startswith("The thing, added.\n\n"))
            self.assertIn("The description could not be written: ", body)
            self.assertIn(reason, body)
            self.assertTrue(body.endswith("Linear: KO-131"))
            self.assertNotIn("Partial text", body)
            self.assertNotIn("## Acceptance criteria", body)

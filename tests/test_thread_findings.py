"""PR finding boundaries: full comments and stable legacy fingerprints."""
import unittest
from unittest.mock import patch

import store
from holophyte import babysitter, pr
from holophyte.review import parse_findings
from holophyte.thread_findings import bounded_raw


class ThreadFindingTests(unittest.TestCase):
    def test_raw_is_redacted_before_the_limit_and_keeps_a_marker(self):
        secret = "secret-that-crosses-the-cut"
        with patch("holophyte.redact._environment_values", frozenset({secret})):
            raw = bounded_raw("x" * 19_995 + secret + "y" * 100)
            self.assertEqual(len(raw), 20_000)
            self.assertTrue(raw.endswith("\n[original comment truncated]"))
            self.assertNotIn("secret", raw)
            self.assertEqual(bounded_raw("<details>" + secret + "</details>"),
                             "<details>[redacted]</details>")

    def test_thread_location_changes_do_not_change_the_legacy_fingerprint(self):
        pull = pr.PullRequest("github.com", "example", "repo", 1, "https://example.test")
        for path, line in (("app.py", 3), ("Dockerfile", None), ("", None)):
            with self.subTest(path=path):
                thread = pr.Thread("T1", path, line, "review-bot",
                                   "**defect**", pull.url)
                args = (pull, 1, (thread,), {1: ("ADDRESS", "fix the defect")},
                        "ok", "abc")
                reply = babysitter.round_reply(*args).rsplit("\n", 1)[0]
                legacy = parse_findings(reply)
                findings = babysitter._thread_findings(*args, ())
                self.assertEqual(findings[0]["path"], path or "(no file)")
                self.assertEqual(store.findings_fingerprint(findings),
                                 store.findings_fingerprint(legacy))

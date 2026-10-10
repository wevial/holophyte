import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from holophyte.loop.gates import InfraFailure
from holophyte.pr import github

STUB = """#!/bin/sh
n=$(($(cat "{calls}" 2>/dev/null || echo 0) + 1))
echo "$n" > "{calls}"
if [ "$n" -le {failures} ]; then
  echo "{error}" >&2
  exit 1
fi
echo '{{}}'
"""


class GhApiRetryTest(unittest.TestCase):
    def stand_in(self, failures, error):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        bindir = root / "bin"
        bindir.mkdir()
        calls = root / "calls"
        gh = bindir / "gh"
        gh.write_text(STUB.format(calls=calls, failures=failures, error=error))
        gh.chmod(0o755)
        path = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.enterContext(patch.dict(os.environ, {"PATH": path}))
        self.enterContext(patch.object(github, "SLEEP", lambda _s: None))
        return SimpleNamespace(path=str(root)), calls

    def test_never_connected_call_is_retried_until_it_answers(self):
        target, calls = self.stand_in(
            2, "Post https://api.github.com/repos/o/r/pulls:"
               " dial tcp 127.0.0.1:443: i/o timeout")

        out = github._gh_output(target, "github.com", "POST",
                                "repos/o/r/pulls", {"title": "t"})

        self.assertEqual(out.strip(), "{}")
        self.assertEqual(calls.read_text().strip(), "3")

    def test_never_connected_call_fails_naming_the_last_attempt(self):
        target, calls = self.stand_in(
            9, "dial tcp 127.0.0.1:443: i/o timeout (attempt $n)")
        sleeps = []
        self.enterContext(patch.object(github, "SLEEP", sleeps.append))

        with self.assertRaises(InfraFailure) as raised:
            github._gh_output(target, "github.com", "PUT",
                              "repos/o/r/pulls/69/merge", {})

        self.assertIn("(attempt 3)", str(raised.exception))
        self.assertEqual(calls.read_text().strip(), "3")
        self.assertEqual(sleeps, [5, 15])

    def test_http_error_status_fails_on_the_first_call(self):
        for error in ("gh: Request failed (HTTP 422)",
                      "gh: Request failed (HTTP 502)",
                      "gh: connection refused (HTTP 502)"):
            with self.subTest(error=error):
                target, calls = self.stand_in(9, error)

                with self.assertRaises(InfraFailure):
                    github._gh_output(target, "github.com", "POST",
                                      "repos/o/r/pulls", {"title": "t"})

                self.assertEqual(calls.read_text().strip(), "1")


if __name__ == "__main__":
    unittest.main()

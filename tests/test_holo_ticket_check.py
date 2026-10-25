"""`holo ticket check` prints and exits as `python3 ticket_template.py` does
on the same arguments, each run as a real process."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.loop_fixture import INVALID_BODY, VALID_BODY
from tests.story_fixture import write_story

ROOT = Path(__file__).resolve().parent.parent


class TicketCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {**os.environ, "HOLOPHYTE_HOME": self.tmp.name}

    def run_both(self, *args):
        holo = subprocess.run(
            [sys.executable, "-m", "holophyte.holo", "ticket", "check", *args],
            cwd=ROOT, env=self.env, capture_output=True, text=True)
        checker = subprocess.run(
            [sys.executable, "ticket_template.py", *args],
            cwd=ROOT, env=self.env, capture_output=True, text=True)
        return holo, checker

    def draft(self, name, body):
        path = Path(self.tmp.name, name)
        path.write_text(body)
        return str(path)

    def test_a_draft_prints_and_exits_as_the_checker_does(self):
        for body, code, verdict in ((VALID_BODY, 0, "OK"),
                                    (INVALID_BODY, 1, "INVALID")):
            path = self.draft(f"{verdict}.md", body)
            holo, checker = self.run_both(path, "--repo", ".")
            self.assertEqual(checker.returncode, code, checker.stderr)
            self.assertEqual(checker.stdout.splitlines()[0],
                             f"{path}: {verdict}")
            self.assertEqual((holo.stdout, holo.returncode),
                             (checker.stdout, checker.returncode))

    def test_a_refused_story_prints_and_exits_as_the_checker_does(self):
        story = write_story(Path(self.tmp.name) / "plans")
        body = story / "story.md"
        body.write_text(body.read_text().replace("## Goal", "## Aim"))
        holo, checker = self.run_both("--story", str(story))
        self.assertEqual(checker.returncode, 1, checker.stderr)
        self.assertEqual(checker.stdout.splitlines()[0], f"{story}: INVALID")
        self.assertEqual((holo.stdout, holo.returncode),
                         (checker.stdout, checker.returncode))


if __name__ == "__main__":
    unittest.main()

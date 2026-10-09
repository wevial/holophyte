"""A verify line that needs Docker is refused for a project whose verify runs
inside the review container, by `ticket_template.py --repo` and by
`--file-ticket`, against a real git repository and a config under a
throwaway `HOLOPHYTE_HOME`."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
import holophyte.config.project

ROOT = Path(__file__).resolve().parent.parent
FLAG_LINE = ("HOLOPHYTE_TEST_DOCKER=1 python3 -m unittest discover -s tests"
             " -p 'test_image.py'")
REMEDY = ("move this check to an operator witness noted in the criterion,"
          " or to a CI job")

TICKET = """\
# Probe the review image

## Summary

Check the review image builds and starts.

## What / Why / How

**What:** The review image builds and runs a trivial command.

**Why:** A broken image stalls every review round.

**How:** Build the image and run a command in it.

## In scope

- The image build probe

## Out of scope

- Publishing the image

## Acceptance criteria

- [ ] Given the image, when it is built, then a command runs inside it.

## Verify command(s)

```
VERIFY
```

## Implementation notes

- The probe lives beside the other image tests.

## Estimate & dependencies

Estimate: 25 min · Depends on: none

## Open questions

- None
"""


class RecordingBoard:
    def __init__(self, text):
        self.calls = []
        self.text = text

    def file(self, *args, **kwargs):
        self.calls.append("file")
        return "KO-9"

    def stored_body(self, identifier):
        return self.text


class DockerInContainerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.dict(os.environ,
                             {"HOLOPHYTE_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "docs").mkdir()
        (self.repo / "tests" / "test_image.py").touch()
        (self.repo / "docs" / "reviewing.md").write_text("docker\n")
        git = ["git", "-C", str(self.repo), "-c", "user.name=t",
               "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-qm", "init"], check=True)
        self.target = holophyte.config.project.Project.locate(self.repo)
        self.ticket = self.root / "TICKET.md"

    def configure(self, agents):
        self.target.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.target.config_path.write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n\n'
            f"[agents]\nimplementer_isolation = {agents}\n")

    def check(self, *verify):
        self.ticket.write_text(TICKET.replace("VERIFY", "\n".join(verify)))
        return subprocess.run(
            [sys.executable, str(ROOT / "ticket_template.py"),
             "--repo", str(self.repo), str(self.ticket)],
            capture_output=True, text=True)

    def docker_problems(self, printed):
        return [line for line in printed.splitlines()
                if "which has no Docker" in line]

    def assert_refused(self, result, *lines):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("INVALID", result.stdout)
        problems = self.docker_problems(result.stdout)
        self.assertEqual(len(problems), len(lines), result.stdout)
        for problem, line in zip(problems, lines):
            self.assertIn(line, problem)
            self.assertIn(REMEDY, problem)

    def assert_accepted(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(": OK", result.stdout)
        self.assertEqual(self.docker_problems(result.stdout), [])

    def test_the_docker_test_flag_is_refused_on_a_container_project(self):
        self.configure('"container"')
        self.assert_refused(self.check(FLAG_LINE), FLAG_LINE)

    def test_the_table_form_of_the_container_backend_is_read(self):
        self.configure('{ backend = "container", memory = "8g" }')
        self.assert_refused(self.check(FLAG_LINE), FLAG_LINE)

    def test_docker_as_a_command_word_is_refused_also_later_in_a_chain(self):
        self.configure('"container"')
        build = "docker build -t probe ."
        chain = "ruff check . && docker run --rm probe true"
        self.assert_refused(self.check(build, chain), build, chain)

    def test_a_project_without_the_container_backend_accepts_the_line(self):
        self.configure('"none"')
        self.assert_accepted(self.check(FLAG_LINE))

    def test_docker_as_an_argument_or_an_unset_flag_is_accepted(self):
        self.configure('"container"')
        for line in ("grep -q docker docs/reviewing.md",
                     FLAG_LINE.replace("DOCKER=1", "DOCKER=0")):
            with self.subTest(line=line):
                self.assert_accepted(self.check(line))

    def test_a_malformed_agents_table_is_named_only_beside_a_docker_line(self):
        self.configure('"podman"')
        result = self.check(FLAG_LINE)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("which is malformed"), 1)
        self.assertIn("[agents]", result.stdout)
        self.assert_accepted(self.check("grep -q docker docs/reviewing.md"))

    def test_filing_refuses_without_touching_the_board(self):
        self.configure('"container"')
        text = TICKET.replace("VERIFY", FLAG_LINE)
        self.ticket.write_text(text)
        board, out = RecordingBoard(text), io.StringIO()
        with patch.object(holophyte.cli.entry, "board_for",
                          return_value=board), \
                contextlib.redirect_stdout(out):
            status = holophyte.cli.entry.cli(
                [str(self.repo), "--file-ticket", str(self.ticket)])
        self.assertEqual(status, 1)
        self.assertIn(FLAG_LINE, out.getvalue())
        self.assertIn(REMEDY, out.getvalue())
        self.assertEqual(board.calls, [])


if __name__ == "__main__":
    unittest.main()

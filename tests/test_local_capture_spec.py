"""A local capture spec the worktree never commits is part of the candidate:
rewriting it reruns the capture, and a fix round that only rewrites it is
verified and reviewed again at the same head."""
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.config.project
import holophyte.loop.implement
import holophyte.loop.review_round
import store
import store.tickets
from holophyte.loop.gates import RunFailure
from holophyte.loop.runs import open_store, set_phase
from holophyte.pr import pr_media

CRITERIA = ["Given a missing file, then load() returns an empty thing"]
TICKET = "Fix load() in `holophyte/load.py` and show it in `ui/page.html`.\n"
SPEC = Path("e2e/capture/KO-1.capture.ts")
CAPTURE = """\
import pathlib, sys
with pathlib.Path(__file__).with_name("runs").open("a") as runs:
    runs.write("run\\n")
spec = pathlib.Path("e2e/capture/KO-1.capture.ts")
if spec.is_file() and spec.read_text() == "BROKEN":
    print("strict mode violation")
    sys.exit(1)
pathlib.Path(sys.argv[-1], "01-state.png").write_bytes(b"png")
"""


def publish(project, wt, output, files, task_id, note, media_repo):
    return "Media lives here.", {
        file: f"https://example.test/{file.name}" for file in files}


class LocalCaptureSpecTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name, "repo")
        self.root.mkdir()
        self.enterContext(patch.dict(
            os.environ, {"HOLOPHYTE_HOME": str(Path(tmp.name, "home"))}))
        script = Path(tmp.name, "capture.py")
        script.write_text(CAPTURE)
        self.runs = script.with_name("runs")
        self.verified = Path(tmp.name, "verified")
        self.verify = shlex.join([sys.executable, "-c",
                                  "import sys; open(sys.argv[1], 'a').write('v')",
                                  str(self.verified)])
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test implementer"),
                     ("config", "user.email", "implementer@example.test"),
                     ("commit", "--allow-empty", "-qm", "base"),
                     ("checkout", "-qb", "task")):
            self.git(*args)
        self.base = self.git("rev-parse", "HEAD")
        for path, text in {
                "holophyte/load.py": "def load():\n    return {}\n",
                "tests/test_load.py": "def test_missing():\n    pass\n",
                "ui/page.html": "<p>load</p>\n"}.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        self.head = self.git("rev-parse", "HEAD")
        self.write_spec("BROKEN")
        self.command = shlex.join([sys.executable, str(script)])
        self.configure(self.command)
        self.enterContext(patch.object(pr_media, "_publish_git", publish))
        self.fix_turns = 0

    def configure(self, command, local="true"):
        self.project = holophyte.config.project.Project.locate(self.root)
        self.project.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.project.config_path.write_text(
            '[merge]\nmode = "pr"\napprove = "auto"\nui_paths = ["ui/**"]\n'
            f"ui_capture = '{command}'\nui_capture_local = {local}\n")

    def run_count(self):
        return len(self.runs.read_text().splitlines()) if self.runs.exists() else 0

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True, stderr=subprocess.PIPE
        ).strip()

    def write_spec(self, text):
        (self.root / SPEC).parent.mkdir(parents=True, exist_ok=True)
        (self.root / SPEC).write_text(text)

    def test_rewritten_spec_is_captured_again_at_the_same_head(self):
        first = pr_media.prepare(self.project, self.root, "KO-1")
        self.write_spec("FIXED")
        second = pr_media.prepare(self.project, self.root, "KO-1")

        self.assertIn("failed (exit 1)", first)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertNotIn("failed (exit 1)", second)
        self.assertIn("![01-state.png](https://example.test/01-state.png)",
                      second)

    def test_unchanged_spec_is_captured_once(self):
        first = pr_media.prepare(self.project, self.root, "KO-1")
        second = pr_media.prepare(self.project, self.root, "KO-1")

        self.assertEqual(self.run_count(), 1)
        self.assertEqual(second, first)

    def test_edited_default_spec_is_captured_again_at_the_same_head(self):
        (self.root / SPEC).unlink()
        default = self.root / "e2e/default.capture.ts"
        for option in ("--default e2e/default.capture.ts",
                       "--default=e2e/default.capture.ts"):
            with self.subTest(option=option):
                self.runs.unlink(missing_ok=True)
                default.write_text("first")
                self.configure(f"{self.command} {option}")

                pr_media.prepare(self.project, self.root, "KO-1")
                default.write_text("second")
                pr_media.prepare(self.project, self.root, "KO-1")

                self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
                self.assertEqual(self.run_count(), 2)

    def test_committed_capture_mode_ignores_uncommitted_capture_files(self):
        self.write_spec("FIXED")
        self.configure(self.command, local="false")

        pr_media.prepare(self.project, self.root, "KO-1")
        self.write_spec("EDITED")
        pr_media.prepare(self.project, self.root, "KO-1")

        self.assertEqual(self.run_count(), 1)

    def review_rounds(self, conn, fix):
        project_id = store.tickets.ensure_project(conn, "team-1", self.root)
        ticket = store.tickets.mirror_ticket(
            conn, project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1",
            acceptance_criteria=CRITERIA, verification_commands=[self.verify],
            time_box_ms=60 * 60 * 1000)
        store.tickets.transition(conn, ticket, "in_flight")
        run_id = store.claim(conn, project_id, ticket)
        set_phase(conn, run_id, "merge_gate")
        replies = ["CRITERION 1: unwitnessed — the capture failed\n"
                   "VERDICT: REQUEST_CHANGES",
                   "CRITERION 1: met — tests/test_load.py::test_missing\n"
                   "VERDICT: APPROVE"]
        self.prompts = []

        def reviewer(target, role, goal, *args, **kwargs):
            self.prompts.append(goal)
            return replies.pop(0)

        def fixer(*args, **kwargs):
            self.fix_turns += 1
            fix()
            return "Repaired the capture spec.", False

        with (patch.object(holophyte.loop.review_round, "agent", reviewer),
              patch.object(holophyte.loop.review_round, "_timed", fixer),
              patch.object(holophyte.loop.implement, "_transport_timed", fixer)):
            return holophyte.loop.review_round._review_rounds(
                self.project, conn, run_id, None, "KO-1", "task", self.root,
                30, self.base, self.head, TICKET, self.verify, (), CRITERIA,
                10, 2)

    def test_fix_round_that_changes_nothing_still_ends_the_run(self):
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        store.init(conn)

        with self.assertRaisesRegex(RunFailure, "fix round made no progress"):
            self.review_rounds(conn, lambda: None)

        self.assertEqual(self.fix_turns, 1)
        self.assertEqual(len(self.prompts), 1)

    def test_fix_round_that_only_rewrites_the_spec_is_reviewed_again(self):
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        store.init(conn)

        result = self.review_rounds(conn, lambda: self.write_spec("FIXED"))

        self.assertEqual(result, (self.head, 2, True))
        self.assertEqual(self.verified.read_text(), "vv")
        self.assertEqual(self.fix_turns, 1)
        for prompt in self.prompts:
            self.assertIn(f"Review commit {self.head} ", prompt)
        self.assertIn("failed (exit 1)", self.prompts[0])
        self.assertNotIn("failed (exit 1)", self.prompts[1])
        self.assertIn("![01-state.png](https://example.test/01-state.png)",
                      self.prompts[1])


if __name__ == "__main__":
    unittest.main()

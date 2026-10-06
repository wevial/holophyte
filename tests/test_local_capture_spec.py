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
from holophyte.loop.runs import open_store, set_phase
from holophyte.pr import pr_media

CRITERIA = ["Given a missing file, then load() returns an empty thing"]
TICKET = "Fix load() in `holophyte/load.py`.\n"
SPEC = Path("e2e/capture/KO-1.capture.ts")
CAPTURE = """\
import pathlib, sys
if pathlib.Path("e2e/capture/KO-1.capture.ts").read_text() == "BROKEN":
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
        command = shlex.join([sys.executable, str(script)])
        self.project = holophyte.config.project.Project.locate(self.root)
        self.project.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.project.config_path.write_text(
            '[merge]\nmode = "pr"\napprove = "auto"\nui_paths = ["ui/**"]\n'
            f"ui_capture = '{command}'\nui_capture_local = true\n")
        self.enterContext(patch.object(pr_media, "_publish_git", publish))

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

    def test_fix_round_that_only_rewrites_the_spec_is_reviewed_again(self):
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        store.init(conn)
        project_id = store.tickets.ensure_project(conn, "team-1", self.root)
        ticket = store.tickets.mirror_ticket(
            conn, project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1",
            acceptance_criteria=CRITERIA, verification_commands=["true"],
            time_box_ms=60 * 60 * 1000)
        store.tickets.transition(conn, ticket, "in_flight")
        run_id = store.claim(conn, project_id, ticket)
        set_phase(conn, run_id, "merge_gate")
        replies = ["CRITERION 1: unwitnessed — the capture failed\n"
                   "VERDICT: REQUEST_CHANGES",
                   "CRITERION 1: met — tests/test_load.py::test_missing\n"
                   "VERDICT: APPROVE"]
        prompts = []

        def reviewer(target, role, goal, *args, **kwargs):
            prompts.append(goal)
            return replies.pop(0)

        def fixer(*args, **kwargs):
            self.write_spec("FIXED")
            return "Repaired the capture spec.", False

        with (patch.object(holophyte.loop.review_round, "agent", reviewer),
              patch.object(holophyte.loop.review_round, "_timed", fixer),
              patch.object(holophyte.loop.implement, "_transport_timed", fixer)):
            result = holophyte.loop.review_round._review_rounds(
                self.project, conn, run_id, None, "KO-1", "task", self.root,
                30, self.base, self.head, TICKET, "true", (), CRITERIA, 10, 2)

        self.assertEqual(result, (self.head, 2, True))
        self.assertIn("failed (exit 1)", prompts[0])
        self.assertNotIn("failed (exit 1)", prompts[1])
        self.assertIn("![01-state.png](https://example.test/01-state.png)",
                      prompts[1])


if __name__ == "__main__":
    unittest.main()

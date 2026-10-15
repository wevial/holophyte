"""A UI capture that fails at a babysit fix head gets one capture fix round
before the covering review."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402
from fake_agent import APPROVE, REQUEST_CHANGES, Commit, FakeAgent, Idle  # noqa: E402
from loop_fixture import BRANCH, VALID_BODY, MergeModeFixture  # noqa: E402

import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_media  # noqa: E402 - after the sys.path insert above

SPEC = Path("e2e/capture/KO-131.capture.ts")
HEADER = "1) [chromium] › e2e/capture/KO-131.capture.ts:9:5 › second view"
ERROR = "Error: locator.click: Test ended."
CAPTURE = f"""\
import os, sys
from pathlib import Path
out = Path(sys.argv[-1])
(out / "01-first.png").write_bytes(b"png")
if Path({str(SPEC)!r}).read_text() != "FIXED":
    print("Running 1 test using 1 worker")
    print("  {HEADER}")
    print("    {ERROR}")
    for n in range(30):
        print(f"    teardown step {{n}} finished")
    print("  1 failed")
    sys.exit(1)
(out / "02-second.png").write_bytes(b"png")
"""
TWO_VIEWS = VALID_BODY.replace(
    "## Implementation notes",
    "## Evidence\n\nFirst view\nSecond view\n\n## Implementation notes")


class SpecFix:
    """The capture fix turn rewrites the ignored local spec; were the turn
    the covering review instead, it asks for changes as that review would."""
    role = ("implement", "review", "adjudicate")

    def __init__(self):
        self.fake = None

    def play(self, cwd, turn):
        if self.fake.turns[turn - 1].role != "implement":
            return REQUEST_CHANGES.play(cwd, turn)
        (cwd / SPEC).write_text("FIXED")
        return "Clicked the second view's tab before it closed."


class CaptureFixRoundTests(cases.BabysitHelpers, MergeModeFixture):
    def test_a_failed_capture_at_a_fix_head_is_fixed_before_its_review(self):
        (self.target / "ui").mkdir()
        (self.target / "ui" / "app.txt").write_text("first\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "ui")
        self.resume_rejected_fix()
        wt = holophyte.config.project.worktree_path(self.project, BRANCH)
        (wt / SPEC).parent.mkdir(parents=True, exist_ok=True)
        (wt / SPEC.parent / ".gitignore").write_text("*\n")
        (wt / SPEC).write_text("BROKEN")
        script = self.db.parent / "capture.py"
        script.write_text(CAPTURE)
        self.configure('[merge]\nmode = "pr"\napprove = "auto"\n'
                       'ui_paths = ["ui/**"]\nui_capture_local = true\n'
                       f'ui_capture = "{sys.executable} {script}"\n')
        self.enterContext(patch.object(holophyte.pr.pr_media, "repo_is_private",
                                       return_value=False))
        self.pr_body.write_text(holophyte.pr.github.pr_body_written(
            "Old.", "KO-131", None))
        self.BODY = TWO_VIEWS
        fix = SpecFix()
        fake = fix.fake = FakeAgent(
            REQUEST_CHANGES, Commit("ui fix", path="ui/app.txt"), fix, APPROVE,
            Idle(""))
        self.loop(fake=fake, provider=self.provider())

        capture_fixes = [turn for turn in fake.turns[2:]
                         if turn.role == "implement"]
        self.assertEqual(len(capture_fixes), 1, fake.roles)
        goal = capture_fixes[0].goal
        self.assertIn("Not captured: 02", goal)
        self.assertIn(HEADER, goal)
        self.assertIn(ERROR, goal)
        review = fake.turns[3]
        self.assertEqual(review.role, "review")
        self.assertNotIn("failed (exit 1)", review.goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"
                                   " ORDER BY id DESC LIMIT 1"), [("merged",)])
        self.assertIn("merge", [kind for kind, _ in self.api_calls()])
        merged = self.git("rev-parse", BRANCH).strip()[:12]
        evidence = holophyte.pr.github.split_pr_body(self.pr_body.read_text())[2]
        self.assertIn(f"Captured at {merged}\n\n", evidence)
        self.assertEqual(self.read("SELECT COUNT(*) FROM runEvents"
                                   " WHERE kind = 'capture_fix_round'"), [(1,)])
        rounds = [text for text, in self.read(
            "SELECT text FROM ledger WHERE kind = 'round'")]
        self.assertTrue(any(text.startswith("Capture fix round 1 of 1")
                            for text in rounds), rounds)

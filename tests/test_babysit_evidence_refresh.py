"""A babysit fix's capture reaches the pull request's Evidence before its review."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))
import babysit_fixture as cases  # noqa: E402
from fake_agent import REQUEST_CHANGES, Commit  # noqa: E402
from loop_fixture import BRANCH, VALID_BODY, MergeModeFixture  # noqa: E402

import holophyte.babysit.babysitter  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_media  # noqa: E402 - after the sys.path insert above


class BabysitEvidenceRefreshTests(cases.BabysitHelpers, MergeModeFixture):
    def test_a_rejected_ui_fix_still_publishes_its_capture(self):
        (self.target / "ui").mkdir()
        (self.target / "ui" / "app.txt").write_text("first\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "ui")
        self.resume_rejected_fix()
        captures = (script := self.db.parent / "capture.py").with_name("log")
        script.write_text(
            "import subprocess, sys\nfrom pathlib import Path\nhead = subprocess"
            ".check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
            f"open({str(captures)!r}, 'a').write(head + '\\n')\n"
            "Path(sys.argv[1], head[:12] + '.png').write_bytes(b'png')\n")
        self.configure('[merge]\nmode = "pr"\nui_paths = ["ui/**"]\n'
                       f'ui_capture = "{sys.executable} {script}"\n')
        self.enterContext(patch.object(holophyte.pr.pr_media, "repo_is_private",
                                       return_value=False))
        a = self.git("rev-parse", BRANCH).strip()[:12]
        self.pr_body.write_text(holophyte.pr.pr_media.append(
            holophyte.pr.github.pr_body_written("Old.", "KO-131", None),
            f"## Evidence\n\nCaptured at {a}\n\n![screen](https://example/{a}.png)"))
        fake, _ = self.loop(REQUEST_CHANGES, Commit("ui fix", path="ui/app.txt"),
                            REQUEST_CHANGES, provider=self.provider())
        b = fake.turns[2].candidate_sha[:12]
        self.assertIn("asked for changes", self.question())
        evidence = holophyte.pr.github.split_pr_body(self.pr_body.read_text())[2]
        self.assertIn(f"Captured at {b}\n\n", evidence)
        self.assertIn(f"{b}.png)", evidence)
        self.assertNotIn(a, evidence)
        self.assertEqual([sha[:12] for sha in captures.read_text().split()
                          ].count(b), 1)
        self.assertNotIn(f"Captured at {a}", fake.turns[2].goal)

    def pr_at_a(self, capture, body=None):
        """A parked pull request whose Evidence reads "Captured at A"."""
        (self.target / "ui").mkdir()
        (self.target / "ui" / "app.txt").write_text("first\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "ui")
        self.resume_rejected_fix()
        self.captures = (script := self.db.parent / "capture.py").with_name("log")
        script.write_text(
            "import subprocess, sys\nfrom pathlib import Path\nhead = subprocess"
            ".check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()\n"
            f"open({str(self.captures)!r}, 'a').write(head + '\\n')\n" + capture)
        self.configure('[merge]\nmode = "pr"\nui_paths = ["ui/**"]\n'
                       f'ui_capture = "{sys.executable} {script}"\n')
        self.enterContext(patch.object(holophyte.pr.pr_media, "repo_is_private",
                                       return_value=False))
        a = self.git("rev-parse", BRANCH).strip()[:12]
        self.old_evidence = (f"## Evidence\n\nCaptured at {a}\n\n"
                             f"![screen](https://example/{a}.png)")
        self.pr_body.write_text(holophyte.pr.pr_media.append(
            holophyte.pr.github.pr_body_written("Old.", "KO-131", None),
            self.old_evidence))
        self.BODY = body or self.BODY
        return a

    def rejected_fix(self, path="ui/app.txt"):
        """A fix at B touching `path`, and a covering review that rejects it."""
        fake, _ = self.loop(REQUEST_CHANGES, Commit("fix", path=path),
                            REQUEST_CHANGES, provider=self.provider())
        self.assertIn("asked for changes", self.question())
        return fake.turns[2].candidate_sha[:12], fake.turns[2].goal

    def evidence(self):
        return holophyte.pr.github.split_pr_body(self.pr_body.read_text())[2]

    def test_a_failed_capture_marks_the_earlier_evidence_stale(self):
        a = self.pr_at_a("raise SystemExit(1)\n")
        b, _ = self.rejected_fix()
        self.assertEqual(
            self.evidence().rstrip(), self.old_evidence.replace(
                "## Evidence\n\n", f"## Evidence\n\nThis Evidence shows {a}; the"
                f" candidate has moved to {b}, and capturing it failed: The UI"
                " capture failed (exit 1).\n\n", 1))
        self.assertEqual([sha[:12] for sha in self.captures.read_text().split()],
                         [b])
        notes = [text for text, in self.read(
            "SELECT text FROM ledger WHERE kind = 'note'")]
        self.assertIn(f"Evidence on {self.URL}: kept the capture at {a}, marked"
                      f" stale; capturing {b} failed: The UI capture failed"
                      " (exit 1).", notes)

    def test_a_capture_missing_a_declared_view_is_a_failure(self):
        self.pr_at_a("Path(sys.argv[1], '01-first.png').write_bytes(b'png')\n",
                     VALID_BODY.replace("## Implementation notes",
                                        "## Evidence\n\nFirst view\nSecond view"
                                        "\n\n## Implementation notes"))
        _, prompt = self.rejected_fix()
        notice = self.evidence().split("\n\n")[1]
        self.assertTrue(notice.endswith(
            "The UI capture saved 1 of 2 views; not captured: 02 Second view"),
            notice)
        self.assertIn("- First view — captured", prompt)
        self.assertIn("- Second view — not captured", prompt)

    def test_a_fix_outside_ui_paths_captures_nothing_before_its_review(self):
        self.pr_at_a("Path(sys.argv[1], head[:12] + '.png').write_bytes(b'png')\n")
        brief = holophyte.babysit.babysitter.evidence_brief
        before_review = []

        def evidence_brief(*args, **kwargs):
            before_review.append((self.captures.exists(), tuple(
                call for call in self.recorded() if call.startswith("gh pr edit"))))
            return brief(*args, **kwargs)

        self.enterContext(patch.object(holophyte.babysit.babysitter,
                                       "evidence_brief", evidence_brief))
        self.rejected_fix(path="README.md")
        self.assertTrue(before_review)
        self.assertEqual(set(before_review), {(False, ())})
        self.assertEqual(self.evidence().rstrip(), self.old_evidence)

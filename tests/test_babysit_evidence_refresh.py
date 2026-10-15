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
from loop_fixture import BRANCH, MergeModeFixture  # noqa: E402

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

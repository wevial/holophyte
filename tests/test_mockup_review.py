"""Every reviewer gets the approved mock-up and the candidate's captured UI."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import SweepTestCase  # noqa: E402

import review_runner  # noqa: E402
from holophyte.agents import review_workspace, roles  # noqa: E402
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.loop.review_round import round_prompt  # noqa: E402
from holophyte.pr import mockup, pr_media  # noqa: E402
from tests import test_pr_media  # noqa: E402

PNG = test_pr_media.PNG
TICKET = "# A gate\n\n## Summary\n\nThe Contract page's read-first gate.\n"


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True,
                                   stderr=subprocess.STDOUT).strip()


def a_repository(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "commit", "-q", "--allow-empty", "-m", "base")
    base = git(path, "rev-parse", "HEAD")
    (path / "gate.tsx").write_text("<Gate />\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "candidate")
    return base, git(path, "rev-parse", "HEAD")


def fetched_mockup(wt):
    """What a fetch leaves: two snapshots and a bundle with one board."""
    directory = wt / mockup.DIRECTORY
    (directory / "bundle-03").mkdir(parents=True)
    (directory / ".gitignore").write_text("*\n")
    (directory / "01.png").write_bytes(PNG)
    (directory / "02.png").write_bytes(PNG + b"second")
    (directory / "bundle-03" / "Gate.dc.html").write_text("<p>Read first</p>\n")
    items = [
        {"kind": "snapshot", "caption": "Gate, unread",
         "path": f"{mockup.DIRECTORY}/01.png"},
        {"kind": "snapshot", "caption": "Gate, read",
         "path": f"{mockup.DIRECTORY}/02.png"},
        {"kind": "bundle", "caption": "Gate boards",
         "path": f"{mockup.DIRECTORY}/bundle-03",
         "boards": [f"{mockup.DIRECTORY}/bundle-03/Gate.dc.html"], "images": []},
    ]
    (directory / "manifest.json").write_text(json.dumps({"items": items}))


def a_capture(wt):
    shot = Path(tempfile.mkdtemp(dir=wt.parent)) / "01-gate.png"
    shot.write_bytes(PNG)
    mockup.clear_captured(wt)
    mockup.keep_captured(wt, shot.parent, [shot])


class ReviewPromptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wt = Path(tmp.name) / "wt"
        self.base, self.sha = a_repository(self.wt)
        self.config = {"merge": {"mode": "pr"}}
        self.project = SimpleNamespace(path=self.wt, config=lambda: self.config,
                                       config_path=Path(tmp.name) / "config.toml")

    def prompt(self):
        return round_prompt(self.project, 7, "KO-1", self.wt, self.base,
                            self.sha, TICKET, [], [], "single", True, "")

    def test_the_round_lists_the_approved_design_and_the_capture(self):
        fetched_mockup(self.wt)
        a_capture(self.wt)
        for mode in ("pr", "local"):
            with self.subTest(mode=mode):
                self.config["merge"]["mode"] = mode
                prompt = self.prompt()
                self.assertIn("`.holophyte/mockup/01.png`: Gate, unread", prompt)
                self.assertIn("`.holophyte/mockup/02.png`: Gate, read", prompt)
                self.assertIn("`.holophyte/mockup/bundle-03/Gate.dc.html`",
                              prompt)
                self.assertIn("`.holophyte/captured/01-gate.png`", prompt)
                self.assertIn("Design-match check", prompt)
                self.assertIn("is a finding unless the ticket text asks for it",
                              prompt)

    def test_without_a_manifest_the_prompt_has_no_design_section(self):
        a_capture(self.wt)
        prompt = self.prompt()
        self.assertNotIn("approved design", prompt)
        self.assertNotIn("Design-match check", prompt)


class CapturedScreenshotTests(unittest.TestCase):
    setUp = test_pr_media.MediaTests.setUp
    git = test_pr_media.MediaTests.git
    candidate = test_pr_media.MediaTests.candidate

    def prepare(self):
        with patch("holophyte.pr.github.origin_url",
                   return_value="https://github.com/example/repo.git"), \
                patch("holophyte.pr.pr_media.repo_is_private", return_value=False):
            return pr_media.prepare(self.target, self.repo, "KO-505")

    def test_a_capture_is_kept_and_a_later_failed_one_leaves_none(self):
        shot = PNG + b"gate"
        self.candidate(script="import sys\nfrom pathlib import Path\n"
                       f'Path(sys.argv[1], "01-gate.png").write_bytes({shot!r})\n')
        self.assertIn("01-gate.png", self.prepare())
        captured = self.repo / mockup.CAPTURED
        self.assertEqual((captured / "01-gate.png").read_bytes(), shot)
        self.assertEqual(self.git("status", "--porcelain"), "")

        self.candidate(script="raise SystemExit(3)")
        self.assertIn("failed", self.prepare())
        self.assertEqual(list(captured.glob("*.png")), [])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_capture_from_an_earlier_head_is_not_shown_as_this_heads(self):
        fetched_mockup(self.repo)
        self.candidate(script="import sys\nfrom pathlib import Path\n"
                       f'Path(sys.argv[1], "01-gate.png").write_bytes({PNG!r})\n')
        self.prepare()
        self.assertIn(f"`{mockup.CAPTURED}/01-gate.png`",
                      mockup.reviewer_brief(self.repo))
        self.git("rm", "-q", "console/src/App.tsx")
        self.git("commit", "-qm", "revert the UI")
        self.assertEqual(self.prepare(), "")
        brief = mockup.reviewer_brief(self.repo)
        self.assertIn("No UI capture was made at this head.", brief)
        self.assertNotIn("01-gate.png", brief)

    def test_screenshots_sharing_a_name_in_two_directories_are_both_kept(self):
        fetched_mockup(self.repo)
        self.candidate(script="import sys\nfrom pathlib import Path\n"
                       "for size in ('desktop', 'mobile'):\n"
                       "    Path(sys.argv[1], size).mkdir()\n"
                       "    Path(sys.argv[1], size, '01-gate.png').write_bytes("
                       f"{PNG!r} + size.encode())\n")
        self.prepare()
        brief = mockup.reviewer_brief(self.repo)
        for size in ("desktop", "mobile"):
            name = f"{mockup.CAPTURED}/{size}/01-gate.png"
            self.assertEqual((self.repo / name).read_bytes(), PNG + size.encode())
            self.assertIn(f"`{name}`", brief)
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_link_the_capture_plants_is_never_written_through(self):
        outside = self.root / "outside.png"
        self.candidate(script="import os, sys\nfrom pathlib import Path\n"
                       f"os.symlink({str(outside)!r}, "
                       f"Path({mockup.CAPTURED!r}, '01-gate.png'))\n"
                       f'Path(sys.argv[1], "01-gate.png").write_bytes({PNG!r})\n')
        self.assertIn("01-gate.png", self.prepare())
        self.assertTrue((self.repo / mockup.CAPTURED / "01-gate.png").is_symlink())
        self.assertFalse(outside.exists())


class ContainerReviewCarryTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.target.rmdir()
        self.base, self.sha = a_repository(self.target)
        (self.target / ".gitignore").write_text("deps/\n")
        git(self.target, "add", ".gitignore")
        git(self.target, "commit", "-q", "-m", "ignore deps")
        self.sha = git(self.target, "rev-parse", "HEAD")
        (self.target / "deps").mkdir()
        self.configure('[worktree]\ncarry = ["deps"]\n')
        self.addCleanup(reset, self.project)

    def carry(self):
        carried = []

        def run_review(*, carry, **_):
            carried.append(carry)
            return "VERDICT: PASS"
        with patch.object(roles.review_runner, "run_review",
                          side_effect=run_review):
            roles.agent(self.project, "review", "judge", self.target,
                        base_sha=self.base, candidate_sha=self.sha,
                        conn=self.conn, run_id=self.a_run())
        return carried[-1]

    def test_the_mockup_directories_follow_the_configured_carry(self):
        self.assertEqual(self.carry(), ["deps"])
        fetched_mockup(self.target)
        a_capture(self.target)
        carry = self.carry()
        self.assertEqual(carry, ["deps", mockup.DIRECTORY, mockup.CAPTURED])

        stage = review_runner.stage_candidate(
            self.target, self.root / "stage", self.base, self.sha, carry=carry)
        for name in (f"{mockup.DIRECTORY}/01.png",
                     f"{mockup.DIRECTORY}/bundle-03/Gate.dc.html",
                     f"{mockup.CAPTURED}/01-gate.png"):
            copy = stage.path / name
            self.assertEqual(copy.read_bytes(), (self.target / name).read_bytes())
            self.assertFalse(copy.stat().st_mode & 0o222, name)
        review_runner._check_clean(stage.path)


class SeatReviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.wt = self.root / "wt"
        base, sha = a_repository(self.wt)
        review_workspace.publish_review_refs(self.wt, base, sha, run_id=9)
        self.addCleanup(review_workspace.cleanup_review_refs, self.wt, 9)

    def test_the_seat_checkout_reads_read_only_copies(self):
        fetched_mockup(self.wt)
        a_capture(self.wt)
        before = {path: path.read_bytes() for path in self.wt.rglob("*")
                  if path.is_file() and ".git" not in path.parts}
        listing = ["sh", "-c", "ls .holophyte/mockup .holophyte/captured && "
                   "find .holophyte -type f -perm /222 -printf 'WRITABLE %p\\n'"]
        seat = SimpleNamespace(turn=lambda goal: listing, named=" ".join,
                               reported_session=lambda output, ask: None)
        env = {key: value for key, value in os.environ.items()
               if key != "HOLOPHYTE_REVIEW_RESUME"}
        with review_workspace.review_scratch(self.wt) as scratch:
            output = review_workspace.table_review(
                seat, "judge", self.wt, scratch, 60, env, "review", run_id=9)
        self.assertEqual(output.exit_code, 0, output)
        self.assertIn("01.png", output)
        self.assertIn("01-gate.png", output)
        self.assertNotIn("WRITABLE", output)
        self.assertEqual(before, {
            path: path.read_bytes() for path in self.wt.rglob("*")
            if path.is_file() and ".git" not in path.parts})
        self.assertTrue(all(os.access(path, os.W_OK) for path in before))
        self.assertEqual(git(self.wt, "worktree", "list").count("\n"), 0)


if __name__ == "__main__":
    unittest.main()

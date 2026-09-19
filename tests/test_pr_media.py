"""PR evidence against a real local bare remote."""

import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from holophyte import pr_media, pullrequest
from holophyte.gates import InfraFailure

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
    "x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


class MediaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.git("commit", "-qm", "base", "--allow-empty")
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        self.git("remote", "add", "origin", "https://github.com/example/repo.git")
        self.git(
            "config",
            "url." + str(self.remote) + ".insteadOf",
            "https://github.com/example/repo.git",
        )
        self.git("checkout", "-qb", "candidate")
        self.config = {
            "merge": {
                "ui_paths": ["console/src/**"],
                "ui_capture": "python3 capture.py",
            }
        }
        self.target = SimpleNamespace(
            path=self.repo,
            config=lambda: self.config,
            config_path=self.root / "config.toml",
        )

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, stderr=subprocess.STDOUT, text=True
        ).strip()

    def candidate(self, path="console/src/App.tsx", script=None):
        file = self.repo / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("changed")
        (self.repo / "capture.py").write_text(
            script
            or "import sys\nfrom pathlib import Path\n"
            'Path("captured").touch()\n'
            f'Path(sys.argv[1], "screen.png").write_bytes({PNG!r})\n'
        )
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")

    def open(self, private=False, error=None):
        with (
            patch(
                "holophyte.loop._timed",
                return_value=("TITLE: A screen\nDescription.", False),
            ),
            patch("holophyte.pr_media.repo_is_private", return_value=private,
                  side_effect=error) as visibility,
            patch("holophyte.pullrequest.ledger") as ledger,
            patch("holophyte.pr.open_pull_request", return_value=None),
            patch("holophyte.pr.create_pull_request", return_value="url") as create,
            patch(
                "holophyte.pr.origin_url",
                return_value="https://github.com/example/repo.git",
            ),
        ):
            pullrequest._open_pr(
                self.target,
                None,
                None,
                "KO-505",
                "task",
                "candidate",
                "",
                1,
                self.repo,
                monotonic(),
                30,
            )
        self.visibility = visibility
        self.ledger = ledger
        return create.call_args.args[3]

    def test_image_body_and_orphan_remote_branch(self):
        self.candidate()
        body = self.open()
        self.assertIn("## Evidence", body)
        self.assertIn(
            "![screen.png](https://raw.githubusercontent.com/"
            "example/repo/pr-media/KO-505/screen.png)",
            body,
        )
        self.assertLess(body.index("## Evidence"), body.index("Linear:"))
        self.visibility.assert_called_once_with(self.target)
        self.assertIn("public", self.ledger.call_args.args[4])
        self.assertIn("raw host", self.ledger.call_args.args[4])
        data = subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "show", "pr-media/KO-505:screen.png"]
        )
        self.assertEqual(data, PNG)
        parents = subprocess.check_output(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "rev-list",
                "--count",
                "pr-media/KO-505",
            ],
            text=True,
        )
        self.assertEqual(parents.strip(), "1")

    def test_private_images_and_cached_visibility_note(self):
        self.candidate(script="import sys\nfrom pathlib import Path\n"
                       'Path(sys.argv[1], "first.png").write_bytes(b"png")\n'
                       'Path(sys.argv[1], "second screen.png").write_bytes(b"png")\n')
        with patch("holophyte.pr.origin_url",
                   return_value="https://github.com/example/repo.git"), patch(
                       "holophyte.pr_media.repo_is_private",
                       return_value=True) as visibility:
            pr_media.prepare(self.target, self.repo, "KO-505")
        body = self.open(private=True)
        visibility.assert_called_once_with(self.target)
        self.visibility.assert_not_called()
        self.assertIn("https://github.com/example/repo/blob/"
                      "pr-media/KO-505/first.png?raw=true", body)
        self.assertIn("https://github.com/example/repo/blob/"
                      "pr-media/KO-505/second%20screen.png?raw=true", body)
        self.assertNotIn("raw.githubusercontent.com", body)
        self.assertIn("private", self.ledger.call_args.args[4])

    def test_failed_visibility_uses_blob_and_records_failure(self):
        self.candidate()
        body = self.open(error=InfraFailure("unavailable"))
        self.assertIn("![screen.png](https://github.com/example/repo/blob/"
                      "pr-media/KO-505/screen.png?raw=true)", body)
        self.visibility.assert_called_once_with(self.target)
        self.ledger.assert_called_once()
        self.assertIn("visibility read failed", self.ledger.call_args.args[4])
        self.assertIn("blob", self.ledger.call_args.args[4])

    def test_legacy_receipt_is_regenerated_for_private_repository(self):
        self.candidate()
        # Freeze the pre-KO-512 cache format to reproduce an upgrade retry.
        legacy_identity = [self.git("rev-parse", "HEAD", "main"), "KO-505",
                           ["console/src/**"], "python3 capture.py",
                           "https://github.com/example/repo.git"]
        legacy_key = hashlib.sha256(json.dumps(legacy_identity).encode()).hexdigest()
        receipt = Path(self.git("rev-parse", "--absolute-git-dir")) / (
            f"pr-media-{legacy_key}.txt")
        receipt.write_text("## Evidence\n\n![screen.png](https://raw.githubusercontent.com/"
                           "example/repo/pr-media/KO-505/screen.png)")

        body = self.open(private=True)

        self.assertIn("![screen.png](https://github.com/example/repo/blob/"
                      "pr-media/KO-505/screen.png?raw=true)", body)
        self.assertNotIn("raw.githubusercontent.com", body)
        self.visibility.assert_called_once_with(self.target)
        self.assertTrue((self.repo / "captured").exists())
        self.assertIn("private", self.ledger.call_args.args[4])
        self.assertEqual(self.open(private=True), body)
        self.visibility.assert_not_called()

    def test_non_ui_and_unconfigured_do_not_capture(self):
        self.candidate("holophyte/loop.py")
        self.assertNotIn("## Evidence", self.open())
        self.assertFalse((self.repo / "captured").exists())
        self.candidate()
        self.config = {}
        self.assertEqual(self.open(), "Description.\n\nLinear: KO-505")
        self.assertFalse((self.repo / "captured").exists())

    def test_failed_or_empty_capture_still_opens(self):
        for script, expected in [
            ("raise SystemExit(7)", "failed"),
            ("pass", "produced no"),
        ]:
            with self.subTest(expected):
                self.candidate(script=script)
                body = self.open()
                self.assertIn("## Evidence", body)
                self.assertIn(expected, body)
                self.assertIn("python3 capture.py", body)

    def test_retry_replaces_media_and_video_is_a_link(self):
        self.candidate()
        self.open()
        self.candidate(
            script="import sys\nfrom pathlib import Path\n"
            'Path(sys.argv[1], "flow.mp4").write_bytes(b"video")\n'
        )
        body = self.open()
        self.assertIn("[flow.mp4](https://github.com/example/repo/blob/"
                      "pr-media/KO-505/flow.mp4?raw=true)", body)
        self.assertNotIn("![flow.mp4]", body)
        names = subprocess.check_output(
            [
                "git",
                "--git-dir",
                str(self.remote),
                "ls-tree",
                "--name-only",
                "pr-media/KO-505",
            ],
            text=True,
        )
        self.assertEqual(names.strip(), "flow.mp4")

    def test_timeout_opens_with_missing_evidence(self):
        self.candidate(script="import time; time.sleep(30)")
        with patch("holophyte.pr_media.CAPTURE_TIMEOUT", 0.05):
            self.assertIn("timed out", self.open())

    def test_visibility_transport_and_invalid_answers(self):
        with patch("holophyte.pr.origin_url",
                   return_value="https://github.com/example/repo.git"), patch(
                       "holophyte.pr_media.shutil.which", return_value="gh"), patch(
                       "holophyte.pr_media.subprocess.run") as run:
            run.return_value = SimpleNamespace(
                returncode=0, stdout='{"isPrivate":true}')
            self.assertTrue(pr_media.repo_is_private(self.target))
            self.assertEqual(run.call_args.args[0],
                             ["gh", "repo", "view", "example/repo",
                              "--json", "isPrivate"])
            run.return_value.stdout = '{"isPrivate":false}'
            self.assertFalse(pr_media.repo_is_private(self.target))
            run.return_value.stdout = '{}'
            with self.assertRaises(ValueError):
                pr_media.repo_is_private(self.target)
        with patch("holophyte.pr.origin_url",
                   return_value="https://github.com/example/repo.git"), patch(
                       "holophyte.pr_media.shutil.which", return_value=None), patch(
                       "holophyte.pr.rest", return_value={"private": True}) as rest:
            self.assertTrue(pr_media.repo_is_private(self.target))
            self.assertEqual(rest.call_args.args[2:], ("GET", "repos/example/repo"))
            rest.return_value = {"private": False}
            self.assertFalse(pr_media.repo_is_private(self.target))
            rest.return_value = {"private": "false"}
            with self.assertRaises(ValueError):
                pr_media.repo_is_private(self.target)

    def test_invalid_ui_configuration(self):
        from holophyte.config_tables import merge_config

        for config in (
            {"ui_paths": ["console/**"]},
            {"ui_capture": "capture"},
            {"ui_paths": "../console", "ui_capture": "capture"},
            {"ui_paths": ["/console/**"], "ui_capture": "capture"},
            {"ui_paths": ["../console/**"], "ui_capture": "capture"},
            {"ui_paths": ["console/**"], "ui_capture": 3},
            {"ui_paths": ["console/**"], "ui_capture": "'"},
        ):
            with self.subTest(config), self.assertRaises(SystemExit):
                self.config = {"merge": config}
                merge_config(self.target)

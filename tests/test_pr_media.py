"""PR evidence against a real local bare remote."""

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

from holophyte import pr_media, pullrequest
from holophyte.gates import InfraFailure
from tests.test_media_store import CREDS, receiver

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
            or "import os, sys\nfrom pathlib import Path\n"
            'assert "HOLOPHYTE_EVIDENCE_STATES" not in os.environ\n'
            'Path("captured").touch()\n'
            f'Path(sys.argv[1], "screen.png").write_bytes({PNG!r})\n'
        )
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")

    def open(self, private=False, error=None, ticket=""):
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
                ticket,
                1,
                self.repo,
                monotonic(),
                30,
            )
        self.visibility = visibility
        self.ledger = ledger
        return create.call_args.args[3]

    def test_isolated_capture_publishes_from_worktree(self):
        import shlex

        from holophyte import isolation
        self.config['agents'] = {'implementer_isolation': 'container'}
        self.candidate()
        states = ['Dialog open', 'Saved']

        def capture(argv, cwd, timeout, *, env):
            mounts = [argv[i + 1] for i, v in enumerate(argv) if v == '--volume']
            self.assertEqual(mounts, [f'{self.repo}:/workspace:rw'])
            self.assertEqual(argv[-3:-1], ['/bin/sh', '-c'])
            script = shlex.split(argv[-1])
            self.assertEqual(script[:2], ['python3', 'capture.py'])
            output = self.repo / Path(script[-1]).relative_to('/workspace')
            self.assertEqual(env['HOLOPHYTE_TICKET'], 'KO-530')
            self.assertEqual(env['HOLOPHYTE_EVIDENCE_STATES'], '\n'.join(states))
            self.assertNotIn('MEDIA_SECRET', env)
            self.assertEqual(self.git('check-ignore', str(output / '01-dialog.png')),
                             str(output / '01-dialog.png'))
            (output / '01-dialog.png').write_bytes(PNG)
            return 0, ''

        with (patch.object(isolation, 'image_ready'),
              patch.object(isolation.review_runner, '_remove_container'),
              patch.object(isolation, 'run_capped', side_effect=capture) as run,
              patch.dict(os.environ, MEDIA_SECRET='host-only'),
              patch('holophyte.pr.origin_url',
                    return_value='https://github.com/example/repo.git'),
              patch.object(pr_media, 'repo_is_private', return_value=False)):
            section = pr_media.prepare(self.target, self.repo, 'KO-530',
                                       evidence_states=states)
        run.assert_called_once()
        self.assertIn('Dialog open — captured', section)
        self.assertEqual(subprocess.check_output(
            ['git', '--git-dir', str(self.remote), 'show',
             'pr-media/KO-530:01-dialog.png']), PNG)
        self.assertFalse(list(self.repo.glob('.holophyte-capture-*')))

    def test_host_capture_preserves_popen_call(self):
        from unittest.mock import ANY
        for agents in ({}, {'implementer_isolation': 'none'}):
            self.config['agents'] = agents
            with patch.object(pr_media.subprocess, 'Popen') as popen:
                popen.return_value.wait.return_value = 0
                error = pr_media._capture('python3 capture.py', self.repo, self.root,
                                          'KO-530', ['Open'], target=self.target)
            self.assertEqual(error, '')
            popen.assert_called_once_with(
                ['python3', 'capture.py', str(self.root)], cwd=self.repo,
                env=dict(os.environ, HOLOPHYTE_TICKET='KO-530',
                         HOLOPHYTE_EVIDENCE_STATES='Open'),
                stdin=subprocess.DEVNULL, stdout=ANY, stderr=ANY,
                start_new_session=True)
            popen.return_value.wait.assert_called_once_with(timeout=300)

    def test_ticket_states_reach_capture_and_review(self):
        from holophyte.review import evidence_brief

        self.config["merge"]["mode"] = "pr"
        states = ["Guest rename dialog open", "Guest renamed"]
        self.candidate(script="import os, sys\nfrom pathlib import Path\n"
                       'assert os.environ["HOLOPHYTE_TICKET"] == "KO-522"\n'
                       'assert os.environ["HOLOPHYTE_EVIDENCE_STATES"] == '
                       f'{chr(10).join(states)!r}\n'
                       f'Path(sys.argv[1], "01-first.png").write_bytes({PNG!r})\n')
        with (
            patch("holophyte.pr_media.repo_is_private", return_value=False),
            patch("holophyte.pr.origin_url",
                  return_value="https://github.com/example/repo.git"),
        ):
            section = pr_media.prepare(self.target, self.repo, "KO-522",
                                       evidence_states=states)
            prompt = evidence_brief(self.target, self.repo, "KO-522",
                                    evidence_states=states)
        self.assertIn("![Guest rename dialog open]", section)
        for line in ("Guest rename dialog open — captured",
                     "Guest renamed — not captured"):
            self.assertIn(line, section)
            self.assertIn(line, prompt)

    def test_capture_brief_names_directory_and_flow_requirement(self):
        from holophyte.loop import _capture_brief

        body = "## Evidence\n\nDialog open\nName saved\n"
        brief = _capture_brief(self.target, body)
        self.assertIn("e2e/capture", brief)
        self.assertIn("01: Dialog open\n02: Name saved", brief)
        self.assertIn("NN-slug.png", brief)
        self.assertIn("recording", brief)
        self.config["merge"]["ui_capture_dir"] = "tests/screens"
        self.assertIn("tests/screens", _capture_brief(self.target, body))
        self.assertEqual(_capture_brief(self.target, "No evidence section"), "")

    def test_bucket_precedes_git_publishers_and_keeps_credentials_out_of_ledger(self):
        self.config["merge"]["media_repo"] = "example/media"
        self.candidate(script="import sys\nfrom pathlib import Path\n"
                       'for name in ("a.png", "b screen.png", "flow.webm"):\n'
                       ' Path(sys.argv[1], name).write_bytes(b"evidence")\n')
        with receiver() as (endpoint, requests), patch.dict(os.environ, CREDS):
            self.config["merge"]["media_bucket"] = {
                "endpoint": endpoint, "bucket": "evidence",
                "public_base": "https://media.example.invalid", "retention_days": 30}
            with patch("holophyte.pr_media._push") as push, patch(
                    "holophyte.pr_media._push_repo") as push_repo:
                body = self.open()
            push.assert_not_called()
            push_repo.assert_not_called()
        self.assertEqual(len(requests), 3)
        prefixes = {path.rsplit("/", 1)[0] for _, path, _, _ in requests}
        self.assertEqual(len(prefixes), 1)
        prefix = prefixes.pop()
        self.assertRegex(prefix, r"^/evidence/repo/KO-505/[A-Za-z0-9_-]{22}$")
        for method, path, headers, data in requests:
            self.assertEqual(method, "PUT")
            self.assertEqual(data, b"evidence")
            self.assertIn("/auto/s3/aws4_request", headers["Authorization"])
            self.assertIn("https://media.example.invalid"
                          + path[len("/evidence"):], body)
            self.assertIn("image/png" if path.endswith(".png") else "video/webm",
                          headers["Content-Type"])
        self.visibility.assert_not_called()
        self.assertIn("30 days", body)
        for value in CREDS.values():
            self.assertNotIn(value, body + str(self.ledger.call_args_list))

    def test_missing_bucket_credentials_reach_evidence_and_review_without_http(self):
        from holophyte.review import evidence_brief

        states = ["Rename dialog open", "Name saved"]
        ticket = "## Evidence\n\n" + "\n".join(states)
        self.candidate(script="import sys\nfrom pathlib import Path\n"
                       f'Path(sys.argv[1], "01-dialog.png").write_bytes({PNG!r})\n')
        self.config["merge"].update(mode="pr", media_bucket={
            "endpoint": "https://objects.example.invalid", "bucket": "evidence",
            "public_base": "https://media.example.invalid"})
        for index, missing in enumerate((tuple(CREDS), (tuple(CREDS)[1],))):
            with self.subTest(missing=missing), patch.dict(os.environ, CREDS), patch(
                    "holophyte.media_store.urlopen") as request:
                for name in missing:
                    os.environ.pop(name, None)
                # Each candidate needs a fresh receipt; review and PR share it.
                self.git("commit", "-qm", f"candidate {index}", "--allow-empty")
                brief = evidence_brief(self.target, self.repo, "KO-505", states)
                body = self.open(ticket=ticket)
                for state in states:
                    self.assertIn(f"{state} — not captured", body)
                    self.assertIn(f"{state} — not captured", brief)
                request.assert_not_called()
                failure = next(line for line in body.splitlines()
                               if "failed to publish evidence to media bucket" in line)
                self.assertIn(missing[0], failure)
                self.assertIn(failure, brief)
                self.assertIn("## Evidence", body)
                for value in CREDS.values():
                    self.assertNotIn(value, body + brief)

    def test_caps_drop_oversized_files_and_videos_before_images(self):
        self.config["merge"].update(media_max_file_mb=1, media_max_total_mb=2)
        self.candidate(script="import sys\nfrom pathlib import Path\n"
                       'for name, size in [("z.png", 1048576), ("y.png", 1048576),'
                       ' ("a.webm", 1048576), ("b.mp4", 524288),'
                       ' ("huge.png", 1048577)]:\n'
                       ' Path(sys.argv[1], name).write_bytes(b"x" * size)\n')
        body = self.open()
        self.assertIn("Dropped `huge.png`: exceeds media_max_file_mb (1 MB).", body)
        self.assertIn("Dropped `a.webm`: exceeds media_max_total_mb (2 MB).", body)
        self.assertIn("Dropped `b.mp4`: exceeds media_max_total_mb (2 MB).", body)
        names = subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "ls-tree", "--name-only",
             "pr-media/KO-505"], text=True).splitlines()
        self.assertEqual(names, ["y.png", "z.png"])

    def test_changed_caps_invalidate_receipt_and_all_dropped_skip_publish(self):
        self.candidate()
        self.assertIn("![screen.png]", self.open())
        self.config["merge"]["media_max_total_mb"] = 0.000001
        with patch("holophyte.pr_media._push") as push:
            body = self.open()
        push.assert_not_called()
        self.assertIn("Dropped `screen.png`: exceeds media_max_total_mb", body)
        self.assertIn("No media remains", body)
        self.assertNotIn("![screen.png]", body)

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

    def media_remote(self):
        remote = self.root / "media.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "evidence",
                        str(remote)], check=True)
        self.git("push", str(remote), "main:evidence")
        config = self.root / "gitconfig"
        config.write_text(
            '[user]\n name = Test\n email = test@example.invalid\n'
            f'[url "{remote}"]\n'
            ' insteadOf = https://github.com/example/media.git\n')
        self.enterContext(patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}))
        self.enterContext(patch("holophyte.pr_media.shutil.which", return_value=None))
        self.enterContext(patch("holophyte.pr.token_from_env",
                                return_value="test-token"))
        self.config["merge"]["media_repo"] = "example/media"
        return remote

    def test_separate_media_repository_and_visibility(self):
        remote = self.media_remote()
        self.candidate(script="import sys\nfrom pathlib import Path\n"
                       f'Path(sys.argv[1], "01-name.png").write_bytes({PNG!r})\n'
                       f'Path(sys.argv[1], "02-name.png").write_bytes({PNG!r})\n')
        for private in (False, True):
            with self.subTest(private=private):
                if private:
                    self.git("commit", "--allow-empty", "-qm", "retry")
                body = self.open(private=private)
                self.visibility.assert_called_once_with(self.target, "example/media")
                for name in ("01-name.png", "02-name.png"):
                    data = subprocess.check_output(
                        ["git", "--git-dir", str(remote), "show",
                         f"evidence:KO-505/{name}"])
                    self.assertEqual(data, PNG)
                    prefix = ("https://github.com/example/media/blob/" if private
                              else "https://raw.githubusercontent.com/example/media/")
                    suffix = "?raw=true" if private else ""
                    self.assertIn(f"{prefix}evidence/KO-505/{name}{suffix}", body)
                self.assertIn("example/media", body)
                self.assertEqual(subprocess.check_output(
                    ["git", "--git-dir", str(self.remote), "for-each-ref",
                     "--format=%(refname) %(objectname)"], text=True),
                    f"refs/heads/candidate {self.git('rev-parse', 'HEAD')}\n")

    def test_separate_media_retries_a_concurrent_push(self):
        remote = self.media_remote()
        self.candidate()
        run = subprocess.run
        pushes = []

        def race(args, **kwargs):
            if args[:3] == ["git", "push", "--porcelain"]:
                pushes.append(args)
                if len(pushes) == 1:
                    other = self.root / "other"
                    run(["git", "clone", "-q", str(remote), str(other)], check=True)
                    (other / "KO-other").write_text("concurrent evidence")
                    run(["git", "add", "."], cwd=other, check=True)
                    run(["git", "commit", "-qm", "other evidence"],
                        cwd=other, check=True)
                    run(["git", "push", "-q"], cwd=other, check=True)
            return run(args, **kwargs)

        with patch("holophyte.pr_media.subprocess.run", side_effect=race):
            body = self.open()
        self.assertEqual(len(pushes), 2)
        self.assertIn("example/media/evidence/KO-505/screen.png", body)
        self.assertEqual(subprocess.check_output(
            ["git", "--git-dir", str(remote), "show", "HEAD:KO-other"]),
            b"concurrent evidence")
        self.assertEqual(subprocess.check_output(
            ["git", "--git-dir", str(remote), "show", "HEAD:KO-505/screen.png"]),
            PNG)

    def test_separate_media_push_failure_does_not_fall_back(self):
        remote = self.media_remote()
        hook = remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.candidate()
        body = self.open()
        self.assertIn("failed to publish evidence", body)
        self.assertIn("example/media", body)
        self.assertEqual(subprocess.check_output(
            ["git", "--git-dir", str(self.remote), "for-each-ref",
             "--format=%(refname) %(objectname)"], text=True),
            f"refs/heads/candidate {self.git('rev-parse', 'HEAD')}\n")
        self.assertEqual(subprocess.check_output(
            ["git", "--git-dir", str(remote), "ls-tree", "-r", "HEAD"],
            text=True), "")

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

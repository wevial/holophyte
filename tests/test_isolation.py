"""Implementer boundaries exercised at the process launch seam."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from holophyte import agents


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.table = {"agents": {"implementer": "agent-cli"}}
        self.target = SimpleNamespace(
            config=lambda: self.table, config_path=self.root / "config.toml"
        )

    def test_none_preserves_process_call(self):
        for backend in (None, "none"):
            if backend:
                self.table["agents"]["implementer_isolation"] = backend
            with patch.object(agents, "run_capped", return_value=(0, "done")) as run:
                self.assertEqual(
                    agents.agent(
                        self.target, "implement", "task", self.root, timeout=17
                    ),
                    "done",
                )
            run.assert_called_once_with(["agent-cli", "task"], self.root, 17)

    def test_container_boundary(self):
        from holophyte import isolation

        source = self.root / "allowed.env"
        source.write_text("ALLOWED=allowed-secret\nBOARD_KEY=excluded\n")
        self.table["worktree"] = {"env_source": str(source), "env_allow": ["ALLOWED"]}
        self.table["agents"].update(
            implementer_isolation="container",
            implementer_credential={"env": "AGENT_KEY"},
        )
        with (
            patch.dict(
                os.environ,
                {
                    "BOARD_KEY": "board-secret",
                    "MEDIA_KEY": "media-secret",
                    "AGENT_KEY": "agent-secret",
                },
            ),
            patch.object(
                isolation.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ),
            patch.object(isolation.review_runner, "_remove_container"),
            patch.object(isolation, "run_capped", return_value=(0, "done")) as run,
        ):
            agents.agent(self.target, "implement", "task", self.root, timeout=17)
        argv = run.call_args.args[0]
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
        self.assertEqual(mounts, [f"{self.root}:/workspace:rw"])
        for flag in (
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network=bridge",
            "--pids-limit=256",
        ):
            self.assertIn(flag, argv)
        self.assertIn("--env=ALLOWED", argv)
        self.assertNotIn("allowed-secret", str(argv))
        self.assertEqual(run.call_args.kwargs["env"]["ALLOWED"], "allowed-secret")
        self.assertIn("--env=AGENT_KEY", argv)
        self.assertNotIn("agent-secret", str(argv))
        self.assertNotIn("board-secret", str(run.call_args))
        self.assertNotIn("media-secret", str(run.call_args))
        self.assertTrue(
            any(v.startswith("--user=") and v != "--user=0:0" for v in argv)
        )
        self.assertEqual(run.call_args.kwargs["env"]["HOME"], "/home/implementer")
        self.assertEqual(argv[-2:], ["agent-cli", "task"])

    def test_file_credential_and_timeout_cleanup(self):
        from holophyte import isolation

        credential = self.root / "auth.json"
        credential.write_text("private")
        route = isolation.Route(
            "container",
            credential={
                "file": str(credential),
                "destination": "/home/implementer/.agent/auth.json",
            },
        )
        with (
            patch.object(isolation, "image_ready"),
            patch.object(isolation.review_runner, "_remove_container") as remove,
            patch.object(
                isolation,
                "run_capped",
                side_effect=subprocess.TimeoutExpired("docker", 1),
            ) as run,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                isolation.launch(route, self.root, {}, ["agent"], timeout=1)
        remove.assert_called_once()
        argv = run.call_args.args[0]
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
        self.assertEqual(
            mounts,
            [
                f"{self.root}:/workspace:rw",
                f"{credential}:/home/implementer/.agent/auth.json:ro",
            ],
        )

    def make_worktree(self):
        from holophyte.isolation_git import git

        main = self.root / "main"
        main.mkdir()
        git(main, "init", "-q")
        git(main, "config", "user.name", "Configured Author")
        git(main, "config", "user.email", "author@example.test")
        git(main, "commit", "--allow-empty", "-qm", "base")
        worktree = self.root / "task"
        git(main, "worktree", "add", "-qb", "task", str(worktree))
        return main, worktree

    def test_linked_worktree_commit_returns_without_host_config(self):
        from holophyte.isolation_git import git, isolated_git

        main, worktree = self.make_worktree()
        original = (worktree / ".git").read_text()
        before = git(main, "rev-parse", "HEAD")
        with isolated_git(worktree) as env:
            self.assertTrue((worktree / ".git").is_dir())
            self.assertEqual(git(worktree, "remote"), "")
            self.assertNotIn(
                "Configured Author", (worktree / ".git/config").read_text()
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-qm", "isolated"],
                cwd=worktree,
                env=dict(os.environ, **env),
                check=True,
            )
            # Container config/hooks are discarded instead of executed on the host.
            (worktree / ".git/config").write_text("[invalid config")
        self.assertEqual((worktree / ".git").read_text(), original)
        self.assertEqual(git(main, "rev-parse", "HEAD"), before)
        self.assertEqual(
            git(worktree, "log", "-1", "--format=%s|%an|%ae"),
            "isolated|Configured Author|author@example.test",
        )
        self.assertEqual(git(worktree, "status", "--porcelain"), "")

    def test_interrupted_index_restore_preserves_host_staging(self):
        from holophyte import isolation_git

        _, worktree = self.make_worktree()
        staged = worktree / "staged"
        staged.write_text("original staged content\n")
        isolation_git.git(worktree, "add", "staged")
        index = Path(isolation_git.git(
            worktree, "rev-parse", "--path-format=absolute", "--git-path", "index"
        ))
        index.chmod(0o640)
        before = index.read_bytes()
        entries = set(index.parent.iterdir())
        copyfile = isolation_git.shutil.copyfile

        def interrupt(source, destination):
            if Path(destination).parent == index.parent:
                Path(destination).write_bytes(b"partial index")
                raise SystemExit(143)
            return copyfile(source, destination)

        with patch.object(isolation_git.shutil, "copyfile", side_effect=interrupt):
            with self.assertRaises(SystemExit):
                with isolation_git.isolated_git(worktree):
                    staged.write_text("new staged content\n")
                    isolation_git.git(worktree, "add", "staged")
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(index.stat().st_mode & 0o777, 0o640)
        self.assertEqual(set(index.parent.iterdir()), entries)
        self.assertEqual(
            isolation_git.git(worktree, "show", ":staged"), "original staged content"
        )
        with isolation_git.isolated_git(worktree):
            isolation_git.git(worktree, "add", "staged")
        self.assertEqual(index.stat().st_mode & 0o777, 0o640)
        self.assertEqual(isolation_git.git(worktree, "show", ":staged"),
                         "new staged content")

    def test_interrupted_object_import_never_publishes_partial_object(self):
        from holophyte import isolation_git

        main, _ = self.make_worktree()
        source = main / ".git" / "objects"
        destination = self.root / "imported"
        destination.mkdir()

        def interrupt(source, target):
            Path(target).write_bytes(b"partial object")
            raise SystemExit(143)

        with patch.object(isolation_git.shutil, "copyfile", side_effect=interrupt):
            with self.assertRaises(SystemExit):
                isolation_git.import_objects(source, destination)
        self.assertFalse([p for p in destination.rglob("*") if p.is_file()])
        isolation_git.import_objects(source, destination)
        for path in source.rglob("*"):
            if path.is_file():
                self.assertEqual((destination / path.relative_to(source)).read_bytes(),
                                 path.read_bytes())

    def test_container_resolves_pending_merge_with_both_parents(self):
        from holophyte import isolation
        from holophyte.claim import reuse_leftover
        from holophyte.isolation_git import git

        main, worktree = self.make_worktree()
        git(main, "branch", "-M", "main")
        for directory, content in ((main, "main"), (worktree, "task")):
            (directory / "conflict").write_text(content)
            git(directory, "add", "conflict")
            git(directory, "commit", "-qm", content)
        parents = [git(worktree, "rev-parse", "HEAD"), git(main, "rev-parse", "HEAD")]
        self.target.path = main
        ok, reason = reuse_leftover(self.target, worktree, "task")
        self.assertTrue(ok, reason)
        self.assertIn("AA conflict", git(worktree, "status", "--porcelain"))

        def resolve(argv, cwd, timeout, *, env):
            # Execute the Git operations with the environment Docker receives.
            (worktree / "conflict").write_text("resolved\n")
            git(worktree, "add", "conflict")
            subprocess.run(["git", "commit", "-qm", "resolved"], cwd=cwd,
                           env=env, check=True, capture_output=True)
            return 0, "resolved"

        with (
            patch.object(isolation, "image_ready"),
            patch.object(isolation.review_runner, "_remove_container"),
            patch.object(isolation, "run_capped", side_effect=resolve),
        ):
            isolation.launch(isolation.Route("container"), worktree, {}, ["agent"])
        self.assertEqual(git(worktree, "log", "-1", "--format=%P").split(), parents)
        git(worktree, "merge-base", "--is-ancestor", "main", "HEAD")
        self.assertEqual(git(worktree, "status", "--porcelain"), "")
        self.assertFalse(Path(git(worktree, "rev-parse", "--path-format=absolute",
                                  "--git-path", "MERGE_HEAD")).exists())

    def test_config_rejects_invalid_boundaries(self):
        from holophyte.isolation import route_for

        for key, value in [
            ("implementer_isolation", "vm"),
            ("implementer_image", ""),
            ("implementer_credential", {"env": "BAD=VALUE"}),
            (
                "implementer_credential",
                {"file": "/tmp/auth", "destination": "/var/run/docker.sock"},
            ),
        ]:
            with self.subTest(key=key, value=value):
                self.table["agents"] = {key: value}
                with self.assertRaises(SystemExit):
                    route_for(self.target)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_container_commit(self):
        import shutil

        from holophyte import isolation
        from holophyte.isolation_git import git

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        main, worktree = self.make_worktree()
        credential = self.root / "credential.json"
        credential.write_text("agent-only")
        route = isolation.Route("container", credential={
            "file": str(credential),
            "destination": "/home/implementer/.agent/auth.json"})
        code, output = isolation.launch(
            route,
            worktree,
            {},
            [
                "/bin/sh",
                "-ec",
                "echo content > created; git add created; git commit -qm isolated",
            ],
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(
            git(worktree, "log", "-1", "--format=%s|%an|%ae"),
            "isolated|Configured Author|author@example.test",
        )
        self.assertEqual((worktree / "created").read_text(), "content\n")
        self.assertEqual(git(main, "log", "-1", "--format=%s"), "base")

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
        from holophyte.target import state_dir

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
            self.assertFalse(state_dir(self.root).exists())

    def test_container_boundary(self):
        from holophyte import isolation

        main, worktree = self.make_worktree()
        self.target.path = main
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
            patch.object(isolation, "image_ready"),
            patch.object(isolation.review_runner, "_remove_container"),
            patch.object(isolation, "run_capped", return_value=(0, "done")) as run,
        ):
            agents.agent(self.target, "implement", "task", worktree, timeout=17)
        argv = run.call_args.args[0]
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
        self.assertEqual(len(mounts), 1)
        self.assertTrue(mounts[0].endswith("/clone:/workspace:rw"))
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

        _, worktree = self.make_worktree()
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
                isolation.launch(route, worktree, {}, ["agent"], timeout=1)
        remove.assert_called_once()
        self.assertFalse(Path(run.call_args.args[1]).exists())
        argv = run.call_args.args[0]
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
        self.assertEqual(
            mounts,
            [
                f"{run.call_args.args[1]}:/workspace:rw",
                f"{credential}:/home/implementer/.agent/auth.json:ro",
            ],
        )

    def make_worktree(self):
        from holophyte.isolation_git import git

        main = self.root / "main"
        main.mkdir()
        git(main, "init", "-q", "-b", "main")
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
            (Path(cwd) / "conflict").write_text("resolved\n")
            git(cwd, "add", "conflict")
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

    def test_inconclusive_turn_preserves_pending_merge_and_staging(self):
        from holophyte import isolation
        from holophyte.isolation_git import git

        main, worktree = self.make_worktree()
        for directory, content in ((main, "main"), (worktree, "task")):
            (directory / "conflict").write_text(content)
            git(directory, "add", "conflict")
            git(directory, "commit", "-qm", content)
        with self.assertRaises(subprocess.CalledProcessError):
            git(worktree, "merge", "main")
        (worktree / "staged").write_text("staged content")
        git(worktree, "add", "staged")
        parents = [git(worktree, "rev-parse", "HEAD"), git(main, "rev-parse", "HEAD")]
        before = git(worktree, "ls-files", "--stage")
        with (
            patch.object(isolation, "image_ready"),
            patch.object(isolation.review_runner, "_remove_container"),
            patch.object(isolation, "run_capped") as run,
        ):
            for code in (0, 1):
                with self.subTest(code=code):
                    run.return_value = (code, "inconclusive")
                    isolation.launch(
                        isolation.Route("container"), worktree, {}, ["agent"]
                    )
                    self.assertEqual(git(worktree, "ls-files", "--stage"), before)
                    self.assertEqual(
                        git(worktree, "rev-parse", "MERGE_HEAD"), parents[1]
                    )
        (worktree / "conflict").write_text("resolved")
        git(worktree, "add", "conflict")
        git(worktree, "commit", "-qm", "resolved afterwards")
        self.assertEqual(git(worktree, "log", "-1", "--format=%P").split(), parents)

    def test_copy_back_refuses_unsafe_entries_without_losing_work(self):
        import socket

        from holophyte import isolation
        from holophyte.gates import InfraFailure
        from holophyte.isolation_git import git

        _, worktree = self.make_worktree()
        (worktree / "keep").write_text("valuable uncommitted work")
        before = git(worktree, "rev-parse", "HEAD")

        def run(argv, cwd, timeout, *, env):
            bad = Path(cwd) / "nested" / "bad"
            bad.parent.mkdir()
            if kind == "fifo":
                os.mkfifo(bad)
            elif kind == "socket":
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.bind(str(bad))
            else:
                (bad.parent / ".git").write_text("nested metadata")
            subprocess.run(["git", "commit", "--allow-empty", "-qm", "new"],
                           cwd=cwd, env=env, check=True, capture_output=True)
            return 0, "done"

        with (
            patch.object(isolation, "image_ready"),
            patch.object(isolation.review_runner, "_remove_container"),
            patch.object(isolation, "run_capped", side_effect=run),
        ):
            for kind in ("fifo", "socket", "nested git"):
                with self.subTest(kind=kind):
                    with self.assertRaisesRegex(InfraFailure, "working files"):
                        isolation.launch(
                            isolation.Route("container"), worktree, {}, ["agent"]
                        )
                    self.assertEqual((worktree / "keep").read_text(),
                                     "valuable uncommitted work")
                    self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
                    self.assertFalse((worktree / "nested").exists())

    def test_clone_turn_returns_commit_and_dirty_file_safely(self):
        from holophyte import isolation
        from holophyte.isolation_git import git

        _, worktree = self.make_worktree()
        sentinel = self.root / "hook-ran"
        mounts = []
        script = "git status; git log -1; echo committed > file; git add file; " \
                 "git commit -qm 'container message'; echo leftover > dirty"

        def run(argv, cwd, timeout, *, env):
            mount = Path(argv[argv.index("--volume") + 1].split(":")[0])
            mounts.append(mount)
            self.assertNotEqual(mount, worktree)
            self.assertFalse((mount / ".git/objects/info/alternates").exists())
            self.assertTrue(Path(git(mount, "rev-parse", "--absolute-git-dir"))
                            .is_relative_to(mount))
            subprocess.run(argv[argv.index(isolation.Route().image) + 1:],
                           cwd=mount, env=env,
                           check=True, capture_output=True)
            hook = mount / ".git/hooks/post-checkout"
            hook.parent.mkdir(exist_ok=True)
            hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
            hook.chmod(0o755)
            git(mount, "config", "alias.container-only", "status")
            git(mount, "config", "uploadpack.packObjectsHook", f"touch {sentinel}")
            return 0, "done"

        with patch.object(isolation, "image_ready"), \
             patch.object(isolation.review_runner, "_remove_container"), \
             patch.object(isolation, "run_capped", side_effect=run):
            isolation.launch(isolation.Route("container"), worktree, {},
                             ["sh", "-ec", script])
        self.assertEqual(git(worktree, "log", "-1", "--format=%s|%an|%ae"),
                         "container message|Configured Author|author@example.test")
        self.assertEqual(git(worktree, "status", "--porcelain"), "?? dirty")
        self.assertEqual((worktree / "dirty").read_text(), "leftover\n")
        subprocess.run(["git", "checkout", "task"], cwd=worktree,
                       check=True, capture_output=True)
        self.assertFalse(sentinel.exists())
        self.assertNotIn("alias.container-only", git(worktree, "config", "--list"))
        self.assertFalse(mounts[0].exists())

    def test_real_timeout_returns_committed_and_dirty_clone_work(self):
        from holophyte import isolation
        from holophyte.gates import run_capped
        from holophyte.isolation_git import git

        _, worktree = self.make_worktree()
        mounts = []
        script = ("echo committed > file; git add file; "
                  "git commit -qm 'before timeout'; "
                  "echo staged > staged; git add staged; "
                  "echo leftover > dirty; echo ready; sleep 30")

        def run(argv, cwd, timeout, *, env):
            mounts.append(Path(cwd))
            return run_capped(argv[argv.index(isolation.Route().image) + 1:],
                              cwd, timeout, env=env)

        with patch.object(isolation, "image_ready"), \
             patch.object(isolation.review_runner, "_remove_container") as remove, \
             patch.object(isolation, "run_capped", side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                isolation.launch(isolation.Route("container"), worktree, {},
                                 ["sh", "-ec", script], timeout=2)
        self.assertIn("ready", raised.exception.output)
        self.assertEqual(git(worktree, "log", "-1", "--format=%s|%an|%ae"),
                         "before timeout|Configured Author|author@example.test")
        self.assertEqual((worktree / "file").read_text(), "committed\n")
        self.assertEqual((worktree / "staged").read_text(), "staged\n")
        self.assertEqual((worktree / "dirty").read_text(), "leftover\n")
        self.assertEqual(git(worktree, "status", "--porcelain"),
                         "?? dirty\n?? staged")
        remove.assert_called_once()
        self.assertFalse(mounts[0].exists())

    def test_copy_back_rolls_back_failed_destination_mutation(self):
        from holophyte.gates import InfraFailure
        from holophyte.isolation_clone import copy_files

        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()
        destination.mkdir()
        (destination / "a").write_text("original uncommitted work")
        (destination / "a").chmod(0o640)
        (destination / "locked").mkdir()
        (destination / "locked/keep").write_text("nested original")
        (destination / "link").symlink_to("a")
        (destination / ".git").write_text("host metadata")
        (destination / ".env").write_text("host environment")
        (source / "a").write_text("replacement")
        (source / "new").write_text("new file")
        rename = Path.rename

        for phase in ("backup", "install"):
            with self.subTest(phase=phase):
                calls = []

                def fail(path, target):
                    matches = (path.parent == destination if phase == "backup"
                               else Path(target).parent == destination)
                    if matches:
                        calls.append(path)
                        if len(calls) == 2:
                            raise OSError("injected filesystem failure")
                    return rename(path, target)

                with patch.object(Path, "rename", fail):
                    with self.assertRaisesRegex(InfraFailure, "injected filesystem"):
                        copy_files(source, destination, protect=True)
                self.assertEqual((destination / "a").read_text(),
                                 "original uncommitted work")
                self.assertEqual((destination / "a").stat().st_mode & 0o777, 0o640)
                self.assertEqual((destination / "locked/keep").read_text(),
                                 "nested original")
                self.assertEqual((destination / "link").readlink(), Path("a"))
                self.assertEqual((destination / ".git").read_text(), "host metadata")
                self.assertEqual((destination / ".env").read_text(),
                                 "host environment")
                self.assertEqual({p.name for p in destination.iterdir()},
                                 {"a", "locked", "link", ".git", ".env"})
                self.assertEqual(set(self.root.iterdir()), {source, destination})

    def test_failed_copy_back_rollback_retains_original_backup(self):
        from holophyte.gates import InfraFailure
        from holophyte.isolation_clone import copy_files

        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()
        destination.mkdir()
        (source / "file").write_text("replacement")
        (destination / "file").write_text("valuable original")
        rename = Path.rename

        def fail(path, target):
            if Path(target).parent == destination:
                raise OSError("destination unavailable")
            return rename(path, target)

        with patch.object(Path, "rename", fail):
            with self.assertRaisesRegex(
                InfraFailure, "originals retained at"
            ) as raised:
                copy_files(source, destination, protect=False)
        backups = list(self.root.glob(".backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), str(raised.exception))
        self.assertEqual((backups[0] / "file").read_text(), "valuable original")

    def test_clone_rewritten_history_is_infrastructure_failure(self):
        from holophyte import isolation
        from holophyte.gates import InfraFailure
        from holophyte.isolation_git import git

        _, worktree = self.make_worktree()
        before = git(worktree, "rev-parse", "HEAD")

        def run(argv, cwd, timeout, *, env):
            mount = Path(argv[argv.index("--volume") + 1].split(":")[0])
            subprocess.run(["git", "commit", "--amend", "--allow-empty", "-qm",
                            "rewritten"], cwd=mount, env=env, check=True)
            return 0, "done"

        with patch.object(isolation, "image_ready"), \
             patch.object(isolation.review_runner, "_remove_container"), \
             patch.object(isolation, "run_capped", side_effect=run):
            with self.assertRaisesRegex(InfraFailure, "fast-forward"):
                isolation.launch(isolation.Route("container"), worktree, {}, ["agent"])
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)

    def test_clone_refuses_environment_history_and_excludes_dirty_environment(self):
        from holophyte import isolation
        from holophyte.gates import InfraFailure
        from holophyte.isolation_git import git

        main, worktree = self.make_worktree()
        git(main, "branch", "-M", "main")
        self.target.path = main
        self.table["worktree"] = {"env_source": "local.env"}
        (worktree / ".env").write_text("host secret")
        before = git(worktree, "rev-parse", "HEAD")

        def run(argv, cwd, timeout, *, env):
            clone = Path(cwd)
            self.assertFalse((clone / ".env").exists())
            (clone / ".env").write_text("container value")
            if commit_environment:
                subprocess.run(["sh", "-ec", "git add -f .env; git commit -qm env; "
                                "git rm .env; git commit -qm removed"],
                               cwd=cwd, env=env, check=True, capture_output=True)
            return 0, "done"

        with patch.object(isolation, "image_ready"), \
             patch.object(isolation.review_runner, "_remove_container"), \
             patch.object(isolation, "run_capped", side_effect=run):
            commit_environment = False
            isolation.launch(isolation.Route("container"), worktree, {}, ["agent"],
                             target=self.target)
            commit_environment = True
            with self.assertRaisesRegex(InfraFailure, "contains .env"):
                isolation.launch(isolation.Route("container"), worktree, {}, ["agent"],
                                 target=self.target)
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual((worktree / ".env").read_text(), "host secret")

    def test_signal_removes_clone_and_preserves_worktree(self):
        import signal

        from holophyte import isolation
        from holophyte.isolation_git import git

        _, worktree = self.make_worktree()
        before = git(worktree, "rev-parse", "HEAD")
        mounts = []

        def run(argv, cwd, timeout, *, env):
            mounts.append(Path(cwd))
            signal.raise_signal(signal.SIGTERM)

        with patch.object(isolation, "image_ready"), \
             patch.object(isolation.review_runner, "_remove_container"), \
             patch.object(isolation, "run_capped", side_effect=run):
            with self.assertRaises(SystemExit):
                isolation.launch(isolation.Route("container"), worktree, {}, ["agent"])
        self.assertFalse(mounts[0].exists())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertTrue((worktree / ".git").is_file())

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

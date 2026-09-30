"""Implementer boundaries exercised at the process launch seam."""

import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from holophyte.agents import agents


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
        from holophyte.config.project import state_dir

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
        from holophyte.isolation import launcher

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
            patch.object(launcher, "image_ready"),
            patch.object(launcher.review_runner, "_remove_container"),
            patch.object(launcher, "run_capped", return_value=(0, "done")) as run,
        ):
            agents.agent(self.target, "implement", "task", worktree, timeout=17)
        argv = run.call_args.args[0]
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
        self.assertEqual(len(mounts), 3)
        self.assertTrue(mounts[0].endswith("/clone:/workspace:rw"))
        self.assertTrue(mounts[1].endswith(":/home/implementer/.claude:rw"))
        self.assertTrue(mounts[2].endswith("/cache:/home/implementer/.cache:rw"))
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

    def test_container_environment_drops_dotenv_quotes(self):
        from holophyte.isolation import launcher

        source = self.root / "allowed.env"
        source.write_text('A="double"\n')
        self.table["worktree"] = {"env_source": str(source), "env_allow": ["A"]}
        self.table["agents"]["implementer_isolation"] = "container"
        self.assertEqual(launcher.environment(self.target), {"A": "double"})

    def test_file_credential_and_timeout_cleanup(self):
        from holophyte.config.project import state_dir
        from holophyte.isolation import launcher

        main, worktree = self.make_worktree()
        credential = self.root / "auth.json"
        credential.write_text("private")
        route = launcher.Route(
            "container",
            credential={
                "file": str(credential),
                "destination": "/home/implementer/.agent/auth.json",
            },
        )
        with (
            patch.object(launcher, "image_ready"),
            patch.object(launcher.review_runner, "_remove_container") as remove,
            patch.object(
                launcher,
                "run_capped",
                side_effect=subprocess.TimeoutExpired("docker", 1),
            ) as run,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                launcher.launch(route, worktree, {}, ["agent"], timeout=1)
        remove.assert_called_once()
        self.assertFalse(Path(run.call_args.args[1]).exists())
        argv = run.call_args.args[0]
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
        scratch = Path(mounts[2].split(":")[0])
        self.assertEqual(
            mounts,
            [
                f"{run.call_args.args[1]}:/workspace:rw",
                f"{(state_dir(main) / 'cache').resolve()}:/home/implementer/.cache:rw",
                f"{scratch}:/home/implementer/.agent:rw",
            ],
        )
        self.assertTrue(scratch.is_relative_to(state_dir(main).resolve()))
        self.assertFalse(scratch.exists())

    def test_file_credential_launch_outside_a_repository_stages_its_copy(self):
        from holophyte.isolation import launcher

        scratch = self.root / "scratch"
        scratch.mkdir()
        credential = self.root / "auth.json"
        credential.write_text("private")
        route = launcher.Route("container", writable=False, credential={
            "file": str(credential),
            "destination": "/home/implementer/.agent/auth.json"})
        seen = {}

        def run(argv, cwd, timeout, env):
            source = next(flag.split(":")[0] for flag in argv
                          if flag.endswith(":/home/implementer/.agent:rw"))
            seen["copy"] = Path(source) / "auth.json"
            return 0, seen["copy"].read_text()

        with (
            patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.root)}),
            patch.object(launcher, "image_ready"),
            patch.object(launcher.review_runner, "_remove_container"),
            patch.object(launcher, "run_capped", side_effect=run),
        ):
            self.assertEqual(
                launcher.launch(route, scratch, {}, ["agent"]), (0, "private"))
        self.assertFalse(seen["copy"].exists())

    def test_file_credential_beside_a_mounted_directory_mounts_its_copy(self):
        from holophyte.isolation import launcher

        _, worktree = self.make_worktree()
        credential = self.root / "netrc"
        credential.write_text("private")
        route = launcher.Route("container", credential={
            "file": str(credential), "destination": "/home/implementer/.netrc"})
        with launcher.credential_copy(route, worktree, None) as scratch:
            command, _ = launcher.container_command(
                route, worktree, {}, ["true"], "n", credential_scratch=scratch)
            self.assertEqual((scratch / ".netrc").read_text(), "private")
            self.assertEqual((scratch / ".netrc").stat().st_mode & 0o777, 0o600)
        self.assertIn(f"{scratch}/.netrc:/home/implementer/.netrc:rw", command)
        self.assertFalse(scratch.exists())

    def test_file_credential_copy_is_removed_after_the_turn_locks_its_directory(self):
        from holophyte.isolation import launcher

        _, worktree = self.make_worktree()
        credential = self.root / "auth.json"
        credential.write_text("private")
        route = launcher.Route("container", credential={
            "file": str(credential),
            "destination": "/home/implementer/.agent/auth.json"})
        with launcher.credential_copy(route, worktree, None) as scratch:
            (scratch / "sessions").mkdir()
            (scratch / "sessions" / "log").write_text("state")
            (scratch / "sessions").chmod(0o500)
            scratch.chmod(0o500)
        self.assertFalse(scratch.exists())

    def test_file_credential_copy_left_behind_is_reported(self):
        from holophyte.isolation import launcher

        _, worktree = self.make_worktree()
        credential = self.root / "auth.json"
        credential.write_text("private")
        route = launcher.Route("container", credential={
            "file": str(credential),
            "destination": "/home/implementer/.agent/auth.json"})
        with (
            patch.object(launcher.shutil, "rmtree",
                         side_effect=PermissionError("denied")),
            self.assertRaisesRegex(RuntimeError, "credential copy remains") as caught,
        ):
            with launcher.credential_copy(route, worktree, None) as scratch:
                pass
        self.assertIn(str(scratch), str(caught.exception))
        self.assertTrue((scratch / "auth.json").exists())

    def test_file_credential_below_a_persistent_mount_mounts_only_its_copy(self):
        from holophyte.isolation import launcher

        _, worktree = self.make_worktree()
        credential = self.root / "auth.json"
        credential.write_text("private")
        for nested in (".claude/projects/example", ".cache/go/auth"):
            destination = f"/home/implementer/{nested}/auth.json"
            route = launcher.Route("container", credential={
                "file": str(credential), "destination": destination})
            with launcher.credential_copy(route, worktree, None) as scratch:
                command, _ = launcher.container_command(
                    route, worktree, {}, ["true"], "n", task=worktree,
                    cache_for=worktree, credential_scratch=scratch)
            mounts = [command[i + 1] for i, part in enumerate(command)
                      if part == "--volume"]
            self.assertIn(f"{scratch}/auth.json:{destination}:rw", mounts)
            self.assertFalse(any(mount.startswith(f"{scratch}:") for mount in mounts))
            self.assertFalse(any(mount.endswith(f"/{nested}:rw") for mount in mounts))

    def make_worktree(self):
        from holophyte.isolation.isolation_git import git

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
        from holophyte.isolation.isolation_git import git, isolated_git

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
        from holophyte.isolation import isolation_git

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
        from holophyte.isolation import isolation_git

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
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.claim import reuse_leftover

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
            patch.object(launcher, "image_ready"),
            patch.object(launcher.review_runner, "_remove_container"),
            patch.object(launcher, "run_capped", side_effect=resolve),
        ):
            launcher.launch(launcher.Route("container"), worktree, {}, ["agent"])
        self.assertEqual(git(worktree, "log", "-1", "--format=%P").split(), parents)
        git(worktree, "merge-base", "--is-ancestor", "main", "HEAD")
        self.assertEqual(git(worktree, "status", "--porcelain"), "")
        self.assertFalse(Path(git(worktree, "rev-parse", "--path-format=absolute",
                                  "--git-path", "MERGE_HEAD")).exists())

    def test_inconclusive_turn_preserves_pending_merge_and_staging(self):
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

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
            patch.object(launcher, "image_ready"),
            patch.object(launcher.review_runner, "_remove_container"),
            patch.object(launcher, "run_capped") as run,
        ):
            for code in (0, 1):
                with self.subTest(code=code):
                    run.return_value = (code, "inconclusive")
                    launcher.launch(
                        launcher.Route("container"), worktree, {}, ["agent"]
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

        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.gates import InfraFailure

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
            patch.object(launcher, "image_ready"),
            patch.object(launcher.review_runner, "_remove_container"),
            patch.object(launcher, "run_capped", side_effect=run),
        ):
            for kind in ("fifo", "socket", "nested git"):
                with self.subTest(kind=kind):
                    with self.assertRaisesRegex(InfraFailure, "working files"):
                        launcher.launch(
                            launcher.Route("container"), worktree, {}, ["agent"]
                        )
                    self.assertEqual((worktree / "keep").read_text(),
                                     "valuable uncommitted work")
                    self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
                    self.assertFalse((worktree / "nested").exists())

    def carry_worktree(self, carry):
        from holophyte.isolation.isolation_git import git

        main, worktree = self.make_worktree()
        (worktree / ".gitignore").write_text("deps/\nnode_modules/\nmissing/\n")
        (worktree / "tracked").mkdir()
        (worktree / "tracked" / "file").write_text("tracked\n")
        git(worktree, "add", ".")
        git(worktree, "commit", "-qm", "ignore installs")
        self.target.path = main
        self.table["worktree"] = {"carry": carry}
        return worktree

    def recorded_volumes(self, launch, turn=lambda clone: None):
        from holophyte.isolation import launcher

        volumes = []

        def run(argv, cwd, timeout, *, env):
            volumes.extend(argv[i + 1] for i, flag in enumerate(argv)
                           if flag == "--volume")
            turn(Path(cwd))
            return 0, "done"

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container"), \
             patch.object(launcher, "run_capped", side_effect=run):
            return launch(), volumes

    def test_nested_carry_is_mounted_and_survives_the_copy_back(self):
        from holophyte.isolation import launcher

        worktree = self.carry_worktree(["tracked/node_modules"])
        installed = worktree / "tracked" / "node_modules" / "dep"
        installed.mkdir(parents=True)
        (installed / "index.js").write_text("installed\n")

        def turn(clone):
            self.assertEqual(list((clone / "tracked/node_modules").iterdir()), [])
            (clone / "tracked" / "file").write_text("edited\n")

        _, volumes = self.recorded_volumes(lambda: launcher.launch(
            launcher.Route("container"), worktree, {}, ["agent"],
            project=self.target), turn)
        source = worktree.resolve() / "tracked" / "node_modules"
        self.assertIn(f"{source}:/workspace/tracked/node_modules:rw", volumes)
        self.assertEqual((installed / "index.js").read_text(), "installed\n")
        self.assertEqual((worktree / "tracked" / "file").read_text(), "edited\n")

    def test_tracked_or_escaping_carry_fails_the_launch_naming_it(self):
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.gates import InfraFailure

        worktree = self.carry_worktree([])
        outside = self.root / "outside"
        outside.mkdir()
        (worktree / "link").symlink_to(outside)
        (worktree / "tracked-link").symlink_to(worktree / "tracked")
        git(worktree, "add", "tracked-link")
        git(worktree, "commit", "-qm", "tracked link")
        (worktree / "deps").symlink_to(outside)
        for entry, reason in (("tracked", "tracked"), ("../outside/deps", "escapes"),
                              ("link/deps", "escapes"), ("tracked-link", "tracked"),
                              ("deps", "leaves the repository")):
            with self.subTest(entry=entry):
                run = Mock(return_value=(0, "done"))
                with patch.object(launcher, "image_ready"), \
                     patch.object(launcher.review_runner, "_remove_container"), \
                     patch.object(launcher, "run_capped", run):
                    with self.assertRaisesRegex(InfraFailure, reason) as raised:
                        launcher.launch(launcher.Route("container"), worktree, {},
                                         ["agent"], carry=[entry])
                self.assertIn(repr(entry), str(raised.exception))
                run.assert_not_called()
                self.assertEqual(list(outside.iterdir()), [])

    def test_copy_back_refuses_a_linked_carry_parent_and_keeps_the_install(self):
        import shutil

        from holophyte.isolation import launcher
        from holophyte.loop.gates import InfraFailure

        worktree = self.carry_worktree(["tracked/node_modules"])
        installed = worktree / "tracked" / "node_modules" / "dep"
        installed.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()

        def run(argv, cwd, timeout, *, env):
            shutil.rmtree(Path(cwd) / "tracked")
            (Path(cwd) / "tracked").symlink_to(outside)
            return 0, "done"

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container"), \
             patch.object(launcher, "run_capped", side_effect=run):
            with self.assertRaisesRegex(InfraFailure, "'tracked/node_modules'"):
                launcher.launch(launcher.Route("container"), worktree, {}, ["agent"],
                                 project=self.target)
        self.assertTrue(installed.is_dir())
        self.assertEqual(list(outside.iterdir()), [])

    def test_carry_linked_to_another_worktree_mounts_that_worktrees_directory(self):
        from holophyte.isolation import launcher

        worktree = self.carry_worktree(["deps"])
        (worktree / "deps").mkdir()
        main = self.target.path
        (main / "deps").symlink_to(worktree / "deps")
        _, volumes = self.recorded_volumes(lambda: launcher.launch(
            launcher.Route("container"), main, {}, ["agent"], carry=["deps"]))
        self.assertIn(f"{(worktree / 'deps').resolve()}:/workspace/deps:rw", volumes)
        self.assertTrue((main / "deps").is_symlink())

    def test_capture_launch_mounts_the_carry_instead_of_copying_it(self):
        from holophyte.pr import pr_media

        worktree = self.carry_worktree(["deps"])
        self.table["agents"]["implementer_isolation"] = "container"
        (worktree / "deps").mkdir()
        os.mkfifo(worktree / "deps" / "pipe")
        runner = self.root / "factory" / "holophyte" / "capture_playwright.py"
        runner.parent.mkdir(parents=True)
        runner.write_text("")
        with patch.object(pr_media.review_runner, "ROOT", runner.parent.parent):
            failure, volumes = self.recorded_volumes(lambda: pr_media._capture(
                "capture", worktree, worktree.resolve() / "shot.png", "HOLO-1", [],
                project=self.target))
        self.assertEqual(failure, "")
        self.assertIn(f"{worktree.resolve() / 'deps'}:/workspace/deps:rw", volumes)

    def test_clone_turn_returns_commit_and_dirty_file_safely(self):
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

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
            subprocess.run(argv[argv.index(launcher.Route().image) + 1:],
                           cwd=mount, env=env,
                           check=True, capture_output=True)
            hook = mount / ".git/hooks/post-checkout"
            hook.parent.mkdir(exist_ok=True)
            hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
            hook.chmod(0o755)
            git(mount, "config", "alias.container-only", "status")
            git(mount, "config", "uploadpack.packObjectsHook", f"touch {sentinel}")
            return 0, "done"

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container"), \
             patch.object(launcher, "run_capped", side_effect=run):
            launcher.launch(launcher.Route("container"), worktree, {},
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

    def test_clone_turn_commit_leaves_local_capture_spec_out(self):
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.claim import run_worktree_setup

        main, worktree = self.make_worktree()
        config = {"merge": {"ui_capture_dir": ".holophyte-capture",
                            "ui_capture_local": True}}
        target = SimpleNamespace(path=main, config=lambda: config,
                                 config_path=self.root / "config.toml")
        self.assertTrue(run_worktree_setup(target, worktree)[0])
        script = ("echo spec > .holophyte-capture/KO-7.capture.ts; "
                  "echo work > work.txt; git add -A; git commit -qm candidate")

        def run(argv, cwd, timeout, *, env):
            subprocess.run(argv[argv.index(launcher.Route().image) + 1:],
                           cwd=cwd, env=env, check=True, capture_output=True)
            return 0, "done"

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container"), \
             patch.object(launcher, "run_capped", side_effect=run):
            launcher.launch(launcher.Route("container"), worktree, {},
                             ["sh", "-ec", script])
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "candidate")
        tree = git(worktree, "ls-tree", "-r", "--name-only", "HEAD")
        self.assertIn("work.txt", tree.split())
        self.assertNotIn(".holophyte-capture", tree)
        self.assertEqual(
            (worktree / ".holophyte-capture/KO-7.capture.ts").read_text(), "spec\n")

    def test_real_timeout_returns_committed_and_dirty_clone_work(self):
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.gates import run_capped

        _, worktree = self.make_worktree()
        mounts = []
        script = ("echo committed > file; git add file; "
                  "git commit -qm 'before timeout'; "
                  "echo staged > staged; git add staged; "
                  "echo leftover > dirty; echo ready; sleep 30")

        def run(argv, cwd, timeout, *, env):
            mounts.append(Path(cwd))
            return run_capped(argv[argv.index(launcher.Route().image) + 1:],
                              cwd, timeout, env=env)

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container") as remove, \
             patch.object(launcher, "run_capped", side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                launcher.launch(launcher.Route("container"), worktree, {},
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
        from holophyte.isolation.isolation_clone import copy_files
        from holophyte.loop.gates import InfraFailure

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
        from holophyte.isolation.isolation_clone import copy_files
        from holophyte.loop.gates import InfraFailure

        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()
        destination.mkdir()
        (source / "file").write_text("replacement")
        (destination / "file").write_text("valuable original")
        (destination / "deps").mkdir()
        (destination / "deps" / "installed").write_text("package")
        rename = Path.rename

        def fail(path, target):
            if Path(target).parent == destination:
                raise OSError("destination unavailable")
            return rename(path, target)

        with patch.object(Path, "rename", fail):
            with self.assertRaisesRegex(
                InfraFailure, "originals retained at"
            ) as raised:
                copy_files(source, destination, protect=False, carry=["deps"])
        backups = list(destination.glob(".backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertIn(str(backups[0]), str(raised.exception))
        self.assertEqual((backups[0] / "file").read_text(), "valuable original")
        kept = list(destination.glob(".copy-*"))
        self.assertEqual(len(kept), 1)
        self.assertIn(str(kept[0]), str(raised.exception))
        self.assertEqual((kept[0] / "deps" / "installed").read_text(), "package")

    def test_clone_rewritten_history_is_infrastructure_failure(self):
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.gates import InfraFailure

        _, worktree = self.make_worktree()
        before = git(worktree, "rev-parse", "HEAD")

        def run(argv, cwd, timeout, *, env):
            mount = Path(argv[argv.index("--volume") + 1].split(":")[0])
            subprocess.run(["git", "commit", "--amend", "--allow-empty", "-qm",
                            "rewritten"], cwd=mount, env=env, check=True)
            return 0, "done"

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container"), \
             patch.object(launcher, "run_capped", side_effect=run):
            with self.assertRaisesRegex(InfraFailure, "fast-forward"):
                launcher.launch(launcher.Route("container"), worktree, {}, ["agent"])
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)

    def test_clone_refuses_environment_history_and_excludes_dirty_environment(self):
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.gates import InfraFailure

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

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container"), \
             patch.object(launcher, "run_capped", side_effect=run):
            commit_environment = False
            launcher.launch(launcher.Route("container"), worktree, {}, ["agent"],
                             project=self.target)
            commit_environment = True
            with self.assertRaisesRegex(InfraFailure, "contains .env"):
                launcher.launch(launcher.Route("container"), worktree, {}, ["agent"],
                                 project=self.target)
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual((worktree / ".env").read_text(), "host secret")

    def test_signal_removes_clone_and_preserves_worktree(self):
        import signal

        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

        _, worktree = self.make_worktree()
        before = git(worktree, "rev-parse", "HEAD")
        mounts = []

        def run(argv, cwd, timeout, *, env):
            mounts.append(Path(cwd))
            signal.raise_signal(signal.SIGTERM)

        with patch.object(launcher, "image_ready"), \
             patch.object(launcher.review_runner, "_remove_container"), \
             patch.object(launcher, "run_capped", side_effect=run):
            with self.assertRaises(SystemExit):
                launcher.launch(launcher.Route("container"), worktree, {}, ["agent"])
        self.assertFalse(mounts[0].exists())
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), before)
        self.assertTrue((worktree / ".git").is_file())

    def test_copy_back_needs_only_writable_destination(self):
        from holophyte.isolation.isolation_clone import copy_files

        source = self.root / "source"
        parent = self.root / "readonly"
        destination = parent / "worktree"
        source.mkdir()
        destination.mkdir(parents=True)
        (source / "file").write_text("returned work")
        (destination / "old").write_text("previous work")
        parent.chmod(0o555)
        try:
            copy_files(source, destination, protect=False)
        finally:
            parent.chmod(0o755)
        self.assertEqual((destination / "file").read_text(), "returned work")
        self.assertEqual({p.name for p in destination.iterdir()}, {"file"})

    def test_racing_branch_update_preserves_worktree_and_index(self):
        from holophyte.isolation import isolation_clone
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.gates import InfraFailure

        _, worktree = self.make_worktree()
        with self.assertRaisesRegex(InfraFailure, "changed|lock|fast-forward"):
            with isolation_clone.turn_clone(worktree) as (clone, env):
                (clone / "file").write_text("container work")
                subprocess.run(["git", "add", "file"], cwd=clone,
                               env={**os.environ, **env}, check=True)
                subprocess.run(["git", "commit", "-qm", "container"], cwd=clone,
                               env={**os.environ, **env}, check=True)
                real_git = isolation_clone.git

                def race(cwd, *args, **kwargs):
                    result = real_git(cwd, *args, **kwargs)
                    if args == ("rev-parse", "HEAD"):
                        (worktree / "file").write_text("concurrent work")
                        git(worktree, "add", "file")
                        git(worktree, "commit", "-qm", "concurrent")
                        (worktree / "staged").write_text("valuable staged work")
                        git(worktree, "add", "staged")
                        snapshot.append((git(worktree, "rev-parse", "HEAD"),
                                         git(worktree, "ls-files", "--stage")))
                    return result

                snapshot = []
                patcher = patch.object(isolation_clone, "git", side_effect=race)
                patcher.start()
                self.addCleanup(patcher.stop)
        patcher.stop()
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), snapshot[0][0])
        self.assertEqual(git(worktree, "ls-files", "--stage"), snapshot[0][1])
        self.assertEqual((worktree / "file").read_text(), "concurrent work")
        self.assertEqual((worktree / "staged").read_text(), "valuable staged work")

    def test_return_locks_refs_and_index_during_file_replacement(self):
        from holophyte.isolation import isolation_clone
        from holophyte.isolation.isolation_git import git

        _, worktree = self.make_worktree()
        old = git(worktree, "rev-parse", "HEAD")
        replace = isolation_clone.replace_files
        attempts = []

        def contend(staged, destination, excluded, finish):
            for args in (("update-ref", "HEAD", old), ("reset", "--hard", old)):
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    git(worktree, *args)
                self.assertIn("lock", raised.exception.stderr)
                attempts.append(args)
            replace(staged, destination, excluded, finish)

        with isolation_clone.turn_clone(worktree) as (clone, env):
            (clone / "file").write_text("returned work")
            git(clone, "add", "file")
            subprocess.run(["git", "commit", "-qm", "returned"], cwd=clone,
                           env={**os.environ, **env}, check=True)
            patcher = patch.object(isolation_clone, "replace_files", contend)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher.stop()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(git(worktree, "log", "-1", "--format=%s"), "returned")
        self.assertEqual(git(worktree, "status", "--porcelain"), "")
        git(worktree, "reset", "--hard", old)  # Locks were released.

    def test_failed_ref_commit_restores_working_files_and_index(self):
        from holophyte.isolation import isolation_clone, isolation_return
        from holophyte.isolation.isolation_git import git
        from holophyte.loop.gates import InfraFailure

        _, worktree = self.make_worktree()
        (worktree / "keep").write_text("valuable staged work")
        git(worktree, "add", "keep")
        old = git(worktree, "rev-parse", "HEAD")
        index = git(worktree, "ls-files", "--stage")
        command = isolation_return.transaction_command

        def fail(process, action):
            if action == "commit":
                command(process, "abort")
                raise InfraFailure("injected ref commit failure")
            command(process, action)

        with self.assertRaisesRegex(InfraFailure, "injected ref commit failure"):
            with isolation_clone.turn_clone(worktree) as (clone, env):
                (clone / "keep").write_text("container version")
                git(clone, "add", "keep")
                subprocess.run(["git", "commit", "-qm", "container"], cwd=clone,
                               env={**os.environ, **env}, check=True)
                patcher = patch.object(isolation_return, "transaction_command", fail)
                patcher.start()
                self.addCleanup(patcher.stop)
        patcher.stop()
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), old)
        self.assertEqual(git(worktree, "ls-files", "--stage"), index)
        self.assertEqual((worktree / "keep").read_text(), "valuable staged work")
        self.assertEqual({p.name for p in worktree.iterdir()}, {".git", "keep"})
        git(worktree, "commit", "-qm", "locks released")

    def test_turn_clone_keeps_identity_in_clone_config_not_environment(self):
        from holophyte.isolation import isolation_clone

        _, worktree = self.make_worktree()
        with isolation_clone.turn_clone(worktree) as (clone, env):
            self.assertEqual(env, {"GIT_CONFIG_COUNT": "1",
                                   "GIT_CONFIG_KEY_0": "safe.directory",
                                   "GIT_CONFIG_VALUE_0": "/workspace"})
            local = subprocess.run(
                ["git", "config", "--local", "--get-regexp", "^user\\."],
                cwd=clone, capture_output=True, text=True, check=True).stdout
        self.assertEqual(local.splitlines(), ["user.name Configured Author",
                                              "user.email author@example.test"])

    def test_host_git_ignores_inherited_repository_locations(self):
        from holophyte.isolation.isolation_git import git

        main, worktree = self.make_worktree()
        (worktree / "file").write_text("task content")
        for variables in (
            {"GIT_DIR": str(main / ".git"), "GIT_WORK_TREE": str(main)},
            {"GIT_COMMON_DIR": str(self.root / "missing")},
            {"GIT_INDEX_FILE": str(self.root / "foreign-index")},
            {"GIT_OBJECT_DIRECTORY": str(self.root / "missing")},
        ):
            with self.subTest(variables=variables), patch.dict(os.environ, variables):
                self.assertEqual(git(worktree, "branch", "--show-current"), "task")
                git(worktree, "add", "file")
                self.assertIn("file", git(worktree, "ls-files"))
            self.assertIn("file", git(worktree, "ls-files"))
            git(worktree, "reset", "--mixed", "HEAD")
        self.assertFalse((self.root / "foreign-index").exists())
        self.assertEqual(git(main, "status", "--porcelain"), "")

    def test_config_rejects_invalid_boundaries(self):
        from holophyte.isolation.launcher import route_for

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

    def test_file_mount_refuses_workspace_and_home_destinations(self):
        from holophyte.isolation import launcher

        _, worktree = self.make_worktree()
        for path in ("/workspace/capture.py", "/home/implementer/capture.py"):
            with self.subTest(path=path), self.assertRaisesRegex(
                RuntimeError, "workspace or home"
            ):
                launcher.container_command(
                    launcher.Route("container"), worktree, {}, ["true"], "n", [path]
                )

    def test_read_only_launch_creates_no_session_directory(self):
        from holophyte.config.project import state_dir
        from holophyte.isolation import launcher

        main, worktree = self.make_worktree()
        self.target.path = main
        home = self.root / "holophyte-home"
        with (
            patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)}),
            patch.object(launcher, "image_ready"),
            patch.object(launcher.review_runner, "_remove_container"),
            patch.object(launcher, "run_capped", return_value=(0, "done")) as run,
        ):
            launcher.launch(launcher.Route("container", writable=False),
                             worktree, {}, ["agent"], project=self.target,
                             keep_session=True)
            self.assertFalse((state_dir(main) / "sessions").exists())
        self.assertNotIn(".claude", str(run.call_args.args[0]))

    def test_launch_outside_a_repository_mounts_no_cache(self):
        from holophyte.isolation import launcher

        scratch = self.root / "scratch"
        scratch.mkdir()
        home = self.root / "holophyte-home"
        with (
            patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home),
                                    "GIT_CEILING_DIRECTORIES": str(self.root)}),
            patch.object(launcher, "image_ready"),
            patch.object(launcher.review_runner, "_remove_container"),
            patch.object(launcher, "run_capped", return_value=(0, "done")) as run,
        ):
            launcher.launch(launcher.Route("container", writable=False),
                             scratch, {}, ["agent"])
        argv = run.call_args.args[0]
        self.assertNotIn(".cache", str(argv))
        self.assertNotIn("GOCACHE", run.call_args.kwargs["env"])
        self.assertFalse(home.exists())

    def test_launch_without_cache_points_tmpdir_at_container_tmp(self):
        from holophyte.isolation import launcher

        scratch = self.root / "scratch"
        scratch.mkdir()
        command, host_env = launcher.container_command(
            launcher.Route("container"), scratch, {}, ["true"], "n"
        )
        self.assertIn("--env=TMPDIR", command)
        self.assertEqual(host_env["TMPDIR"], "/tmp")

    def test_container_tmp_allows_running_programs(self):
        from holophyte.isolation import launcher

        scratch = self.root / "scratch"
        scratch.mkdir()
        command, _ = launcher.container_command(
            launcher.Route("container"), scratch, {}, ["true"], "n"
        )
        mount = next(flag for flag in command if flag.startswith("/tmp:"))
        options = mount.split(":", 1)[1].split(",")
        for option in ("exec", "nosuid", "nodev", "size=1g"):
            self.assertIn(option, options)
        self.assertNotIn("noexec", options)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_launch_creates_a_temporary_directory(self):
        import shutil
        import uuid

        from holophyte.isolation import launcher

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        scratch = self.root / "scratch"
        scratch.mkdir()
        route = launcher.Route("container")
        launcher.image_ready(route)
        name = "holophyte-test-" + uuid.uuid4().hex
        command, host_env = launcher.container_command(
            route, scratch, {}, ["sh", "-c", "mktemp -d && echo ok > probe.txt"],
            name,
        )
        try:
            code, output = launcher.run_capped(command, scratch, 120, env=host_env)
        finally:
            launcher.review_runner._remove_container(name, env={"PATH": os.defpath})
        self.assertEqual(code, 0, output)
        self.assertEqual((scratch / "probe.txt").read_text(), "ok\n")

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_launch_runs_a_script_from_tmpdir(self):
        import shutil
        import uuid

        from holophyte.isolation import launcher

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        scratch = self.root / "scratch"
        scratch.mkdir()
        route = launcher.Route("container")
        launcher.image_ready(route)
        name = "holophyte-test-" + uuid.uuid4().hex
        script = (
            'probe="$TMPDIR/probe.sh"; '
            "printf '#!/bin/sh\\necho ran-from-tmpdir\\n' > \"$probe\"; "
            'chmod +x "$probe" && "$probe" && grep " /tmp " /proc/mounts'
        )
        command, host_env = launcher.container_command(
            route, scratch, {}, ["sh", "-c", script], name,
        )
        try:
            code, output = launcher.run_capped(command, scratch, 120, env=host_env)
        finally:
            launcher.review_runner._remove_container(name, env={"PATH": os.defpath})
        self.assertEqual(code, 0, output)
        self.assertIn("ran-from-tmpdir", output)
        entry = next(line for line in output.splitlines() if " /tmp " in line)
        options = entry.split()[3].split(",")
        self.assertIn("nosuid", options)
        self.assertIn("nodev", options)
        self.assertNotIn("noexec", options)

    def test_relative_state_home_mounts_an_absolute_session_directory(self):
        from holophyte.isolation import launcher

        main, worktree = self.make_worktree()
        self.target.path = main
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": "relative-home"}):
            command, _ = launcher.container_command(
                launcher.Route("container"), worktree, {}, ["true"], "n",
                task=worktree, project=self.target,
            )
        source = next(flag.split(":")[0] for flag in command
                      if flag.endswith(":/home/implementer/.claude:rw"))
        self.assertTrue(Path(source).is_absolute(), source)
        self.assertTrue(Path(source).is_dir())
        self.assertTrue(
            Path(source).is_relative_to((self.root / "relative-home").resolve())
        )

    def fake_codex_release(self, names=("codex", "codex-code-mode-host")):
        release = self.root / "release"
        release.mkdir()
        for name in names:
            (release / name).write_text("#!/bin/sh\n")
            (release / name).chmod(0o755)
        return release

    def test_claude_implementer_mounts_no_codex_file(self):
        from holophyte.isolation import launcher

        _, worktree = self.make_worktree()
        release = self.fake_codex_release()
        for implementer in ("claude -p", {"harness": "claude"}):
            with self.subTest(implementer=implementer):
                self.table["agents"] = {"implementer_isolation": "container",
                                        "implementer": implementer}
                with patch.dict(os.environ, {"PATH": str(release)}):
                    command, _ = launcher.container_command(
                        launcher.route_for(self.target), worktree, {},
                        ["claude", "-p"], "n")
                self.assertNotIn("codex", " ".join(command))
                self.assertEqual(command[-2:], ["claude", "-p"])

    def test_codex_fallback_mounts_the_host_release_read_only(self):
        from holophyte.isolation import launcher

        _, worktree = self.make_worktree()
        release = self.fake_codex_release()
        self.table["agents"] = {"implementer_isolation": "container",
                                "implementer": {"harness": "claude"},
                                "implementer_fallback": "codex exec -m model"}
        with patch.dict(os.environ, {"PATH": str(release)}):
            command, _ = launcher.container_command(
                launcher.route_for(self.target), worktree, {}, ["codex"], "n")
        mounts = [command[i + 1] for i, part in enumerate(command)
                  if part == "--volume"]
        self.assertIn(f"{release}/codex:/opt/codex/bin/codex:ro", mounts)
        self.assertIn(f"{release}/codex-code-mode-host:"
                      "/opt/codex/bin/codex-code-mode-host:ro", mounts)
        (release / "codex-code-mode-host").unlink()
        with (patch.dict(os.environ, {"PATH": str(release)}),
              self.assertRaisesRegex(RuntimeError, "codex-code-mode-host")):
            launcher.container_command(
                launcher.route_for(self.target), worktree, {}, ["codex"], "n")

    def test_quoted_codex_program_is_a_codex_implementer(self):
        from holophyte.isolation.launcher import route_for

        self.table["agents"] = {"implementer_isolation": "container",
                                "implementer": '"codex" exec -m model'}
        self.assertTrue(route_for(self.target).codex)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_codex_implementer_runs_the_host_release_read_only(self):
        import shutil

        from holophyte.isolation import launcher

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        if not shutil.which("codex"):
            self.skipTest("Codex absent from the host PATH")
        _, worktree = self.make_worktree()
        self.table["agents"] = {"implementer_isolation": "container",
                                "implementer": "codex exec -m MODEL"}
        route = replace(launcher.route_for(self.target), writable=False)
        host = subprocess.run(["codex", "--version"], capture_output=True,
                              text=True, check=True).stdout
        code, output = launcher.launch(route, worktree, {}, ["codex", "--version"])
        self.assertEqual((code, output.strip()), (0, host.strip()))
        code, output = launcher.launch(
            route, worktree, {}, ["/bin/sh", "-c", 'touch -c "$(command -v codex)"'])
        self.assertNotEqual(code, 0, output)
        self.assertIn("Read-only file system", output)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_session_files_persist_per_task_worktree(self):
        import shutil

        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        main, worktree = self.make_worktree()
        other = self.root / "other"
        git(main, "worktree", "add", "-qb", "other", str(other))
        before = {path: git(path, "status", "--porcelain")
                  for path in (worktree, other)}
        route = launcher.Route("container")

        def turn(path, script):
            return launcher.launch(route, path, {}, ["/bin/sh", "-ec", script],
                                    keep_session=True)

        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")}):
            code, output = turn(worktree, "mkdir -p ~/.claude; touch ~/.claude/marker")
            self.assertEqual(code, 0, output)
            code, output = turn(worktree, 'ls -A "$HOME/.claude"')
            self.assertEqual((code, output.split()), (0, ["marker"]))
            code, output = turn(other, 'ls -A "$HOME/.claude"')
            self.assertEqual((code, output.split()), (0, []))
        for path in (worktree, other):
            self.assertEqual(git(path, "status", "--porcelain"), before[path])
            self.assertEqual(list(path.rglob("marker")), [])

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_tool_caches_persist_per_project(self):
        import shutil

        from holophyte.config.project import state_dir
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        main, worktree = self.make_worktree()
        other = self.root / "other-project"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        git(other, "config", "user.name", "Configured Author")
        git(other, "config", "user.email", "author@example.test")
        git(other, "commit", "--allow-empty", "-qm", "base")
        build = (
            "mkdir /tmp/module; cd /tmp/module;"
            " printf 'module example.test/m\\n\\ngo 1.26\\n' > go.mod;"
            " printf 'package main\\n\\nfunc main() {}\\n' > main.go;"
            " echo '{}' > package.json; go build -o /dev/null . >&2;"
            " go env GOMODCACHE GOCACHE GOTMPDIR; npm config get cache; bun pm cache"
        )
        count = (
            'cache=$(go env GOCACHE); if [ -e "$cache" ];'
            ' then find "$cache" -type f > /tmp/files; wc -l < /tmp/files;'
            " else echo 0; fi"
        )

        def turn(path, script, writable=True):
            route = launcher.Route("container", writable=writable)
            return launcher.launch(route, path, {}, ["/bin/sh", "-ec", script])

        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")}):
            for writable in (True, False):
                code, output = turn(worktree, build, writable)
                self.assertEqual(code, 0, output)
                paths = output.split()[-5:]
                self.assertEqual(len(paths), 5, output)
                for path in paths:
                    self.assertTrue(path.startswith("/home/implementer/.cache/"),
                                    output)
            self.assertTrue(any((state_dir(main) / "cache").rglob("*")))
            code, output = turn(worktree, count, writable=False)
            self.assertEqual(code, 0, output)
            self.assertGreater(int(output.split()[-1]), 0, output)
            code, output = turn(other, count)
            self.assertEqual((code, int(output.split()[-1])), (0, 0), output)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_implementer_turn_has_no_capture_runner(self):
        import shutil

        import review_runner
        from holophyte.isolation import launcher

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        _, worktree = self.make_worktree()
        runner = review_runner.ROOT / "holophyte" / "capture_playwright.py"
        self.assertTrue(runner.is_file())
        code, output = launcher.launch(
            launcher.Route("container"),
            worktree,
            {},
            ["/bin/sh", "-c", 'test ! -e "$0"', str(runner.resolve())],
        )
        self.assertEqual(code, 0, output)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_capture_modules_pass_with_the_checkout_at_the_workspace(self):
        import shutil
        import uuid

        import review_runner
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        checkout = self.root / "checkout"
        git(self.root, "clone", "-q", str(review_runner.ROOT), str(checkout))
        name = "holophyte-test-" + uuid.uuid4().hex
        script = ('export HOLOPHYTE_HOME="$(mktemp -d)"; status=0; for module; do'
                  ' python3 -m unittest discover -s tests -p "$module" || status=1;'
                  ' done; exit $status')
        command = ["docker", "run", "--rm", "--pull=never", "--name", name,
                   f"--user={os.getuid()}:{os.getgid()}", "--env=HOME=/tmp",
                   f"--volume={checkout}:/workspace:rw", "--workdir=/workspace",
                   launcher.Route().image, "/bin/sh", "-c", script, "sh",
                   "test_isolation.py", "test_pr_media.py"]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=600)
        finally:
            review_runner._remove_container(name, env={"PATH": os.defpath})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_file_credential_is_a_writable_private_copy(self):
        import shutil

        from holophyte.config.project import state_dir
        from holophyte.isolation import launcher

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        main, worktree = self.make_worktree()
        credential = self.root / "auth.json"
        credential.write_text("host-token")
        os.utime(credential, ns=(1_000_000_000_000_000_000,) * 2)
        route = launcher.Route("container", credential={
            "file": str(credential),
            "destination": "/home/implementer/.agent/state/auth.json"})
        script = (
            "cd /home/implementer/.agent/state; echo log > session.log;"
            " echo refreshed > auth.json; stat -c %a auth.json; cat auth.json"
        )
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")}):
            code, output = launcher.launch(route, worktree, {},
                                            ["/bin/sh", "-ec", script])
            state = state_dir(main)
        self.assertEqual((code, output.split()), (0, ["600", "refreshed"]), output)
        self.assertEqual(credential.read_text(), "host-token")
        self.assertEqual(credential.stat().st_mtime_ns, 1_000_000_000_000_000_000)
        self.assertTrue(state.is_dir())
        left = [path for path in state.rglob("*")
                if path.name in ("auth.json", "session.log")]
        self.assertEqual(left, [])

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_file_credential_below_the_session_keeps_its_state(self):
        import shutil

        from holophyte.isolation import launcher

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        _, worktree = self.make_worktree()
        credential = self.root / "auth.json"
        credential.write_text("host-token")
        route = launcher.Route("container", credential={
            "file": str(credential),
            "destination": "/home/implementer/.claude/projects/example/auth.json"})
        script = ("cd /home/implementer/.claude/projects/example;"
                  " cat saved.json auth.json; echo new > next.json")
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")}):
            saved = launcher.session_directory(worktree, None) / "projects/example"
            saved.mkdir(parents=True)
            (saved / "saved.json").write_text("earlier\n")
            code, output = launcher.launch(route, worktree, {},
                                            ["/bin/sh", "-ec", script],
                                            keep_session=True)
        self.assertEqual((code, output.split()), (0, ["earlier", "host-token"]), output)
        self.assertEqual((saved / "next.json").read_text(), "new\n")
        self.assertEqual(credential.read_text(), "host-token")

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_launch_writes_carry_directories_in_the_worktree(self):
        import shutil

        from holophyte.isolation import launcher

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        worktree = self.carry_worktree(["deps", "missing"])
        (worktree / "deps").mkdir()
        (worktree / "deps" / "installed").write_text("package\n")
        os.mkfifo(worktree / "deps" / "pipe")
        script = ('test "$(cat deps/installed)" = package; echo built > deps/built;'
                  " echo new > missing/created")
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")}):
            code, output = launcher.launch(
                launcher.Route("container"), worktree, {},
                ["/bin/sh", "-ec", script], project=self.target)
        self.assertEqual(code, 0, output)
        self.assertEqual((worktree / "deps" / "built").read_text(), "built\n")
        created = worktree / "missing" / "created"
        self.assertEqual(created.read_text(), "new\n")
        for path in (created, created.parent):
            self.assertEqual(path.stat().st_uid, os.getuid(), path)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_container_commit(self):
        import shutil

        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        main, worktree = self.make_worktree()
        credential = self.root / "credential.json"
        credential.write_text("agent-only")
        route = launcher.Route("container", credential={
            "file": str(credential),
            "destination": "/home/implementer/.agent/auth.json"})
        code, output = launcher.launch(
            route,
            worktree,
            {},
            [
                "/bin/sh",
                "-ec",
                'test "$(cat "$0")" = agent-only;'
                " echo content > created; git add created; git commit -qm isolated",
                "/home/implementer/.agent/auth.json",
            ],
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(
            git(worktree, "log", "-1", "--format=%s|%an|%ae"),
            "isolated|Configured Author|author@example.test",
        )
        self.assertEqual((worktree / "created").read_text(), "content\n")
        self.assertEqual(git(main, "log", "-1", "--format=%s"), "base")

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_launch_leaves_other_repositories_their_own_identity(self):
        import shutil

        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        _, worktree = self.make_worktree()
        script = (
            'fixture=$(mktemp -d "$TMPDIR/fixture.XXXXXX"); cd "$fixture";'
            " git init -q; git config user.name 'Fixture Author';"
            " git config user.email fixture@example.test;"
            " git commit --allow-empty -qm fixture;"
            " git log -1 --format=%an/%ae; cd /workspace;"
            " git commit --allow-empty -qm workspace"
        )
        code, output = launcher.launch(
            launcher.Route("container"), worktree, {}, ["/bin/sh", "-ec", script])
        self.assertEqual(code, 0, output)
        self.assertIn("Fixture Author/fixture@example.test", output)
        self.assertEqual(git(worktree, "log", "-1", "--format=%s|%an|%ae"),
                         "workspace|Configured Author|author@example.test")

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_claude_fix_round_resumes_the_session_its_turn_opened(self):
        import shutil
        import uuid

        import store
        from holophyte.agents import fix_session
        from holophyte.config.project import Project

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        context = self.root / "stub"
        context.mkdir()
        (context / "claude").write_text(
            '#!/bin/sh\ncase "$2" in\n'
            '  --session-id) touch "$HOME/.claude/$3" ;;\n'
            '  --resume) test -f "$HOME/.claude/$3" ;;\n'
            "  *) exit 2 ;;\nesac\n"
        )
        (context / "Dockerfile").write_text(
            "FROM ubuntu:24.04\nCOPY --chmod=755 claude /usr/local/bin/claude\n"
        )
        image = f"holophyte-test-claude-stub:{uuid.uuid4().hex[:12]}"
        subprocess.run(["docker", "build", "--pull=false", "-q", "-t", image,
                        str(context)], check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["docker", "image", "rm", "-f", image],
                        capture_output=True)
        main, worktree = self.make_worktree()
        holo = self.root / "holo"
        holo.mkdir()
        (holo / "config.toml").write_text(
            f'[agents]\nimplementer_isolation = "container"\n'
            f'implementer_image = "{image}"\n'
            '[agents.implementer]\nharness = "claude"\n'
        )
        target = Project(path=main, holo_dir=holo, store_path=holo / "store.db",
                         config_path=holo / "config.toml",
                         worktrees=self.root / "worktrees")
        conn = store.open(target.store_path)
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.ensure_project(conn, "test", main)
        ticket = store.mirror_ticket(conn, project, "KO-1", "KO-1", "session",
                                     acceptance_criteria=["resume"],
                                     verification_commands=["true"])
        run = store.claim(conn, project, ticket)

        first = agents.agent(target, "implement", "implement", worktree,
                             conn=conn, run_id=run)
        self.assertEqual(first.exit_code, 0, first)
        argv, reason = fix_session.resume_argv(target, conn, run)
        self.assertIsNone(reason)
        self.assertEqual(argv[:3], ["claude", "-p", "--resume"])
        resumed = agents.agent(target, "implement", "findings", worktree,
                               conn=conn, run_id=run, argv=argv)
        self.assertEqual(resumed.exit_code, 0, resumed)

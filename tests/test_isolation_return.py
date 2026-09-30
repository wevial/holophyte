"""Work an isolated implementer turn made, returned to the host worktree."""

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from isolation_fixture import IsolationCase


class IsolationReturnTests(IsolationCase):
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

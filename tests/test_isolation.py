"""Implementer boundaries exercised at the process launch seam."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from go_race_cases import GoRaceCases
from isolation_fixture import IsolationCase

from holophyte.agents import roles


class IsolationTests(GoRaceCases, IsolationCase):
    def test_none_preserves_process_call(self):
        from holophyte.config.project import state_dir

        for backend in (None, "none"):
            if backend:
                self.table["agents"]["implementer_isolation"] = backend
            with patch.object(roles, "run_capped", return_value=(0, "done")) as run:
                self.assertEqual(
                    roles.agent(
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
            roles.agent(self.target, "implement", "task", worktree, timeout=17)
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
        ):
            self.assertIn(flag, argv)
        self.assertEqual(
            [v for v in argv if v.startswith("--pids-limit")], ["--pids-limit=4096"]
        )
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

    def agy_project(self):
        self.target.path, worktree = self.make_worktree()
        release = self.fake_codex_release()
        binary = release / "agy"
        binary.write_text('#!/bin/sh\nprintf "%s" "$PATH"\n')
        binary.chmod(0o755)
        host_login = self.root / ".gemini/antigravity-cli/antigravity-oauth-token"
        host_login.parent.mkdir(parents=True)
        host_login.write_text("host-login")
        self.table["agents"].update(
            implementer_isolation="container",
            implementer={"harness": "agy", "model": "gemini-4-neon-xhigh"},
        )
        path = f"{release}{os.pathsep}{os.environ['PATH']}"
        self.enterContext(patch.dict(os.environ, {"HOME": str(self.root),
                                                  "PATH": path}))
        return worktree, binary, host_login

    def test_claude_and_codex_turns_in_an_agy_project_get_no_agy_mounts(self):
        from holophyte.isolation import launcher

        worktree, _, _ = self.agy_project()
        for program in ("claude", "codex"):
            with self.subTest(program=program):
                argv = [program, "-p", "goal"]
                route = launcher.turn_route(self.target, argv)
                _, mounts = self.recorded_volumes(lambda: launcher.launch(
                    route, worktree, {}, argv, project=self.target))
                self.assertFalse(any("/opt/agy/" in vol or "/.gemini" in vol
                                     for vol in mounts))

    def test_agy_mounts_the_host_binary_and_a_private_gemini_home_per_worktree(self):
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

        worktree, binary, host_login = self.agy_project()
        other = self.root / "other"
        git(self.target.path, "worktree", "add", "-qb", "other", str(other))
        target = Path("/home/implementer/.gemini")
        login = Path("antigravity-cli/antigravity-oauth-token")
        sibling = self.root / "provider.json"
        sibling.write_text("provider")
        provider = {"file": str(sibling),
                    "destination": str(target / login.with_name("key"))}
        argv = ["agy", "-p", "goal"]

        def run(command, cwd, timeout, *, env):
            mounts = [command[i + 1] for i, flag in enumerate(command)
                      if flag == "--volume"]
            self.assertIn(f"{binary}:/opt/agy/bin/agy:ro", mounts)
            turn = command[command.index(route.image) + 1:]
            self.assertEqual(turn[-len(argv):], argv)
            turn[-len(argv)] = str(binary)
            path = subprocess.check_output(turn, cwd=cwd, env=env, text=True)
            self.assertEqual(path.split(os.pathsep)[0], "/opt/agy/bin")
            volumes = [vol.split(":") for vol in mounts]
            gemini = [volume for volume in volumes
                      if Path(volume[1]).is_relative_to(target)]
            self.assertEqual([volume[1:] for volume in gemini], [[str(target), "rw"]])
            home = Path(gemini[0][0])
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            copy = home / login
            self.assertFalse(copy.samefile(host_login))
            self.assertEqual(copy.read_bytes(), b"host-login")
            self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
            copy.write_bytes(b"refreshed")
            if credential:
                self.assertEqual(copy.with_name("key").read_text(), "provider")
                copy.parent.chmod(0o500)
            return 0, str(home)

        with (patch.object(launcher, "image_ready"),
              patch.object(launcher.review_runner, "_remove_container"),
              patch.object(launcher, "run_capped", side_effect=run)):
            homes = []
            for task, credential in ((worktree, {}), (worktree, provider), (other, {})):
                self.table["agents"]["implementer_credential"] = credential
                route = launcher.turn_route(self.target, argv)
                code, home = launcher.launch(route, task, {}, argv, project=self.target,
                                             keep_session=True)
                self.assertEqual(code, 0)
                self.assertFalse((Path(home) / login.with_name("key")).exists())
                homes.append(home)
        self.assertEqual(homes[0], homes[1])
        self.assertNotEqual(homes[1], homes[2])
        self.assertEqual(host_login.read_bytes(), b"host-login")

    def test_agy_launch_requires_the_host_executable(self):
        from holophyte.isolation import launcher

        route = launcher.Route("container", writable=False)
        with (patch.dict(os.environ, {"PATH": str(self.root), "HOME": str(self.root)}),
              self.assertRaisesRegex(RuntimeError, "agy executable missing from PATH")):
            self.recorded_volumes(lambda: launcher.launch(
                route, self.root, {}, ["agy", "-p", "goal"]))

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

    def test_a_codex_trimmer_mounts_codex_for_its_own_launches_only(self):
        from holophyte.isolation import launcher

        _, worktree = self.make_worktree()
        release = self.fake_codex_release()
        for key, trimmer in (("trimmer", {"harness": "codex"}),
                             ("trimmer_fallback", "codex exec -m model")):
            with self.subTest(key=key):
                self.table["agents"] = {"implementer_isolation": "container",
                                        "implementer": {"harness": "claude"},
                                        key: trimmer}
                with patch.dict(os.environ, {"PATH": str(release)}):
                    implement, _ = launcher.container_command(
                        launcher.turn_route(self.target, ["claude", "-p"]),
                        worktree, {}, ["claude", "-p"], "n")
                    trim, _ = launcher.container_command(
                        launcher.turn_route(self.target, ["codex", "exec"]),
                        worktree, {}, ["codex", "exec"], "n")
                self.assertNotIn("codex", " ".join(implement))
                mounts = [trim[i + 1] for i, part in enumerate(trim)
                          if part == "--volume"]
                self.assertIn(f"{release}/codex:/opt/codex/bin/codex:ro", mounts)

    def probe_codex_fallback(self, fallback, implementer=None, credential=None):
        from holophyte.agents import probes
        from holophyte.isolation import launcher

        release = self.root / "release"
        if not release.exists():
            self.fake_codex_release()
        codex_home = self.root / "codex-home"
        codex_home.mkdir(exist_ok=True)
        (codex_home / "auth.json").write_text("codex-login-secret")
        self.table["agents"] = {"implementer_isolation": "container",
                                "implementer": implementer or {"harness": "claude"},
                                "implementer_fallback": fallback}
        if credential is not None:
            self.table["agents"]["implementer_credential"] = credential
        path = f"{release}{os.pathsep}{os.environ.get('PATH', '')}"
        with (patch.dict(os.environ, {"PATH": path, "CODEX_HOME": str(codex_home)}),
              patch.object(launcher, "image_ready"),
              patch.object(launcher.review_runner, "_remove_container"),
              patch.object(launcher, "run_capped",
                           return_value=(0, "PROBE_OK")) as run):
            result = probes.probe_launch(self.target, fallback.split(), 5,
                                         "implementer")
        self.assertEqual(result.returncode, 0, result.launch_error)
        return codex_home / "auth.json", run.call_args, result

    def test_codex_fallback_probe_mounts_the_codex_login_read_only(self):
        login, call, result = self.probe_codex_fallback("codex exec -m model")
        argv = call.args[0]
        mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "--volume"]
        self.assertIn(f"{login}:/opt/codex/login/auth.json:ro", mounts)
        self.assertIn('ln -sf /opt/codex/login/auth.json '
                      '"${CODEX_HOME:-/home/implementer/.codex}/auth.json"',
                      " ".join(argv))
        self.assertNotIn("codex-login-secret", str(call))
        self.assertNotIn("codex-login-secret", str(result))

    def test_non_codex_fallback_probe_gets_no_codex_login(self):
        _, call, _ = self.probe_codex_fallback("claude -p", "codex exec -m model")
        command = " ".join(call.args[0])
        self.assertIn('PATH="/opt/codex/bin:$PATH" exec', command)
        self.assertNotIn("auth.json", command)

    def test_only_an_explicit_codex_auth_file_replaces_the_host_login(self):
        token = self.root / "token"
        token.write_text("provider")
        for destination, mounted in (
                ("/home/implementer/.codex/provider/token", True),
                ("/home/implementer/.codex/auth.json", False)):
            with self.subTest(destination=destination):
                login, call, _ = self.probe_codex_fallback(
                    "codex exec -m model",
                    credential={"file": str(token), "destination": destination})
                mount = f"{login}:/opt/codex/login/auth.json:ro"
                self.assertEqual(mount in call.args[0], mounted)

    def test_quoted_codex_program_is_a_codex_implementer(self):
        from holophyte.isolation.launcher import route_for

        self.table["agents"] = {"implementer_isolation": "container",
                                "implementer": '"codex" exec -m model'}
        self.assertTrue(route_for(self.target).codex)

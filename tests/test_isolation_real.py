"""Real container launches, run only where Docker is enabled."""

import os
import shutil
import subprocess
import unittest
from dataclasses import replace
from unittest.mock import patch

from isolation_fixture import IsolationCase

from holophyte.agents import roles


class IsolationRealTests(IsolationCase):
    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_codex_implementer_runs_the_host_release_read_only(self):
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
    def test_real_codex_fallback_probe_reads_the_login_and_records_no_secret(self):
        import json

        from holophyte.agents import probes

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        release = self.fake_codex_release()
        (release / "codex").write_text(
            '#!/bin/sh\ngrep -q codex-login-secret'
            ' "${CODEX_HOME:-$HOME/.codex}/auth.json" && echo ready\n')
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text("codex-login-secret\n")
        token = self.root / "token"
        token.write_text("provider")
        source = self.root / "worktree.env"
        source.write_text("CODEX_HOME=/home/implementer/custom-codex\n")
        path = f"{release}{os.pathsep}{os.environ.get('PATH', '')}"
        for credential, allow in (
                ({}, []),
                ({}, ["CODEX_HOME"]),
                ({"file": str(token),
                  "destination": "/home/implementer/.codex/provider/token"}, [])):
            with self.subTest(credential=credential, allow=allow):
                self.table["worktree"] = {"env_source": str(source),
                                          "env_allow": allow}
                self.table["agents"] = {
                    "implementer_isolation": "container",
                    "implementer": {"harness": "claude"},
                    "implementer_fallback": "codex exec -m MODEL",
                    "implementer_credential": credential}
                with patch.dict(os.environ, {"PATH": path,
                                             "CODEX_HOME": str(codex_home)}):
                    probe = probes.probe_seat(self.target, "implement",
                                              fallback=True, timeout=120)
                self.assertTrue(probe.ok, probe.describe())
                recorded = (json.dumps(probe.to_json())
                            + probes.probe_diagnostic(self.target, probe))
                self.assertNotIn("codex-login-secret", recorded)

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_codex_fallback_reads_the_host_login_read_only(self):
        from holophyte.isolation import launcher

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        _, worktree = self.make_worktree()
        release = self.fake_codex_release()
        codex_home = self.root / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text("host-login\n")
        self.table["agents"] = {"implementer_isolation": "container",
                                "implementer": {"harness": "claude"},
                                "implementer_fallback": "codex exec -m MODEL"}
        script = ("cat ~/.codex/auth.json; mkdir ~/.codex/sessions && echo writable;"
                  " echo changed > ~/.codex/auth.json")
        path = f"{release}{os.pathsep}{os.environ.get('PATH', '')}"
        with patch.dict(os.environ, {"PATH": path, "CODEX_HOME": str(codex_home)}):
            route = replace(launcher.route_for(self.target), writable=False)
            code, output = launcher.launch(route, worktree, {},
                                           ["/bin/sh", "-c", script])
        self.assertNotEqual(code, 0, output)
        self.assertEqual(output.split()[:2], ["host-login", "writable"], output)
        self.assertIn("Read-only file system", output)
        self.assertEqual((codex_home / "auth.json").read_text(), "host-login\n")

    @unittest.skipUnless(
        os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
        "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
    )
    def test_real_session_files_persist_per_task_worktree(self):
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
                   "test_isolation*.py", "test_pr_media.py"]
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

        first = roles.agent(target, "implement", "implement", worktree,
                             conn=conn, run_id=run)
        self.assertEqual(first.exit_code, 0, first)
        argv, reason = fix_session.resume_argv(target, conn, run)
        self.assertIsNone(reason)
        self.assertEqual(argv[:3], ["claude", "-p", "--resume"])
        resumed = roles.agent(target, "implement", "findings", worktree,
                               conn=conn, run_id=run, argv=argv)
        self.assertEqual(resumed.exit_code, 0, resumed)

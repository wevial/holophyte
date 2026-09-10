"""Focused contracts for the local exact-SHA reviewer boundary.

Run: python3 -m unittest discover -s tests -p 'test_review_runner*' -v
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import review_runner

ROOT = Path(__file__).resolve().parent.parent


class ReviewerBoundaryTests(unittest.TestCase):
    def test_stage_is_exact_detached_clean_and_zero_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            stage = root / "stage"
            source.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=source, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=source,
                check=True,
            )
            (source / "value.txt").write_text("base\n")
            subprocess.run(["git", "add", "value.txt"], cwd=source, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "base"], cwd=source, check=True
            )
            base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source, text=True
            ).strip()
            (source / "value.txt").write_text("candidate\n")
            subprocess.run(
                ["git", "commit", "-qam", "candidate"], cwd=source, check=True
            )
            candidate = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=source, text=True
            ).strip()

            staged = review_runner.stage_candidate(source, stage, base, candidate)

            self.assertEqual((staged.base_sha, staged.candidate_sha), (base, candidate))
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=stage, text=True
                ).strip(),
                candidate,
            )
            self.assertNotEqual(
                subprocess.run(
                    ["git", "symbolic-ref", "-q", "HEAD"], cwd=stage
                ).returncode,
                0,
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "remote"], cwd=stage, text=True
                ).strip(),
                "",
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=stage, text=True
                ).strip(),
                "",
            )

    def _worktree_with_ignored_install(self, root):
        """A committed repository whose worktree holds an ignored directory.

        Returns (source, base, candidate). `console/node_modules` stands in
        for what `[worktree] setup` installs: present on disk, ignored by
        git, absent from every commit.
        """
        source = root / "source"
        source.mkdir()
        git = lambda *a: subprocess.run(["git", *a], cwd=source, check=True)  # noqa: E731
        git("init", "-q", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.invalid")
        (source / ".gitignore").write_text("node_modules/\n")
        (source / "console").mkdir()
        (source / "console" / "package.json").write_text("{}\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        base = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        (source / "console" / "package.json").write_text('{"name": "c"}\n')
        git("commit", "-qam", "candidate")
        candidate = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        installed = source / "console" / "node_modules" / "dep"
        installed.mkdir(parents=True)
        (installed / "index.js").write_text("module.exports = 1;\n")
        return source, base, candidate

    def test_carried_directories_are_copied_read_only_outside_the_fingerprint(
            self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, base, candidate = self._worktree_with_ignored_install(root)

            bare = review_runner.stage_candidate(
                source, root / "bare", base, candidate)
            staged = review_runner.stage_candidate(
                source, root / "stage", base, candidate,
                carry=["console/node_modules"])

            copied = staged.path / "console" / "node_modules" / "dep" / "index.js"
            self.assertEqual(copied.read_text(), "module.exports = 1;\n")
            for path in (copied, copied.parent, copied.parent.parent):
                self.assertFalse(path.stat().st_mode & 0o222, path)
            # The copy is a copy: the worktree's install is left alone.
            self.assertTrue(os.access(
                source / "console" / "node_modules" / "dep" / "index.js", os.W_OK))
            # Ignored in the stage too, so the clean check and the identity
            # the round is held to see the same tree with or without it.
            self.assertEqual(staged.fingerprint, bare.fingerprint)
            self.assertEqual(review_runner._fingerprint(staged.path),
                             staged.fingerprint)
            self.assertEqual(subprocess.check_output(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=staged.path, text=True), "")

    def test_a_tracked_absent_or_escaping_carry_path_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, base, candidate = self._worktree_with_ignored_install(root)
            (root / "outside").mkdir()

            for path in ("console", "console/.cache", "../outside"):
                with self.subTest(path=path):
                    stage = root / f"stage-{abs(hash(path))}"
                    with self.assertRaises(review_runner.ReviewBoundaryError) as e:
                        review_runner.stage_candidate(
                            source, stage, base, candidate, carry=[path])
                    self.assertIn(path, str(e.exception))
                    self.assertIn("carry", str(e.exception))

    def test_container_is_hardened_and_mounts_only_allowlisted_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "candidate"
            home = root / "home"
            toolchain = root / "toolchain"
            for path in (workspace, home, toolchain):
                path.mkdir()
            command = review_runner.container_command(
                image="holophyte-reviewer:test",
                workspace=workspace,
                reviewer_home=home,
                toolchain=toolchain,
                name="holophyte-review-test",
                prompt="review",
                uid=1000,
                gid=1000,
            )

        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn(f"{workspace.resolve()}:/workspace:ro", command)
        self.assertIn(f"{home.resolve()}:/home/reviewer:rw", command)
        self.assertIn(f"{toolchain.resolve()}:/opt/codex/bin:ro", command)
        mounts = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--volume"
        ]
        self.assertFalse(any("docker.sock" in mount for mount in mounts))
        rendered = "\n".join(command)
        self.assertIn("--json", rendered)
        # With no route named, the container runs today's pair, handed to the
        # script as arguments after the goal rather than spelled into it.
        self.assertEqual(command[-4:], ["review", "review", "gpt-5.6-sol",
                                        'model_reasoning_effort="medium"'])
        self.assertIn('-m "$2" -c "$3"', rendered)
        self.assertNotIn("gpt-5.6-sol", command[command.index("-c") + 1])

    def test_container_runs_the_model_and_effort_it_is_handed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("candidate", "home", "toolchain"):
                (root / name).mkdir()
            command = review_runner.container_command(
                image="holophyte-reviewer:test",
                workspace=root / "candidate",
                reviewer_home=root / "home",
                toolchain=root / "toolchain",
                name="holophyte-review-test",
                prompt="review",
                uid=1000,
                gid=1000,
                model="gpt-6-astra",
                effort="medium",
            )

        # `-m "$2"` and `-c "$3"` in the script, the pair as the arguments the
        # script reads them from: the quoting stays the shell's, not Python's.
        self.assertEqual(command[-2:], ["gpt-6-astra",
                                        'model_reasoning_effort="medium"'])
        self.assertIn('-m "$2" -c "$3"', "\n".join(command))
        self.assertNotIn("gpt-5.6-sol", "\n".join(command))
        self.assertEqual(review_runner.profile_for("gpt-6-astra", "medium"),
                         "codex-astra-medium")
        self.assertEqual(review_runner.PROFILE, "codex-sol-medium")

    def test_run_review_refuses_a_route_the_profile_does_not_name(self):
        # Refused before any staging: a `reviewRounds` row naming one route
        # about a round another ran is the record the runner will not help
        # write, and an effort outside Codex's vocabulary is not a route.
        with patch.object(review_runner, "_ensure_image",
                          side_effect=AssertionError("staged")):
            for kwargs, expected in (
                (dict(model="gpt-6-astra", effort="medium",
                      profile="codex-sol-medium"), "codex-astra-medium"),
                (dict(model="gpt-6-astra", effort="max"), "max"),
                (dict(model=""), "empty"),
            ):
                with self.subTest(**kwargs):
                    with self.assertRaises(review_runner.ReviewBoundaryError) as e:
                        review_runner.run_review(
                            repo=ROOT, base_sha="1" * 40, candidate_sha="2" * 40,
                            prompt="review", **kwargs)
                    self.assertIn(expected, str(e.exception))

    def test_structured_events_require_command_success_and_terminal_verdict(self):
        events = [
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "exit_code": 0},
            },
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": (
                        "Expected syntax: VERDICT: APPROVE\n"
                        "VERDICT: REQUEST_CHANGES"
                    ),
                },
            },
        ]
        output = "\n".join(json.dumps(event) for event in events)

        message, verdict = review_runner.parse_codex_output(output)

        self.assertEqual(verdict, "REQUEST_CHANGES")
        self.assertTrue(message.endswith("VERDICT: REQUEST_CHANGES"))
        without_command = "\n".join(json.dumps(event) for event in events[1:])
        with self.assertRaises(review_runner.ReviewBoundaryError):
            review_runner.parse_codex_output(without_command)


if __name__ == "__main__":
    unittest.main()


# --- the container's lifetime past the `finally` ------------------------------
# `run_review()` removes its container in a `finally`, which a process killed
# by a stop signal never reaches. These tests stand in a shim `docker` that
# records every argv it is given: `run` sleeps in place of the reviewer, `ps`
# answers from a file the test writes, and `inspect` reports nothing exists,
# so the runner's own post-removal check passes.

SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "$HOLOPHYTE_DOCKER_LOG"
case "$1" in
  run) : > "$HOLOPHYTE_DOCKER_STARTED"; exec sleep 10 ;;
  ps) cat "$HOLOPHYTE_DOCKER_PS" 2>/dev/null ;;
  inspect) exit 1 ;;
esac
exit 0
"""


def docker_shim(root: Path) -> tuple[Path, dict[str, str]]:
    """A bin directory holding the shim `docker`, and the environment it reads.

    The environment names the argv log, the file `ps` prints and the marker
    `run` touches before it sleeps; PATH puts the shim ahead of any real
    `docker`. `codex` and its helper are stubbed beside it so `run_review()`
    finds a "release" to copy.
    """
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("docker", "codex", "codex-code-mode-host"):
        script = bin_dir / name
        script.write_text(SHIM if name == "docker" else "#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOLOPHYTE_DOCKER_LOG": str(root / "docker.log"),
        "HOLOPHYTE_DOCKER_PS": str(root / "docker.ps"),
        "HOLOPHYTE_DOCKER_STARTED": str(root / "docker.started"),
    }
    return bin_dir, env


def two_commit_repo(path: Path) -> tuple[str, str]:
    """A repository with a base and a candidate commit; returns both SHAs."""
    path.mkdir()
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "value.txt").write_text("base\n")
    subprocess.run([*git, "add", "value.txt"], cwd=path, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], cwd=path, check=True)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path,
                                   text=True).strip()
    (path / "value.txt").write_text("candidate\n")
    subprocess.run([*git, "commit", "-qam", "candidate"], cwd=path, check=True)
    candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path,
                                        text=True).strip()
    return base, candidate


REVIEW_UNDER_TEST = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import review_runner
scratch = Path(sys.argv[2])
review_runner.SCRATCH_ROOT = scratch / "reviews"
review_runner.CODEX_AUTH = scratch / "auth.json"
review_runner.run_review(repo=Path(sys.argv[3]), base_sha=sys.argv[4],
                         candidate_sha=sys.argv[5], prompt="review")
"""


class ContainerLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.bin_dir, self.env = docker_shim(self.root)
        self.log = Path(self.env["HOLOPHYTE_DOCKER_LOG"])

    def recorded(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_stop_signal_removes_the_container_before_the_process_ends(self):
        """The leak the ticket names: a loop killed mid-review by SIGTERM
        (a closed tmux session, a supervisor stop) never reaches the
        `finally`. The shim must have been told to remove the container
        before the process ends, and the process must still end by the
        signal -- the caller's kill is not turned into a clean exit."""
        base, candidate = two_commit_repo(self.root / "repo")
        (self.root / "auth.json").write_text("{}")
        started = Path(self.env["HOLOPHYTE_DOCKER_STARTED"])
        process = subprocess.Popen(
            [sys.executable, "-c", REVIEW_UNDER_TEST, str(ROOT), str(self.root),
             str(self.root / "repo"), base, candidate],
            env={**os.environ, **self.env}, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._end_session, process)
        deadline = time.monotonic() + 15
        while not started.exists() and process.poll() is None:
            self.assertLess(time.monotonic(), deadline, "reviewer never started")
            time.sleep(0.05)
        if process.poll() is not None:
            self.fail(f"review ended before the signal: {process.communicate()[1]}")
        run_line = next(line for line in self.recorded() if line.startswith("run "))
        name = run_line.split()[run_line.split().index("--name") + 1]
        self.assertTrue(name.startswith("holophyte-review-"), name)
        self.assertNotIn(f"rm --force {name}", self.recorded())

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)

        self.assertEqual(process.returncode, -signal.SIGTERM, stderr)
        self.assertIn(f"rm --force {name}", self.recorded())

    @staticmethod
    def _end_session(process):
        """Reap the process and the shim's `sleep`, which outlives its parent."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            process.wait(timeout=5)

    def test_stray_containers_are_the_running_ones_without_a_scratch_directory(
            self):
        """Two review containers are running; one still has its scratch
        directory, so its loop is alive and it is not a stray. The other's
        directory is gone with the process that made it."""
        scratch = self.root / "reviews"
        (scratch / "review.live1234").mkdir(parents=True)
        Path(self.env["HOLOPHYTE_DOCKER_PS"]).write_text(
            "holophyte-review-live1234\nholophyte-review-gone5678\n")
        with patch.dict(os.environ, self.env), \
                patch.object(review_runner, "SCRATCH_ROOT", scratch):
            strays = review_runner.stray_containers()

        self.assertEqual(strays, ["holophyte-review-gone5678"])
        self.assertEqual(
            self.recorded(),
            ["ps --filter name=holophyte-review- --format {{.Names}}"])

    def test_without_docker_on_path_the_check_is_refused_not_answered(self):
        with patch.dict(os.environ, {"PATH": str(self.root / "empty")}):
            with self.assertRaises(review_runner.ReviewBoundaryError):
                review_runner.stray_containers()


class ContainerCommandTests(unittest.TestCase):
    def test_script_creates_the_temp_directory_and_keeps_tmp_noexec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("candidate", "home", "toolchain"):
                (root / name).mkdir()
            command = review_runner.container_command(
                image="holophyte-reviewer:test",
                workspace=root / "candidate",
                reviewer_home=root / "home",
                toolchain=root / "toolchain",
                name="holophyte-review-test",
                prompt="review",
                uid=1000,
                gid=1000,
            )

        script = command[command.index("-c") + 1]
        lines = script.splitlines()
        # The directory exists before any preflight probe runs.
        self.assertRegex(lines[0], r'^mkdir -p( -m 0?700)? "\$TMPDIR"$')
        self.assertTrue(any("rev-parse" in line for line in lines[1:]))
        tmpfs = command[command.index("--tmpfs") + 1]
        self.assertTrue(tmpfs.startswith("/tmp:"))
        self.assertIn("noexec", tmpfs.split(":", 1)[1].split(","))

    @staticmethod
    def _rendered(root):
        for name in ("candidate", "home", "toolchain"):
            (root / name).mkdir()
        return review_runner.container_command(
            image="holophyte-reviewer:test",
            workspace=root / "candidate",
            reviewer_home=root / "home",
            toolchain=root / "toolchain",
            name="holophyte-review-test",
            prompt="review",
            uid=1000,
            gid=1000,
        )

    def test_codex_runs_in_a_writable_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = self._rendered(Path(tmp))
        lines = command[command.index("-c") + 1].splitlines()
        preflight_ok = next(i for i, line in enumerate(lines) if "PREFLIGHT_OK" in line)
        copy = next(i for i, line in enumerate(lines) if line.startswith("cp -a "))
        run = next(i for i, line in enumerate(lines) if line.startswith("exec "))
        self.assertLess(preflight_ok, copy)
        self.assertLess(copy, run)
        self.assertEqual(lines[copy], "cp -a /workspace /home/reviewer/candidate")
        self.assertIn(" -C /home/reviewer/candidate", lines[run])
        self.assertNotIn("-C /workspace", lines[run])

    def test_workspace_stays_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = self._rendered(Path(tmp))
        script = command[command.index("-c") + 1]
        self.assertIn("touch /workspace/.holophyte-write-probe", script)
        mounts = [command[i + 1] for i, a in enumerate(command) if a == "--volume"]
        self.assertTrue(any(m.endswith(":/workspace:ro") for m in mounts), mounts)
        self.assertFalse(any(":/workspace:rw" in m for m in mounts), mounts)


class ReviewerImageTests(unittest.TestCase):
    DOCKERFILE = ROOT / "docker" / "reviewer.Dockerfile"

    def test_image_tag_is_v4_and_nothing_still_names_an_older_tag(self):
        self.assertEqual(review_runner.IMAGE, "holophyte-reviewer:ubuntu24.04-v4")
        stale = [
            path
            for path in [*ROOT.glob("*.py"), *(ROOT / "docs").glob("*.md")]
            if re.search(r"ubuntu24\.04-v[123]\b", path.read_text())
        ]
        self.assertEqual(stale, [])

    def test_dockerfile_installs_pinned_checksummed_bun_on_path(self):
        text = self.DOCKERFILE.read_text()
        version = re.search(r"^ARG BUN_VERSION=(\d+\.\d+\.\d+)$", text, re.M)
        checksum = re.search(r"^ARG BUN_SHA256=([0-9a-f]{64})$", text, re.M)
        self.assertIsNotNone(version, "Dockerfile pins no Bun version")
        self.assertIsNotNone(checksum, "Dockerfile pins no Bun SHA-256")
        self.assertIn("bun-v${BUN_VERSION}/bun-linux-x64.zip", text)
        self.assertRegex(
            text, r"(?m)^\s*&& echo \"\$\{BUN_SHA256\}  .*\| sha256sum -c -"
        )
        self.assertRegex(text, r"(?m)^ENV PATH=/opt/bun/bin:\$PATH$")
        self.assertRegex(text, r"(?m)^\s*&& ln -s bun /opt/bun/bin/bunx")

    def test_dockerfile_installs_pinned_checksummed_go_with_local_toolchain(self):
        text = self.DOCKERFILE.read_text()
        tarball = re.search(
            r"^ARG GO_TARBALL=go(\d+\.\d+\.\d+)\.linux-amd64\.tar\.gz$", text, re.M
        )
        checksum = re.search(r"^ARG GO_SHA256=([0-9a-f]{64})$", text, re.M)
        self.assertIsNotNone(tarball, "Dockerfile pins no Go tarball")
        self.assertEqual(tarball.group(1), "1.26.6")
        self.assertIsNotNone(checksum, "Dockerfile pins no Go SHA-256")
        self.assertIn("https://go.dev/dl/${GO_TARBALL}", text)
        self.assertRegex(
            text, r"(?m)^\s*&& echo \"\$\{GO_SHA256\}  .*\| sha256sum -c -"
        )
        self.assertRegex(text, r"(?m)^\s*&& tar -C /usr/local -xzf ")
        self.assertRegex(text, r"(?m)^ENV PATH=/usr/local/go/bin:\$PATH")
        self.assertRegex(text, r"(?m)^\s*GOTOOLCHAIN=local\b")
        self.assertRegex(text, r"(?m)^\s*GOPATH=/home/reviewer/go\b")
        self.assertRegex(text, r"(?m)^\s*GOMODCACHE=/home/reviewer/go/pkg/mod\b")
        self.assertRegex(text, r"(?m)^\s*GOCACHE=/home/reviewer/\.cache/go-build\b")

    def test_go_temp_directory_is_under_the_home(self):
        # `/tmp` is a noexec tmpfs; `go test` executes its test binaries from
        # GOTMPDIR and `t.TempDir()` follows TMPDIR, so both must name the same
        # executable directory on the writable reviewer home.
        text = self.DOCKERFILE.read_text()
        tmpdir = re.search(r"(?m)^\s*TMPDIR=(\S+?)\s*\\?$", text)
        gotmpdir = re.search(r"(?m)^\s*GOTMPDIR=(\S+?)\s*\\?$", text)
        self.assertIsNotNone(tmpdir, "Dockerfile sets no TMPDIR")
        self.assertIsNotNone(gotmpdir, "Dockerfile sets no GOTMPDIR")
        self.assertEqual(tmpdir.group(1), gotmpdir.group(1))
        self.assertTrue(tmpdir.group(1).startswith("/home/reviewer/"))

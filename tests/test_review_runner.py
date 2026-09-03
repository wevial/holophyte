"""Focused contracts for the local exact-SHA reviewer boundary.

Run: python3 -m unittest discover -s tests -p 'test_review_runner*' -v
"""
from __future__ import annotations

import contextlib
import json
import os
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
        self.assertIn("gpt-5.6-sol", rendered)
        self.assertIn('model_reasoning_effort="medium"', rendered)

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

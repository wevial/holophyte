"""Behavior and security-contract tests for the local reviewer boundary.

Run: python3 -m unittest discover -s tests -p 'test_review_runner*' -v
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import review_runner


class CandidateStagingTests(unittest.TestCase):
    def test_stage_is_detached_zero_remote_and_contains_exact_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            stage = root / "stage"
            source.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=source, check=True)
            (source / "value.txt").write_text("base\n")
            subprocess.run(["git", "add", "value.txt"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=source, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
            (source / "value.txt").write_text("candidate\n")
            subprocess.run(["git", "commit", "-qam", "candidate"], cwd=source, check=True)
            candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()

            staged = review_runner.stage_candidate(source, stage, base, candidate)

            self.assertEqual(staged.base_sha, base)
            self.assertEqual(staged.candidate_sha, candidate)
            self.assertEqual(
                subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=stage, text=True).strip(),
                candidate,
            )
            detached = subprocess.run(
                ["git", "symbolic-ref", "-q", "HEAD"], cwd=stage, capture_output=True
            )
            self.assertNotEqual(detached.returncode, 0)
            self.assertEqual(
                subprocess.check_output(["git", "remote"], cwd=stage, text=True).strip(), ""
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "refs/review/base", "refs/review/candidate"],
                    cwd=stage,
                    text=True,
                ).splitlines(),
                [base, candidate],
            )


class ContainerContractTests(unittest.TestCase):
    def test_tool_host_failure_cannot_be_accepted_as_review_evidence(self):
        failed = (
            "Code Mode is unavailable because failed to spawn code-mode host\n"
            "LOCAL_CONTAINER_REVIEW_OK"
        )

        with self.assertRaises(review_runner.ReviewBoundaryError):
            review_runner.validate_review_transcript(failed)

        review_runner.validate_review_transcript(
            "exec\n/bin/bash -lc 'git status' in /workspace\n succeeded in 1ms"
        )
        review_runner.validate_review_transcript(
            "exec\n/bin/bash -lc 'git diff' in /workspace\n succeeded in 1ms\n"
            '+        "Code Mode is unavailable because failed to spawn code-mode host"'
        )

    def test_container_contract_is_hardened_and_workspace_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "candidate"
            reviewer_home = root / "home"
            codex_release = root / "codex-release"
            workspace.mkdir()
            reviewer_home.mkdir()
            codex_release.mkdir()
            for executable in ("codex", "codex-code-mode-host"):
                path = codex_release / executable
                path.write_text("binary")
                path.chmod(0o700)
            command = review_runner.container_command(
                image="holophyte-reviewer:test",
                workspace=workspace,
                reviewer_home=reviewer_home,
                uid=1000,
                gid=1000,
                inner_command=["git", "status", "--short"],
                codex_release_dir=codex_release,
            )

        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn("--network=bridge", command)
        self.assertIn(f"{workspace.resolve()}:/workspace:ro", command)
        self.assertIn(f"{reviewer_home.resolve()}:/home/reviewer:rw", command)
        self.assertIn(f"{codex_release.resolve()}:/opt/codex/bin:ro", command)
        self.assertFalse(any("docker.sock" in part for part in command))
        self.assertEqual(command[-3:], ["git", "status", "--short"])

    def test_codex_profile_pins_sol_medium_inside_container_boundary(self):
        command = review_runner.profile_command("codex-sol-medium", "Review the exact candidate")

        self.assertEqual(
            command,
            [
                "/opt/codex/bin/codex",
                "exec",
                "-C",
                "/workspace",
                "-m",
                "gpt-5.6-sol",
                "-c",
                'model_reasoning_effort="medium"',
                "-s",
                "danger-full-access",
                "--ephemeral",
                "Review the exact candidate",
            ],
        )


if __name__ == "__main__":
    unittest.main()

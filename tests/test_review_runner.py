"""Focused contracts for the local exact-SHA reviewer boundary.

Run: python3 -m unittest discover -s tests -p 'test_review_runner*' -v
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import review_runner


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

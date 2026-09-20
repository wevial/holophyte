"""Run-local review boundaries in a repository shared by worktrees."""

import contextlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import review_runner
from holophyte import agents, dispatch, loop
from holophyte.dispatch import MergeParked
from holophyte.gates import InfraFailure, RunFailure
from holophyte.pr import Thread


class ReviewRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("commit", "--allow-empty", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "first")
        self.first = self.git("rev-parse", "HEAD")
        self.other = Path(self.tmp.name) / "other"
        self.git("worktree", "add", "-qb", "other", str(self.other), self.base)
        self.git("-C", str(self.other), "commit", "--allow-empty", "-qm", "second")
        self.second = self.git("-C", str(self.other), "rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, text=True, stderr=subprocess.PIPE
        ).strip()

    def test_two_worktrees_keep_their_own_candidates_and_stage(self):
        agents.publish_review_refs(self.repo, self.base, self.first, run_id=340)
        agents.publish_review_refs(self.other, self.base, self.second, run_id=341)
        self.assertEqual(self.git("rev-parse", "refs/review/340/candidate"), self.first)
        self.assertEqual(
            self.git("rev-parse", "refs/review/341/candidate"), self.second
        )
        stage = review_runner.stage_candidate(
            self.repo, Path(self.tmp.name) / "stage", self.base, self.first, run_id=340
        )
        self.assertEqual(
            self.git("-C", str(stage.path), "rev-parse", "refs/review/340/candidate"),
            self.first,
        )

    def test_dispatch_environment_and_moving_either_ref_is_infra_failure(self):
        target = Mock()
        target.config.return_value = {
            "agents": {"reviewer": "review-wrapper", "adjudicator": "judge-wrapper"}
        }
        for role in ("review", "adjudicate"):
            for moved in (None, "base", "candidate"):
                with self.subTest(role=role, moved=moved):

                    def dispatch_command(cmd, cwd, timeout, **kwargs):
                        self.assertIn(cmd[0], ("review-wrapper", "judge-wrapper"))
                        self.assertEqual(
                            kwargs["env"]["HOLOPHYTE_REVIEW_CANDIDATE"],
                            "refs/review/340/candidate",
                        )
                        if moved:
                            self.git(
                                "update-ref", f"refs/review/340/{moved}", self.second
                            )
                        return 0, "VERDICT: APPROVE"

                    with patch.object(
                        agents, "run_capped", side_effect=dispatch_command
                    ):
                        call = lambda: agents.agent(  # noqa: E731
                            target,
                            role,
                            "judge",
                            self.repo,
                            base_sha=self.base,
                            candidate_sha=self.first,
                            run_id=340,
                        )
                        if moved:
                            with self.assertRaises(InfraFailure):
                                call()
                        else:
                            self.assertEqual(call(), "VERDICT: APPROVE")

    def test_close_out_removes_only_its_run_for_every_outcome(self):
        for outcome in ("merged", "failed", "parked", "killed"):
            with self.subTest(outcome=outcome), contextlib.ExitStack() as stack:
                agents.publish_review_refs(self.repo, self.base, self.first, run_id=340)
                agents.publish_review_refs(
                    self.other, self.base, self.second, run_id=341
                )
                task = stack.enter_context(patch.object(loop, "run_task"))
                if outcome == "merged":
                    task.return_value = self.first
                else:
                    task.side_effect = {
                        "failed": RunFailure("failed"),
                        "parked": MergeParked("parked"),
                        "killed": KeyboardInterrupt(),
                    }[outcome]
                for name in (
                    "release_run",
                    "mirror_status",
                    "release_lease_label",
                    "refresh_findings",
                    "close_out_failure",
                ):
                    stack.enter_context(patch.object(dispatch, name))
                try:
                    dispatch._dispatch(
                        SimpleNamespace(path=self.repo), None, 340, None, {}, 1
                    )
                except KeyboardInterrupt:
                    self.assertEqual(outcome, "killed")
                for name in ("base", "candidate"):
                    result = subprocess.run(
                        ["git", "rev-parse", "--verify", f"refs/review/340/{name}"],
                        cwd=self.repo,
                        capture_output=True,
                    )
                    self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    self.git("rev-parse", "refs/review/341/candidate"), self.second
                )

    def test_all_four_prompts_name_the_run_pair(self):
        from holophyte import babysitter

        class Captured(Exception):
            pass

        prompts = []

        def capture(target, role, goal, *args, **kwargs):
            self.assertEqual(kwargs["run_id"], 340)
            prompts.append(goal)
            raise Captured

        common = dict(
            target=Mock(config=Mock(return_value={"merge": {"approve": "auto"}})),
            conn=None,
            run_id=340,
            provider=None,
            task_id=1,
            branch="task",
            wt=self.repo,
            beat_s=1,
            sha=self.first,
            ticket="ticket",
            verify_cmd="true",
            contracts=(),
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(loop, "agent", side_effect=capture))
            stack.enter_context(patch.object(loop, "set_phase"))
            stack.enter_context(patch.object(loop, "merge_conflicts", return_value=[]))
            stack.enter_context(patch.object(babysitter, "_next_round", return_value=1))
            for module in (loop, babysitter):
                stack.enter_context(
                    patch.object(module, "run_verify", return_value=(True, "ok"))
                )
            with self.assertRaises(Captured):
                loop._review_rounds(
                    **common, base_sha=self.base, criteria=(), budget_min=10, cap=1
                )
            with self.assertRaises(Captured):
                loop._terminal_adjudication(
                    **common, base_sha=self.base, task={}, cap=1
                )
            with self.assertRaises(Captured):
                babysitter._review_fix(
                    **common, reviewed=self.base, pull=SimpleNamespace(url="pull")
                )
            with self.assertRaises(Captured):
                babysitter._answer_threads(
                    **common,
                    pull=SimpleNamespace(url="pull"),
                    state=SimpleNamespace(threads=(Thread(
                        id="thread", path="app.py", line=1, author="review-bot",
                        body="Check this change", url="thread", author_kind="bot",
                    ),)),
                    rnd=1, pass_no=1, model="reviewer", budget_min=10,
                )
        self.assertEqual(len(prompts), 4)
        for prompt in prompts:
            self.assertIn("refs/review/340/base", prompt)
            self.assertIn("refs/review/340/candidate", prompt)
            self.assertNotIn("refs/review/candidate", prompt)

    def test_sweep_cleans_refs_only_after_confirmed_close_out(self):
        from holophyte import board

        agents.publish_review_refs(self.repo, self.base, self.first, run_id=340)
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    board.store,
                    "transaction",
                    side_effect=lambda conn: contextlib.nullcontext(),
                )
            )
            for name in (
                "release_run",
                "escalate",
                "release_lease_label",
                "refresh_findings",
            ):
                stack.enter_context(patch.object(board, name))
            for confirmed in (False, True):
                self.assertEqual(
                    board.close_out_failure(
                        SimpleNamespace(path=self.repo),
                        None,
                        340,
                        1,
                        confirm=lambda: confirmed,
                    ),
                    confirmed,
                )
                for name in ("base", "candidate"):
                    result = subprocess.run(
                        ["git", "rev-parse", "--verify", f"refs/review/340/{name}"],
                        cwd=self.repo,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode == 0, not confirmed)

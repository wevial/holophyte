"""A ticket's verify commands run under the project's `[verify] timeout_sec`."""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.tickets
from holophyte.config.project import Project
from holophyte.loop import merge_gate, review_round
from holophyte.loop.gates import RunFailure
from holophyte.loop.runs import open_store

TICKET = "# A ticket\n\n## Acceptance criteria\n\n- [ ] it works\n"
SLOW = "sleep 3"


class TicketVerifyTimeoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.email", "factory@example.invalid"),
                     ("config", "user.name", "Factory Test"),
                     ("commit", "-q", "--allow-empty", "-m", "base")):
            subprocess.run(["git", *args], cwd=self.repo, check=True,
                           capture_output=True)
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                  check=True, capture_output=True,
                                  text=True).stdout.strip()

    def open_project(self, timeout_sec):
        project = Project.locate(self.repo)
        project.config_path.parent.mkdir(parents=True, exist_ok=True)
        project.config_path.write_text(f"[verify]\ntimeout_sec = {timeout_sec}\n")
        conn = open_store(project)
        self.addCleanup(conn.close)
        project_id = store.tickets.ensure_project(conn, "team", self.repo)
        ticket = store.tickets.mirror_ticket(
            conn, project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["it works"],
            verification_commands=[SLOW], time_box_ms=3_600_000)
        run_id = store.claim(conn, project_id, ticket)
        store.set_phase(conn, run_id, "working")
        return project, conn, run_id

    def review(self, timeout_sec):
        """One review round whose reviewer approves; return result and prompts."""
        project, conn, run_id = self.open_project(timeout_sec)
        prompts = []

        def reviewer(target, role, goal, *args, **kwargs):
            prompts.append(goal)
            return "Looks right.\nVERDICT: APPROVE"

        with patch.object(review_round, "agent", side_effect=reviewer), \
                patch.object(review_round, "merge_conflicts", return_value=[]), \
                patch("holophyte.agents.fix_session.fix_turn",
                      return_value=("nothing to fix", False)), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                result = review_round._review_rounds(
                    project, conn, run_id, provider=None, task_id="KO-1",
                    branch="main", wt=self.repo, beat_s=60,
                    base_sha=self.sha, sha=self.sha, ticket=TICKET,
                    verify_cmd=SLOW, contracts=(), criteria=(),
                    budget_min=10, cap=1)
            except RunFailure as failure:
                result = failure
        return result, prompts

    def merge(self, timeout_sec):
        """The merge gate's verify on the reviewed sha; return result and output."""
        project, conn, run_id = self.open_project(timeout_sec)
        for phase in ("verifying", "reviewing"):
            store.set_phase(conn, run_id, phase)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                result = merge_gate._merge_gate(
                    project, conn, run_id, None, "KO-1", "issue-1", "main",
                    self.repo, 60, self.sha, SLOW, [], TICKET, 10,
                    sync_main=False)
            except RunFailure as failure:
                result = failure
        return result, out.getvalue()

    def test_review_round_verify_times_out_at_the_configured_cap(self):
        result, prompts = self.review(2)

        self.assertIsInstance(result, RunFailure)
        self.assertIn("verify timed out after 2s", prompts[0])

    def test_review_round_verify_passes_under_a_cap_above_its_runtime(self):
        result, prompts = self.review(5)

        self.assertEqual(result, (self.sha, 1, True))
        self.assertNotIn("timed out", prompts[0])

    def test_merge_gate_verify_times_out_at_the_configured_cap(self):
        result, out = self.merge(2)

        self.assertIsInstance(result, RunFailure)
        self.assertIn("verify FAILED before merge", out)
        self.assertIn("verify timed out after 2s", out)

    def test_merge_gate_verify_passes_under_a_cap_above_its_runtime(self):
        result, out = self.merge(5)

        self.assertEqual(result, (True, self.sha))
        self.assertIn("verify ok before merge", out)


if __name__ == "__main__":
    unittest.main()

"""A failed verify is rerun once; a pass on the rerun is flaky, not a fix round."""
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import holophyte.cli.entry
import store
import store.tickets
from holophyte.config.project import Project
from holophyte.loop import gates, review_round
from holophyte.loop.gates import RunFailure, run_verify
from holophyte.loop.runs import open_store

ACCESS_LOG = re.compile(r'\[[^\]]+\] "GET /')
TICKET = "# A ticket\n\n## Acceptance criteria\n\n- [ ] it works\n"


def flaky_command(marker):
    """Fails while `marker` is absent and creates it, so only the rerun passes."""
    return (f"test -e {marker} || {{ touch {marker};"
            f" echo 'AssertionError: first attempt'; exit 1; }}")


def counting_command(counter, status):
    return (f"echo run >> {counter}; echo \"attempt $(wc -l < {counter})\";"
            f" exit {status}")


class VerifyRerunTests(unittest.TestCase):
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
        self.project = Project.locate(self.repo)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(self.conn, "team", self.repo)
        self.run_id = self.claim(1)

    def claim(self, number):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{number}",
            linear_identifier=f"KO-{number}", title="a ticket",
            acceptance_criteria=["it works"],
            verification_commands=["true"], time_box_ms=3_600_000)
        run_id = store.claim(self.conn, self.project_id, ticket)
        store.set_phase(self.conn, run_id, "working")
        return run_id

    def flaky_events(self):
        return [json.loads(payload)["report"] for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'verify_flaky'")]

    def review(self, verify_cmd):
        """Drive one round whose reviewer approves; return prompts and fix turns."""
        prompts, fixes = [], []

        def reviewer(target, role, goal, *args, **kwargs):
            prompts.append(goal)
            return "Looks right.\nVERDICT: APPROVE"

        def fix_turn(*args, **kwargs):
            fixes.append(args)
            return "nothing to fix", False

        with patch.object(review_round, "agent", side_effect=reviewer), \
                patch.object(review_round, "merge_conflicts", return_value=[]), \
                patch("holophyte.agents.fix_session.fix_turn", fix_turn), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                result = review_round._review_rounds(
                    self.project, self.conn, self.run_id, provider=None,
                    task_id=1, branch="main", wt=self.repo, beat_s=60,
                    base_sha=self.sha, sha=self.sha, ticket=TICKET,
                    verify_cmd=verify_cmd, contracts=(), criteria=(),
                    budget_min=10, cap=1)
            except RunFailure as failure:
                result = failure
        return result, prompts, fixes

    def test_a_verify_that_passes_on_its_rerun_is_flaky_and_starts_no_fix_turn(self):
        result, _, fixes = self.review(flaky_command(self.root / "marker"))

        self.assertEqual(result, (self.sha, 1, True))
        self.assertEqual(fixes, [])
        (report,) = self.flaky_events()
        self.assertIn("[verify] FAILED: command exited 1", report)
        self.assertIn("AssertionError: first attempt", report)

    def test_a_verify_that_fails_twice_goes_to_a_fix_turn_with_both_reports(self):
        counter = self.root / "count"
        result, prompts, fixes = self.review(counting_command(counter, 1))

        self.assertIsInstance(result, RunFailure)
        self.assertEqual(len(fixes), 1)
        self.assertEqual(counter.read_text().count("run"), 2)
        self.assertIn("attempt 1", prompts[0])
        self.assertIn("attempt 2", prompts[0])
        self.assertEqual(self.flaky_events(), [])

    def test_a_timeout_that_passes_on_its_rerun_is_flaky(self):
        marker = self.root / "marker"
        ok, out = run_verify(f"test -e {marker} || {{ touch {marker}; sleep 30; }}",
                             self.repo, timeout=1, conn=self.conn,
                             run_id=self.run_id)

        self.assertTrue(ok, out)
        (report,) = self.flaky_events()
        self.assertTrue(gates.verify_timed_out(report), report)

    def test_a_verify_that_times_out_twice_fails_with_both_reports(self):
        counter = self.root / "count"
        ok, out = run_verify(f"echo run >> {counter};"
                             f" echo \"attempt $(wc -l < {counter})\"; sleep 30",
                             self.repo, timeout=1, conn=self.conn,
                             run_id=self.run_id)

        self.assertFalse(ok)
        self.assertTrue(gates.verify_timed_out(out), out)
        self.assertEqual(counter.read_text().count("run"), 2)
        self.assertIn("attempt 1", out)
        self.assertIn("attempt 2", out)
        self.assertEqual(self.flaky_events(), [])

    def test_a_passing_verify_runs_exactly_once(self):
        counter = self.root / "count"
        ok, _ = run_verify(counting_command(counter, 0), self.repo,
                           conn=self.conn, run_id=self.run_id)

        self.assertTrue(ok)
        self.assertEqual(counter.read_text(), "run\n")

    def test_report_prints_the_count_of_flaky_verifies(self):
        for number, run_id in ((1, self.run_id), (2, self.claim(2))):
            ok, _ = run_verify(flaky_command(self.root / f"marker-{number}"),
                               self.repo, conn=self.conn, run_id=run_id)
            self.assertTrue(ok)
        out = io.StringIO()
        with patch("holophyte.cli.entry.board_for", return_value=Mock()), \
                contextlib.redirect_stdout(out):
            holophyte.cli.entry.cli([str(self.repo), "--report"])

        self.assertIn("verify flaky: 2", out.getvalue().splitlines())


class KeyLinesTests(unittest.TestCase):
    LOG = "".join(f'127.0.0.1 - - [02/Oct/2026 10:{n // 60 % 60:02d}:{n % 60:02d}]'
                  f' "GET /asset/{n}.js HTTP/1.1" 200 -\n' for n in range(2500))

    def test_failing_clause_report_keeps_the_assertion_and_drops_the_access_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server.log"
            log.write_text(self.LOG + "AssertionError: expected 3 got 2\n" + self.LOG)
            ok, out = run_verify(f"true && sh -c 'cat {log}; exit 1'", tmp)

        self.assertFalse(ok)
        key = out.split("[verify]   key lines:\n", 1)[1].split("[verify]   ---")[0]
        self.assertIn("AssertionError: expected 3 got 2", key)
        self.assertIsNone(ACCESS_LOG.search(out))

    def test_timeout_report_leads_with_key_lines_and_drops_the_access_log(self):
        running = self.LOG + "AssertionError: expected 3 got 2\n" + self.LOG
        report = gates.timeout_failure_report(
            "make lint && make check", ["make lint", "make check"],
            {1: "lint ok", 2: running}, "lint ok\n" + running, 5)

        key = report.split("[verify]   key lines:\n", 1)[1]
        key = key.split("[verify]   ---")[0]
        self.assertIn("AssertionError: expected 3 got 2", key)
        self.assertIsNone(ACCESS_LOG.search(report))


if __name__ == "__main__":
    unittest.main()

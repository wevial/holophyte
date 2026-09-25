"""`--abort KO-n --close-pr`: the abort, then the pull request's comment and
close, recorded on the intervention before anything is killed (KO-611)."""
from __future__ import annotations

import contextlib
import io
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, IMPLEMENT, Commit, FakeAgent, Idle, Reply  # noqa: E402
from loop_fixture import BRANCH, MergeModeFixture  # noqa: E402

import holophyte.cli  # noqa: E402

NOTE = "wrong approach"
BOARD = '[board]\nteam = "team-1"\nproject_id = "project-1"\n'
COMMENT = "POST repos/example/repo/issues/7/comments"
CLOSE = "PATCH repos/example/repo/pulls/7"


class AbortCloseTests(MergeModeFixture):
    def abort(self, *flags):
        """`factory.py TARGET --abort KO-131 FLAGS --note NOTE` through the
        real command line; the board is the loop's stub."""
        out = io.StringIO()
        with patch.object(holophyte.cli, "board_for",
                          return_value=self.provider()), \
                contextlib.redirect_stdout(out):
            holophyte.cli.cli([str(self.target), "--abort", "KO-131", *flags,
                               "--note", NOTE])
        return out.getvalue()

    def gh(self, *kinds):
        """The recorded `gh api` calls that are comments or closes, in order."""
        return [kind for line in self.recorded() for kind in kinds
                if line.startswith("gh api") and kind in line]

    def parked_on_its_pull_request(self, close_exit=0):
        """A run parked on pull request 7 awaiting a human's merge, with an
        edit left in its tree and its worker's process gone."""
        self.configure(BOARD + '[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(close_exit=close_exit)
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(Commit(), APPROVE, Idle(""), provider=self.provider())
        (self.worktrees / "ko-131-add-a-thing" / "late.txt").write_text("keep\n")
        dead = subprocess.Popen(["true"])
        dead.wait()
        with contextlib.closing(sqlite3.connect(self.db)) as raw, raw:
            raw.execute("UPDATE runs SET workerPid = ?", (dead.pid,))

    def test_a_live_fix_turn_is_aborted_then_its_pull_request_closed(self):
        self.configure(BOARD + '[merge]\nmode = "pr"\napprove = "auto"\n'
                       "[supervisor]\nheartbeat_stale_min = 0.05\n")
        self.fake_route(states=[self.pr_state([self.DEFECT])])
        test, seen = self, {}

        class SleepingFix:
            """The fix turn: a real sleeping process group the abort kills."""

            role = IMPLEMENT

            def play(self, cwd, turn):
                Path(cwd, "fix.txt").write_text("half a fix\n")
                proc = subprocess.Popen(["sh", "-c", "sleep 30 & exec sleep 30"],
                                        start_new_session=True)
                fake.turns[-1].on_start(proc)
                seen["cli"] = test.abort("--close-pr")
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                seen["returncode"] = proc.returncode
                time.sleep(0.2)
                return "killed"

        fake = FakeAgent(Commit(), APPROVE, Idle(""),
                         Reply("THREAD 1: ADDRESS -- broken"), SleepingFix())
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(fake=fake, provider=self.provider())

        self.assertIn("abort requested", seen["cli"])
        self.assertEqual(seen["returncode"], -signal.SIGKILL)
        self.assertEqual(self.read("SELECT outcome, outcomeReason FROM runs"),
                         [("abandoned", NOTE)])
        ((asked, ended),) = self.read(
            "SELECT i.at, r.endedAt FROM interventions i JOIN runs r"
            " ON r.id = i.runId WHERE i.action = 'abort_close'")
        self.assertLessEqual(asked, ended)
        wip = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.subjects(BRANCH)[0],
                         "WIP: preserve work at operator abort")
        self.assertEqual(self.pushed()[-1], (BRANCH, wip))
        self.assertEqual(self.gh(COMMENT, CLOSE), [COMMENT, CLOSE])
        (body,) = [call["body"] for kind, call in self.api_calls()
                   if kind == "conversation"]
        self.assertTrue(body.startswith("---- Comment by "), body)
        self.assertIn(NOTE, body)
        self.assertIn(wip[:7], body)
        self.assertIn("--requeue KO-131", body)

    def test_the_cli_closes_for_a_worker_that_is_gone(self):
        self.parked_on_its_pull_request()
        pushes = len(self.pushed())

        self.assertIn("no live worker", self.abort("--close-pr"))

        wip = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.subjects(BRANCH)[0],
                         "WIP: preserve work at operator abort")
        self.assertEqual(self.pushed()[pushes:], [(BRANCH, wip)])
        self.assertEqual(self.gh(COMMENT, CLOSE), [COMMENT, CLOSE])
        self.assertEqual(self.read("SELECT action FROM interventions"
                                   " WHERE action LIKE 'abort%'"),
                         [("abort_close",)])
        self.assertEqual(self.read(
            "SELECT r.outcome, t.status FROM runs r JOIN tickets t"
            " ON t.id = r.ticketId"), [("abandoned", "blocked_on_operator")])

    def test_a_plain_abort_leaves_the_pull_request_open(self):
        self.parked_on_its_pull_request()

        self.abort()

        self.assertEqual(self.read("SELECT action FROM interventions"
                                   " WHERE action LIKE 'abort%'"), [("abort",)])
        self.assertEqual(self.gh(COMMENT, CLOSE), [])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("abandoned",)])

    def test_a_refused_close_does_not_undo_the_abort(self):
        self.parked_on_its_pull_request(close_exit=1)

        printed = self.abort("--close-pr")

        self.assertEqual(self.gh(COMMENT, CLOSE), [COMMENT, CLOSE])
        self.assertEqual(self.read(
            "SELECT r.outcome, t.status FROM runs r JOIN tickets t"
            " ON t.id = r.ticketId"), [("abandoned", "blocked_on_operator")])
        (event,) = self.read("SELECT summary FROM runEvents"
                             " WHERE kind = 'warning' AND summary LIKE 'abort%'")
        self.assertIn("abort close of https://github.com/example/repo/pull/7",
                      event[0])
        self.assertIn("abort close", printed)

    def test_close_pr_without_abort_is_refused_naming_abort(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as exited:
            holophyte.cli.cli([str(self.target), "--close-pr"])
        self.assertNotEqual(exited.exception.code, 0)
        self.assertIn("--abort", err.getvalue())

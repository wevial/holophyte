"""A pause landing while a merge-gate check fix turn runs."""
from __future__ import annotations

import io
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # factory.py imports store by name
sys.path.insert(0, str(HERE))  # `discover -s tests` and `-m unittest` alike
import babysit_fixture as cases  # noqa: E402
from abort_fixture import group_runs  # noqa: E402
from fake_agent import APPROVE, Commit, FakeAgent, Idle  # noqa: E402
from loop_fixture import BRANCH, MergeModeFixture  # noqa: E402
from pause_fixture import PauseEdit  # noqa: E402

import holophyte.config.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402
import holophyte.pr.pr_status  # noqa: E402
import store  # noqa: E402
import store.read  # noqa: E402
from holophyte.loop.stop import command  # noqa: E402


class PauseFixTurnTests(cases.BabysitHelpers, MergeModeFixture):
    UNIT = {"name": "unit", "status": "completed", "conclusion": "failure",
            "html_url": "https://github.com/example/repo/actions/runs/5/job/42",
            "id": 42, "app": {"slug": "github-actions"}}

    def red_on_first_head(self, log, config=""):
        """`UNIT` red with `log` as its job log on the first head, a new job
        id on every read, and green on every later head."""
        self.configure('[merge]\nmode = "pr"\n' + config)
        self.fake_route()
        self.enterContext(patch.object(holophyte.pr.github, "SLEEP"))
        heads, ids = [], iter(range(42, 10**4))
        def check_runs(target, pull, sha):
            if sha not in heads:
                heads.append(sha)
                self.job_log.write_text(log)
            if len(heads) > 1:
                return [dict(self.UNIT, conclusion="success")]
            return [dict(self.UNIT, id=next(ids))]
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of", check_runs))

    def pause_a_running_check_fix_turn(self):
        """Run the loop to a check fix turn that writes `pause-work.txt`,
        runs a real process group and records a pause while it runs."""
        self.red_on_first_head("FAIL: test_a (tests.test_y.Case.test_a)\n",
                               "[supervisor]\nheartbeat_stale_min = 0.05\n")
        seen, fake = {}, FakeAgent()
        test = self

        class SleepUntilPaused(PauseEdit):
            """Writes `pause-work.txt`, then runs a real process group
            sleeping 30 s and records the pause while it runs."""

            def play(self, cwd, turn):
                proc = subprocess.Popen(["sh", "-c", "sleep 30 & exec sleep 30"],
                                        start_new_session=True)
                fake.turns[-1].on_start(proc)
                super().play(cwd, turn)
                seen["pushed"] = len(test.pushed())
                asked = time.monotonic()
                try:
                    proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                seen["elapsed"] = time.monotonic() - asked
                seen["returncode"] = proc.returncode
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if not group_runs(proc.pid):
                        seen["group_gone"] = True
                        break
                    time.sleep(0.05)
                return "stopped"

        fake.script = [Commit("the scripted work"), APPROVE, Idle(""),
                       SleepUntilPaused(self.db)]
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(fake=fake, provider=self.provider())
        return seen, fake

    def test_a_pause_kills_a_running_check_fix_turn_at_the_next_heartbeat(self):
        seen, fake = self.pause_a_running_check_fix_turn()
        beat_s = holophyte.config.config_tables.sweep_config(
            self.project).heartbeat_stale_ms / 2000
        self.assertIn("CHECK unit", fake.turns[3].goal)
        self.assertEqual(seen["returncode"], -signal.SIGKILL)
        self.assertLess(seen["elapsed"], beat_s + 1)
        self.assertTrue(seen.get("group_gone"))
        self.assertEqual(
            self.read("SELECT outcome, resumePhase, parkKind FROM runs"),
            [("paused", "merge_gate", None)])

    def test_a_fix_turn_paused_mid_turn_parks_the_ticket_and_frees_its_slot(self):
        self.pause_a_running_check_fix_turn()
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        self.assertEqual([(t.linearIdentifier, t.blockedQuestion)
                          for t in store.read.blocked_tickets(conn)],
                         [("KO-131", "reboot writer")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        ((asked, ended),) = self.read(
            "SELECT i.at, r.endedAt FROM interventions i JOIN runs r"
            " ON r.id = i.runId WHERE i.action = 'pause'")
        self.assertLessEqual(asked, ended)
        self.assertEqual(len(self.read(
            "SELECT 1 FROM runEvents WHERE kind = 'pause_checkpoint'")), 1)

    def test_a_fix_turn_paused_mid_turn_commits_its_edit_and_publishes_nothing(self):
        seen, _ = self.pause_a_running_check_fix_turn()
        self.assertEqual(self.git("show", f"{BRANCH}:pause-work.txt"),
                         "preserve this uncommitted work\n")
        self.assertEqual(len(self.pushed()), seen["pushed"])
        self.assertEqual([kind for kind, _ in self.api_calls()
                          if kind in ("reply", "resolve", "merge")], [])

    def test_resuming_a_fix_turn_paused_mid_turn_reruns_it_on_its_commit(self):
        self.pause_a_running_check_fix_turn()
        preserved = self.git("log", "-1", "--format=%H", BRANCH,
                             "--", "pause-work.txt").strip()
        command(self.project, "KO-131", "go on", resume=True)
        with patch.object(sys, "stdout", io.StringIO()):
            fake, _ = self.loop(Commit("fix: unit"), APPROVE, Idle(""),
                                provider=self.provider())
        (goal,) = [turn.goal for turn in fake.turns
                   if turn.role == "implement" and "CHECK unit" in turn.goal]
        self.assertIn(preserved, goal)
        head = self.pushed()[-1][1]
        self.assertEqual(self.git("show", f"{head}:pause-work.txt"),
                         "preserve this uncommitted work\n")
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [head])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])

    def test_a_pause_during_the_rerun_wait_ends_the_run_before_its_next_nap(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        self.job_log.write_text("FAIL: test_x (tests.test_y.Case.test_x)")
        self.enterContext(patch("holophyte.pr.pr_status._check_runs_of",
                                lambda target, pull, sha: [self.UNIT]))
        paused, naps = [], []
        self.enterContext(patch.object(
            holophyte.pr.github, "SLEEP", lambda s: naps.append(bool(paused))))
        real = holophyte.pr.pr_status.pr_state

        def pr_state(target, pull):
            state = real(target, pull)
            if not paused and any("rerun-failed-jobs" in line
                                  for line in self.recorded()):
                conn = store.open(self.db)
                try:
                    (run,) = conn.execute(
                        "SELECT id FROM runs WHERE endedAt IS NULL").fetchone()
                    store.pause(conn, run, "reboot writer")
                finally:
                    conn.close()
                paused.append(True)
            return state
        self.enterContext(patch.object(holophyte.pr.pr_status, "pr_state", pr_state))
        with patch.object(sys, "stdout", io.StringIO()):
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                provider=self.provider())
        self.assertTrue(paused)
        self.assertNotIn(True, naps)
        self.assertEqual([turn.goal for turn in fake.turns
                          if "CHECK unit" in turn.goal], [])
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "merge_gate")])

    def test_a_pause_lets_an_implement_phase_turn_finish_first(self):
        self.configure('[merge]\nmode = "pr"\n'
                       "[supervisor]\nheartbeat_stale_min = 0.05\n")
        self.fake_route()
        seen, fake, db = {}, FakeAgent(), self.db

        class SleepPastAPause(PauseEdit):
            """Records a pause, then runs a real process group sleeping 3 s
            across a heartbeat."""

            def play(self, cwd, turn):
                proc = subprocess.Popen(["sh", "-c", "sleep 3 & exec sleep 3"],
                                        start_new_session=True)
                fake.turns[-1].on_start(proc)
                super().play(cwd, turn)
                seen["returncode"] = proc.wait(timeout=20)
                conn = store.open(db)
                try:
                    seen["ended"] = conn.execute(
                        "SELECT endedAt FROM runs").fetchone()[0]
                finally:
                    conn.close()
                return "done"

        fake.script = [SleepPastAPause(db)]
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(fake=fake, provider=self.provider())
        self.assertEqual(seen["returncode"], 0)
        self.assertIsNone(seen["ended"])
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "verifying")])

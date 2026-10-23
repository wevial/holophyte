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
from loop_fixture import MergeModeFixture  # noqa: E402
from pause_fixture import PauseEdit  # noqa: E402

import holophyte.config.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402


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

    def test_a_pause_kills_a_running_check_fix_turn_at_the_next_heartbeat(self):
        self.red_on_first_head("FAIL: test_a (tests.test_y.Case.test_a)\n",
                               "[supervisor]\nheartbeat_stale_min = 0.05\n")
        beat_s = holophyte.config.config_tables.sweep_config(
            self.project).heartbeat_stale_ms / 2000
        seen, fake = {}, FakeAgent()

        class SleepUntilPaused(PauseEdit):
            """Writes `pause-work.txt`, then runs a real process group
            sleeping 30 s and records the pause while it runs."""

            def play(self, cwd, turn):
                proc = subprocess.Popen(["sh", "-c", "sleep 30 & exec sleep 30"],
                                        start_new_session=True)
                fake.turns[-1].on_start(proc)
                super().play(cwd, turn)
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
        self.assertIn("CHECK unit", fake.turns[3].goal)
        self.assertEqual(seen["returncode"], -signal.SIGKILL)
        self.assertLess(seen["elapsed"], beat_s + 1)
        self.assertTrue(seen.get("group_gone"))
        self.assertEqual(
            self.read("SELECT outcome, resumePhase, parkKind FROM runs"),
            [("paused", "merge_gate", None)])

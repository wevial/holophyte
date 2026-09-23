"""An operator's `--abort` landing while a real turn process runs (KO-592)."""
import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from fake_agent import IMPLEMENT, Commit, FakeAgent
from loop_fixture import BRANCH, VALID_BODY, StubProvider, a_task

import holophyte.agents as agents
import holophyte.config_tables
import store

NOTE = "host going down"


def live_run(db):
    conn = store.open(str(db))
    run = conn.execute("SELECT id FROM runs WHERE endedAt IS NULL"
                       " ORDER BY id DESC LIMIT 1").fetchone()[0]
    return conn, run


class AbortEdit:
    """An implementer turn that leaves an edit uncommitted and is aborted."""

    role = IMPLEMENT

    def __init__(self, db):
        self.db = db

    def play(self, cwd, turn):
        Path(cwd, "abort-work.txt").write_text("keep this edit\n")
        conn, run = live_run(self.db)
        try:
            store.abort(conn, run, NOTE)
        finally:
            conn.close()
        return "edit left for the abort"


class AbortTurnCases:
    def test_abort_kills_the_turn_group_commits_wip_and_parks(self):
        # A 3 s stale threshold beats every 1.5 s.
        self.configure("[supervisor]\nheartbeat_stale_min = 0.05\n")
        beat_s = holophyte.config_tables.sweep_config(
            self.project).heartbeat_stale_ms / 2000
        db, seen, fake = self.db, {}, FakeAgent()

        class SleepUntilAborted(AbortEdit):
            """A real process group, a shell and its child, both sleeping."""

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
                    try:
                        os.killpg(proc.pid, 0)
                    except ProcessLookupError:
                        seen["group_gone"] = True
                        break
                    time.sleep(0.05)
                return "killed"

        fake.script = [SleepUntilAborted(db)]
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(fake=fake)
        self.assertEqual(seen["returncode"], -signal.SIGKILL)
        self.assertLess(seen["elapsed"], beat_s + 1)
        self.assertTrue(seen.get("group_gone"))
        self.assertEqual(self.read("SELECT outcome, outcomeReason FROM runs"),
                         [("abandoned", NOTE)])
        self.assertEqual(self.read("SELECT status, blockedQuestion FROM tickets"),
                         [("blocked_on_operator", NOTE)])
        self.assertEqual(self.subjects(BRANCH)[0],
                         "WIP: preserve work at operator abort")
        self.assertEqual(self.git("show", f"{BRANCH}:abort-work.txt"),
                         "keep this edit\n")
        ((asked, ended),) = self.read(
            "SELECT i.at, r.endedAt FROM interventions i JOIN runs r"
            " ON r.id = i.runId WHERE i.action = 'abort'")
        self.assertLessEqual(asked, ended)
        events = [summary for (summary,) in
                  self.read("SELECT summary FROM runEvents ORDER BY seq")]
        request = events.index(f"human abort: {NOTE}")
        release = next(i for i, s in enumerate(events) if "outcome abandoned" in s)
        self.assertLess(request, release)

    def test_abort_kills_a_configured_reviewers_group(self):
        self.configure("[supervisor]\nheartbeat_stale_min = 0.05\n[agents]\n"
                       "reviewer = \"sh -c 'sleep 30 & exec sleep 30'\"\n")
        beat_s = holophyte.config_tables.sweep_config(
            self.project).heartbeat_stale_ms / 2000
        implementer, seen, real = FakeAgent(Commit("work")), {}, agents.run_capped

        def run_capped(cmd, cwd, timeout, on_start=None, **kwargs):
            def started(proc):
                on_start(proc)
                conn, run = live_run(self.db)
                try:
                    store.abort(conn, run, NOTE)
                finally:
                    conn.close()
                seen["asked"] = time.monotonic()
            code, out = real(cmd, cwd, timeout, on_start=started, **kwargs)
            seen["elapsed"], seen["code"] = time.monotonic() - seen["asked"], code
            return code, out

        def dispatch(target, role, goal, cwd, **kwargs):
            if role == IMPLEMENT:
                return implementer(target, role, goal, cwd, **kwargs)
            return agents.agent(target, role, goal, cwd, **kwargs)

        with patch.object(agents, "run_capped", run_capped), \
                patch.object(sys, "stdout", io.StringIO()):
            self.loop(fake=dispatch,
                      provider=StubProvider(dict(a_task(), body=VALID_BODY)))
        self.assertEqual(seen["code"], -signal.SIGKILL)
        self.assertLess(seen["elapsed"], beat_s + 1)
        self.assertEqual(self.read("SELECT outcome, outcomeReason FROM runs"),
                         [("abandoned", NOTE)])

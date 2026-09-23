"""Configured review turns keep a live run's heartbeat moving (KO-443)."""
import subprocess
import sys
from unittest.mock import patch

import holophyte.agents
import holophyte.runs
import store
import store.tickets
from tests.config_fixture import ConfigTestCase
from tests.heartbeat_fixture import LOADED_MS, patch_beats

ROLES = (("review", "reviewer"), ("adjudicate", "adjudicator"))


class ConfiguredHeartbeatTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.locate()
        self.repo = self.target
        for args in (("init", "-q", "-b", "main"),
                     ("-c", "user.name=test", "-c", "user.email=test@example.com",
                      "commit", "-qm", "candidate", "--allow-empty")):
            subprocess.run(["git", *args], cwd=self.repo, check=True,
                           capture_output=True)
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                  check=True, capture_output=True,
                                  text=True).stdout.strip()
        self.command = self.repo / "slow-review.py"
        # Sample the real store from the child: heartbeat() intentionally
        # updates lastHeartbeat without emitting narrative runEvents rows.
        # It waits for two beats, or for one 3 s stale window without them,
        # so a busy runner's slow beats are waited for, not missed (KO-674).
        self.command.write_text(
            "import sqlite3, time\n"
            "conn = sqlite3.connect('store.db')\n"
            "start = int(time.time() * 1000)\n"
            "deadline = time.monotonic() + 3\n"
            "beats = set()\n"
            "while len(beats) < 2 and time.monotonic() < deadline:\n"
            "    time.sleep(0.05)\n"
            "    beat, = conn.execute('SELECT lastHeartbeat FROM runs "
            "WHERE endedAt IS NULL').fetchone()\n"
            "    if beat > start:\n"
            "        beats.add(beat)\n"
            "print(len(beats))\n"
            "print('VERDICT: APPROVE')\n")
        self.conn = store.open(self.repo / "store.db")
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project = store.tickets.ensure_project(
            self.conn, "team", str(self.repo))
        # The window is 3 s so no beat judged against it is late, and the
        # beat every 30 ms rather than every 1.5 s, as in
        # `tests/test_claim_lock_wait.py`, so two beats take a moment.
        beat = holophyte.runs.heartbeat_while
        self.enterContext(patch.object(
            holophyte.runs, "heartbeat_while",
            lambda conn, run_id, interval_s, on_swept=None:
                beat(conn, run_id, 0.03, on_swept)))

    def beats_during(self, role, key):
        """Run a configured `role` turn; return the beats its command saw."""
        self.locate(f'[agents]\n{key} = "{sys.executable} {self.command}"\n'
                    '[supervisor]\nheartbeat_stale_min = 0.05\n')
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project, role, role, "Review heartbeat",
            acceptance_criteria=["beats during the command"],
            verification_commands=["true"])
        run_id = store.claim(self.conn, self.project, ticket)
        reply = holophyte.agents.agent(
            self.tgt, role, "review it", self.repo,
            base_sha=self.sha, candidate_sha=self.sha,
            conn=self.conn, run_id=run_id)
        beats, verdict = reply.splitlines()
        self.assertEqual(verdict, "VERDICT: APPROVE")
        store.release(self.conn, run_id, "failed", "test complete")
        return int(beats)

    def assert_beat(self, beats):
        self.assertGreaterEqual(beats, 2)

    def test_configured_review_and_adjudication_heartbeat_during_command(self):
        for role, key in ROLES:
            with self.subTest(role=role):
                self.assert_beat(self.beats_during(role, key))

    def test_beats_each_late_on_a_loaded_runner_still_count(self):
        patch_beats(self, LOADED_MS)
        for role, key in ROLES:
            with self.subTest(role=role):
                self.assert_beat(self.beats_during(role, key))

    def test_a_silent_heartbeat_during_the_command_fails_the_check(self):
        patch_beats(self, silent=True)
        beats = self.beats_during(*ROLES[0])
        with self.assertRaises(AssertionError):
            self.assert_beat(beats)

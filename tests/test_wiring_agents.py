"""Configured review turns keep a live run's heartbeat moving (KO-443)."""
import subprocess
import sys

from config_fixture import ConfigTestCase

import holophyte.agents
import store
import store.tickets


class ConfiguredHeartbeatTests(ConfigTestCase):
    def test_configured_review_and_adjudication_heartbeat_during_command(self):
        self.locate()
        root = self.target
        for args in (("init", "-q", "-b", "main"),
                     ("-c", "user.name=test", "-c", "user.email=test@example.com",
                      "commit", "-qm", "candidate", "--allow-empty")):
            subprocess.run(["git", *args], cwd=root, check=True,
                           capture_output=True)
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                             check=True, capture_output=True,
                             text=True).stdout.strip()
        command = root / "slow-review.py"
        # Sample the real store from the child: heartbeat() intentionally
        # updates lastHeartbeat without emitting narrative runEvents rows.
        command.write_text(
            "import sqlite3, time\n"
            "conn = sqlite3.connect('store.db')\n"
            "start = int(time.time() * 1000)\n"
            "beats = set()\n"
            "for _ in range(9):\n"
            "    time.sleep(0.1)\n"  # Three 0.3-second beat intervals.
            "    beat, = conn.execute('SELECT lastHeartbeat FROM runs "
            "WHERE endedAt IS NULL').fetchone()\n"
            "    if beat > start:\n"
            "        beats.add(beat)\n"
            "print(len(beats))\n"
            "print('VERDICT: APPROVE')\n")
        conn = store.open(root / "store.db")
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team", str(root))
        for role, key in (("review", "reviewer"),
                          ("adjudicate", "adjudicator")):
            with self.subTest(role=role):
                self.locate(
                    f'[agents]\n{key} = "{sys.executable} {command}"\n'
                    '[supervisor]\nheartbeat_stale_min = 0.01\n')
                ticket = store.tickets.mirror_ticket(
                    conn, project, role, role, "Review heartbeat",
                    acceptance_criteria=["beats during the command"],
                    verification_commands=["true"])
                run_id = store.claim(conn, project, ticket)
                reply = holophyte.agents.agent(
                    self.tgt, role, "review it", root,
                    base_sha=sha, candidate_sha=sha,
                    conn=conn, run_id=run_id)
                beats, verdict = reply.splitlines()
                self.assertEqual(verdict, "VERDICT: APPROVE")
                self.assertGreaterEqual(int(beats), 2)
                store.release(conn, run_id, "failed", "test complete")

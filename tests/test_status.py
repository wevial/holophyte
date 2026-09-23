"""`factory.py TARGET --status [--json]`: what the factory is doing right now
(KO-596), read off a seeded store and the target's two lock files.

Run: python3 -m unittest discover -s tests -p 'test_status.py' -v
"""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from unittest.mock import patch

import holophyte.cli
import holophyte.status
import store
import store.schema
import store.tickets
from holophyte.gates import merge_lock_path
from holophyte.supervisor_lock import supervisor_lock_path
from tests.phase_fixture import park_run
from tests.sweep_fixture import MINUTE, T0, SweepTestCase, Tripwire, no_network

QUESTION = "Which branch is canonical?"


class StatusTests(SweepTestCase):
    """One enabled project, a run verifying, a run parked on a question and
    two ready tickets -- the store the acceptance criteria name."""

    def setUp(self):
        super().setUp()
        self.live = self.a_run(phase="verifying", claimed_at=T0)
        self.heartbeat_at(self.live, T0 + MINUTE)
        self.parked = self.a_run(phase="working", claimed_at=T0)
        ticket = self.ticket_of[self.parked]
        store.tickets.transition(self.conn, ticket, "blocked_on_operator")
        self.conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                          (QUESTION, ticket))
        self.conn.commit()
        park_run(self.conn, self.parked, "blocked_on_operator",
                 "asked the operator", now=T0 + MINUTE)
        for n in (101, 102):
            store.tickets.mirror_ticket(
                self.conn, self.project, linear_issue_id=f"issue-{n}",
                linear_identifier=f"KO-{n}", title=f"ticket {n}",
                acceptance_criteria=["Given it, then it is done"],
                verification_commands=["echo ok"])

    def status(self, *flags, at=T0 + 3 * MINUTE):
        """The mode end to end, with the board and the network as tripwires."""
        holophyte.cli.eager_import()
        out = io.StringIO()
        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}), \
                no_network(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.status, "time", lambda: at / 1000):
            code = holophyte.cli.cli([str(self.target), "--status", *flags])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_json_is_one_object_witnessing_each_field(self):
        snap = json.loads(self.status("--json"))
        self.assertEqual(snap["schema_version"], store.schema.SCHEMA_VERSION)
        self.assertEqual(snap["projects"], [{
            "path": str(self.target.resolve()), "admission": "enabled",
            "hold_note": None}])
        [live] = snap["live"]
        self.assertEqual(
            {key: live[key] for key in ("run", "ticket", "phase",
                                        "heartbeat_age_s")},
            {"run": self.live, "ticket": "KO-1", "phase": "verifying",
             "heartbeat_age_s": 120})
        self.assertIn("worker", live)
        self.assertEqual(snap["parked"], [{"run": self.parked,
                                           "ticket": "KO-2",
                                           "question": QUESTION}])
        self.assertEqual(snap["ready"], 2)
        self.assertIsNone(snap["supervisor_lock"])
        self.assertIsNone(snap["merge_lock"])

    def test_text_names_project_live_run_parked_question_and_ready(self):
        lines = self.status().splitlines()
        self.assertIn(f"project {self.target.resolve()} enabled", lines)
        live = [line for line in lines if line.startswith("live ")]
        self.assertEqual(len(live), 1)
        self.assertTrue(live[0].startswith(f"live KO-1 run {self.live} verifying"))
        self.assertIn(f"parked KO-2 run {self.parked}: {QUESTION}", lines)
        self.assertIn("ready 2", lines)

    def test_locks_name_merge_holder_and_stale_supervisor(self):
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        supervisor_lock_path(self.tgt).write_text(f"host-a {dead.pid} {T0}\n")
        merge_lock_path(self.tgt).write_text(f"{self.live} {T0 / 1000}\n")
        conn = store.read.open_readonly(self.db)
        self.addCleanup(conn.close)
        snap = holophyte.status.snapshot(self.tgt, conn, now=T0)
        self.assertEqual(snap["merge_lock"], {"run": self.live, "stale": False})
        self.assertEqual(snap["supervisor_lock"],
                         {"pid": dead.pid, "stale": True})
        lines = holophyte.status.render(snap)
        self.assertIn(f"merge lock: held, run {self.live}", lines)
        self.assertIn(f"supervisor lock: stale, pid {dead.pid}", lines)

    def test_store_file_is_unchanged_by_the_command(self):
        self.conn.close()
        files = [path for path in (self.db, self.db.with_name("store.db-wal"))
                 if path.exists()]

        def digests():
            return [hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in files]

        before = digests()
        self.status()
        self.status("--json")
        self.assertEqual(digests(), before)

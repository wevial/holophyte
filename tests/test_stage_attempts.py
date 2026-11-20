"""The stage-attempt record: the additive migration that adds it, its
constrained outcome and edge, and what `close_stage` sums from the turns of
the roles an attempt owns.

Run: python3 -m unittest discover -s tests -p 'test_stage_attempts.py' -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store  # noqa: E402 - after the sys.path insert above
import store.schema  # noqa: E402 - after the sys.path insert above
from store import stages  # noqa: E402 - after the sys.path insert above


def a_run(conn):
    project = store.ensure_project(conn, "team-1", "/repo-1")
    ticket = store.mirror_ticket(conn, project, linear_issue_id="issue-1",
                                 linear_identifier="KO-1", title="candidate")
    return store.claim(conn, project, ticket)


def a_turn(conn, run_id, role, seconds, usage):
    store.record_event(conn, run_id, "agent_turn", f"{role} turn ended",
                       level="detail", payload=json.dumps(
                           {"role": role, "seconds": seconds, "usage": usage}))


class StageAttemptMigrationTests(unittest.TestCase):
    def test_a_previous_version_store_gains_the_constrained_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = store.open(path)
            run = a_run(conn)
            conn.close()
            raw = sqlite3.connect(path)
            raw.executescript("DROP TABLE stageAttempts;\n"
                              "PRAGMA user_version = 72;\n")
            raw.close()

            conn = store.open(path)
            self.addCleanup(conn.close)

            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                             (store.schema.SCHEMA_VERSION,))
            self.assertEqual(store.schema.SCHEMA_VERSION, 73)
            self.assertEqual(store.schema.READABLE_FROM, 50)
            insert = ("INSERT INTO stageAttempts (runId, stageId, attempt,"
                      " startedAt, openSeq, outcome, edge)"
                      " VALUES (?, 'review', ?, 0, 0, ?, ?)")
            for n, outcome in enumerate(("pass", "fail", "skipped", "error"), 1):
                conn.execute(insert, (run, n, outcome, "next"))
            for n, edge in enumerate(("next", "on_fail", "again", "park",
                                      "end"), 5):
                conn.execute(insert, (run, n, "fail", edge))
            for outcome, edge in (("passed", "next"), ("pass", "retry")):
                with self.subTest(outcome=outcome, edge=edge), \
                        self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(insert, (run, 20, outcome, edge))


class CloseStageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conn = store.open(Path(directory.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.run_id = a_run(self.conn)

    def attempt(self, attempt_id):
        return self.conn.execute(
            "SELECT stageId, attempt, agentSeconds, inputTokens, outputTokens,"
            " costUsd, outcome, edge FROM stageAttempts WHERE id = ?",
            (attempt_id,)).fetchone()

    def test_an_attempt_sums_only_its_own_roles_turns_since_it_opened(self):
        a_turn(self.conn, self.run_id, "implement", 99,
               {"input_tokens": 7, "output_tokens": 7, "cost_usd": 9.0})
        first = stages.open_stage(self.conn, self.run_id, "implement", "a" * 40)
        a_turn(self.conn, self.run_id, "implement", 30,
               {"input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.40})
        a_turn(self.conn, self.run_id, "review", 50,
               {"input_tokens": 5000, "output_tokens": 800, "cost_usd": 2.5})

        stages.close_stage(self.conn, first, "pass", "next", "b" * 40,
                           roles=["implement"])
        second = stages.open_stage(self.conn, self.run_id, "implement")

        self.assertEqual(self.attempt(first),
                         ("implement", 1, 30, 1200, 300, 0.40, "pass", "next"))
        self.assertEqual(self.attempt(second)[:2], ("implement", 2))

    def test_turns_that_report_no_cost_leave_the_cost_null(self):
        attempt = stages.open_stage(self.conn, self.run_id, "implement")
        a_turn(self.conn, self.run_id, "implement", 12,
               {"input_tokens": 10, "output_tokens": 4})

        stages.close_stage(self.conn, attempt, "pass", "next",
                           roles=["implement"])

        self.assertEqual(self.attempt(attempt)[2:6], (12, 10, 4, None))

    def test_the_writers_keep_the_times_they_are_given(self):
        attempt = stages.open_stage(self.conn, self.run_id, "implement", now=10)
        a_turn(self.conn, self.run_id, "implement", 5, {"input_tokens": 3})
        stages.close_stage(self.conn, attempt, "pass", "next",
                           roles=(role for role in ["implement"]), now=20)
        skipped = stages.skip_stage(self.conn, self.run_id, "trim", "off",
                                    now=30)

        self.assertEqual(self.conn.execute(
            "SELECT id, startedAt, endedAt, inputTokens FROM stageAttempts"
            " ORDER BY id").fetchall(),
            [(attempt, 10, 20, 3), (skipped, 30, 30, None)])

    def test_an_attempt_is_closed_once(self):
        attempt = stages.open_stage(self.conn, self.run_id, "verify")
        stages.close_stage(self.conn, attempt, "fail", "next")

        with self.assertRaises(ValueError):
            stages.close_stage(self.conn, attempt, "pass", "next")
        self.assertEqual(self.attempt(attempt)[6:], ("fail", "next"))


if __name__ == "__main__":
    unittest.main()

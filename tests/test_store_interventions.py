"""Operator surface: `record_intervention()` and `walk_ticket()`.

The KO-146 incident produced four falsely-labeled 'resume' interventions and
raw SQL because the schema offered no truthful action for an operator
close-out and `resume()` was the table's only writer — the schema made
honesty impossible. These tests pin the general writer (row plus narrative
event, atomic), the CHECK-widening rebuild an older store needs before it
can hold a 'close_out' row, and the §3 walk helper that replaces hand-found
status paths.

Run: python3 -m unittest discover -s tests -p 'test_store_interventions*' -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import store

MINUTE = 60 * 1000
T0 = 1_700_000_000_000

# `interventions` exactly as it shipped before 'close_out' joined the action
# CHECK, kept verbatim rather than derived from store.SCHEMA: the point of
# the migration test is that init() carries a real older store forward.
LEGACY_INTERVENTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS interventions (
    id        INTEGER PRIMARY KEY,
    runId     INTEGER NOT NULL REFERENCES runs (id),
    source    TEXT    NOT NULL CHECK (source IN ('supervisor', 'human')),
    "trigger" TEXT    NOT NULL
        CHECK ("trigger" IN ('time_box', 'off_criteria', 'looping',
                             'review_stuck', 'linear_cancelled', 'manual')),
    "action"  TEXT    NOT NULL
        CHECK ("action" IN ('redirect', 'kill', 'extend_time_box', 'resume')),
    question  TEXT,
    guidance  TEXT,
    at        INTEGER NOT NULL
);
"""


class InterventionFixture(unittest.TestCase):
    """A store with one project, one in-flight ticket, and its claimed run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.conn = store.open(str(self.root / "store.sqlite3"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project = store.ensure_project(self.conn, "team-1",
                                            self.root / "repo")
        self.ticket = self.a_ticket("KO-1")
        store.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, self.project, self.ticket, now=T0)

    def a_ticket(self, identifier):
        return store.mirror_ticket(
            self.conn, self.project, linear_issue_id=f"issue-{identifier}",
            linear_identifier=identifier, title=f"ticket {identifier}",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)

    def rows(self, sql):
        return self.conn.execute(sql).fetchall()


class RecordInterventionTests(InterventionFixture):
    def test_a_close_out_writes_the_row_and_its_narrative_event(self):
        rid = store.record_intervention(
            self.conn, self.run, "close_out",
            "operator closed the run out by hand", now=T0 + MINUTE)

        (row,) = self.rows(
            'SELECT runId, source, "trigger", "action", at FROM interventions')
        self.assertEqual(row, (self.run, "human", "manual", "close_out",
                               T0 + MINUTE))
        self.assertIsInstance(rid, int)
        (summary,) = [s for (s,) in self.rows(
            "SELECT summary FROM runEvents WHERE kind = 'intervention'")]
        self.assertIn("human close_out", summary)
        self.assertIn("operator closed the run out by hand", summary)

    def test_a_failed_validation_writes_nothing(self):
        for bad in (
            dict(action="obliterate", note="x"),
            dict(action="close_out", note="x", trigger="whim"),
            dict(action="close_out", note="x", source="ghost"),
            dict(action="close_out", note="   "),
        ):
            with self.assertRaises(ValueError):
                store.record_intervention(self.conn, self.run, **bad)
        with self.assertRaises(ValueError):
            store.record_intervention(self.conn, 999, "close_out", "no run")

        self.assertEqual(self.rows("SELECT * FROM interventions"), [])
        self.assertEqual(
            self.rows("SELECT * FROM runEvents WHERE kind = 'intervention'"),
            [])

    def test_a_supervisor_source_is_recorded_as_such(self):
        store.record_intervention(self.conn, self.run, "close_out",
                                  "swept", source="supervisor")

        self.assertEqual(self.rows("SELECT source FROM interventions"),
                         [("supervisor",)])


class MigrationTests(InterventionFixture):
    def test_an_older_store_is_rebuilt_to_accept_close_out(self):
        """A store whose action CHECK predates 'close_out' would refuse the
        row forever — CREATE IF NOT EXISTS never touches an existing table
        and SQLite cannot ALTER a CHECK. init() rebuilds it, keeping the
        rows already there."""
        # Regress this store's interventions table to the legacy DDL, with
        # one legacy row in it — the upgrade as it actually happens.
        self.conn.execute("DROP TABLE interventions")
        self.conn.executescript(LEGACY_INTERVENTIONS_TABLE)
        self.conn.execute(
            'INSERT INTO interventions (runId, source, "trigger", "action", at)'
            " VALUES (?, 'human', 'manual', 'resume', ?)", (self.run, T0))
        self.conn.commit()

        store.init(self.conn)
        store.record_intervention(self.conn, self.run, "close_out", "repair")

        self.assertEqual(
            self.rows('SELECT "action" FROM interventions ORDER BY id'),
            [("resume",), ("close_out",)])

    def test_a_second_init_leaves_the_rebuilt_table_alone(self):
        before = self.rows("SELECT sql FROM sqlite_master"
                           " WHERE name = 'interventions'")

        store.init(self.conn)

        self.assertEqual(
            self.rows("SELECT sql FROM sqlite_master"
                      " WHERE name = 'interventions'"), before)


class WalkTicketTests(InterventionFixture):
    def test_the_walk_takes_the_shortest_legal_path(self):
        ticket = self.a_ticket("KO-2")  # ready

        path = store.walk_ticket(self.conn, ticket, "merged")

        self.assertEqual(path, ("in_flight", "merged"))
        self.assertEqual(
            self.rows(f"SELECT status FROM tickets WHERE id = {ticket}"),
            [("merged",)])

    def test_a_walk_with_no_path_raises_and_writes_nothing(self):
        ticket = self.a_ticket("KO-3")
        store.walk_ticket(self.conn, ticket, "merged")

        with self.assertRaises(store.IllegalTransition):
            store.walk_ticket(self.conn, ticket, "ready")

        self.assertEqual(
            self.rows(f"SELECT status FROM tickets WHERE id = {ticket}"),
            [("merged",)])

    def test_walking_to_the_current_status_is_a_no_op(self):
        ticket = self.a_ticket("KO-4")

        self.assertEqual(store.walk_ticket(self.conn, ticket, "ready"), ())

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(store.IllegalTransition):
            store.walk_ticket(self.conn, self.ticket, "done")


if __name__ == "__main__":
    unittest.main()

"""Pickability contract for the v2 store (docs/v2/state-model.md §2).

The predicate the loop's gate asks about a ticket::

    status == 'ready'
      && activeRunId == null
      && acceptanceCriteria.length > 0
      && verificationCommands.length > 0
      && all(dependsOn).status == 'merged'

Each test starts from one ticket that satisfies every clause and breaks
exactly one of them, so a clause that stopped being checked shows up as a
single failure rather than being masked by its neighbours. The expectations
are read off the predicate above, not off `store.pickable()`'s branches.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import store


class PickabilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = self.add_project("team_abc")
        self.ticket_id = self.mirror("iss_1")
        self.conn.commit()

    def add_project(self, linear_team_id):
        return self.conn.execute(
            "INSERT INTO projects"
            " (linearTeamId, repoPath, defaultBranch, autonomyProfile)"
            " VALUES (?, '/repos/holophyte', 'main', 'personal')",
            (linear_team_id,),
        ).lastrowid

    def mirror(self, linear_issue_id, project_id=None, **kwargs):
        """Mirror a fully specced ticket: `ready`, criteria, commands, no run."""
        fields = {
            "linear_identifier": "HOL-1",
            "title": "a ticket",
            "acceptance_criteria": ["given/when/then"],
            "verification_commands": ["python3 -m unittest discover tests"],
        }
        fields.update(kwargs)
        return store.mirror_ticket(
            self.conn,
            self.project_id if project_id is None else project_id,
            linear_issue_id,
            **fields,
        )

    def set_column(self, ticket_id, column, value):
        # Written straight to the column: the point of several of these tests
        # is that the predicate re-reads the data rather than trusting the
        # status a writer left behind.
        self.conn.execute(
            f"UPDATE tickets SET {column} = ? WHERE id = ?", (value, ticket_id)
        )
        self.conn.commit()

    def merge(self, ticket_id):
        store.transition(self.conn, ticket_id, "in_flight")
        store.transition(self.conn, ticket_id, "merged")

    # --- the clauses, one at a time -------------------------------------

    def test_fully_specced_ready_ticket_is_pickable(self):
        verdict = store.pickable(self.conn, self.ticket_id)
        self.assertTrue(verdict)
        self.assertIsNone(verdict.reason)

    def test_not_pickable_without_verification_commands(self):
        self.set_column(self.ticket_id, "verificationCommands", "[]")
        self.assertFalse(store.pickable(self.conn, self.ticket_id))

    def test_not_pickable_without_acceptance_criteria(self):
        self.set_column(self.ticket_id, "acceptanceCriteria", "[]")
        self.assertFalse(store.pickable(self.conn, self.ticket_id))

    def test_not_pickable_unless_status_is_ready(self):
        for status in ("needs_spec", "in_flight", "blocked_on_deps",
                       "blocked_on_operator", "merged", "abandoned"):
            with self.subTest(status=status):
                self.set_column(self.ticket_id, "status", status)
                self.assertFalse(store.pickable(self.conn, self.ticket_id))

    def test_not_pickable_while_a_run_is_active(self):
        store.claim(self.conn, self.project_id, self.ticket_id)
        self.assertFalse(store.pickable(self.conn, self.ticket_id))

    def test_dependency_must_be_merged(self):
        dep_id = self.mirror("iss_dep")
        blocked_id = self.mirror("iss_2", depends_on=["iss_dep"])
        self.conn.commit()
        self.assertFalse(store.pickable(self.conn, blocked_id))
        self.merge(dep_id)
        self.assertTrue(store.pickable(self.conn, blocked_id))

    def test_unmirrored_dependency_is_not_pickable(self):
        blocked_id = self.mirror("iss_2", depends_on=["iss_nowhere"])
        self.conn.commit()
        self.assertFalse(store.pickable(self.conn, blocked_id))

    def test_missing_ticket_is_not_pickable(self):
        self.assertFalse(store.pickable(self.conn, self.ticket_id + 1000))


if __name__ == "__main__":
    unittest.main()

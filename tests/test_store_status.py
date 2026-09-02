"""Ticket status contract for the v2 store (docs/v2/state-model.md §2-§3).

Two rules are under test. The routing rule from §2: a ticket lacking
acceptance criteria or a verification command is not pickable, so it is
mirrored into `needs_spec` rather than `ready`. And the §3 diagram: a status
change is legal only if the diagram draws it.

The legal and illegal moves below are transcribed from that diagram by hand,
on purpose — walking `store.TICKET_TRANSITIONS` to generate them would only
prove the module agrees with itself. This transcription is the independent
oracle, and the stored `status` column is what every assertion reads.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import store

# The §3 drawing, read as (from, to) pairs, plus Holophyte's own escalation
# edge `in_flight → blocked_on_operator` (KO-140): a ticket the loop keeps
# failing on is parked for an operator rather than given up on, and §3 draws
# an in-flight ticket no other way to stop being claimed.
#
#     needs_spec → ready → in_flight → merged
#                     ↕         ↓        ↘
#              blocked_on_deps  │   abandoned
#                     ↕         │
#             blocked_on_operator ←┘
LEGAL_EDGES = {
    ("needs_spec", "ready"),
    ("ready", "in_flight"),
    ("in_flight", "merged"),
    ("in_flight", "abandoned"),
    ("in_flight", "blocked_on_operator"),
    ("ready", "blocked_on_deps"),
    ("blocked_on_deps", "ready"),
    ("blocked_on_deps", "blocked_on_operator"),
    ("blocked_on_operator", "blocked_on_deps"),
}
STATUSES = {status for edge in LEGAL_EDGES for status in edge}


class TicketStatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = self.conn.execute(
            "INSERT INTO projects"
            " (linearTeamId, repoPath, defaultBranch, autonomyProfile)"
            " VALUES ('team_abc', '/srv/dev/holophyte', 'main', 'personal')"
        ).lastrowid
        self.conn.commit()

    def mirror(self, linear_issue_id="iss_1", **kwargs):
        fields = {
            "linear_identifier": "HOL-1",
            "title": "a ticket",
            "acceptance_criteria": ["given/when/then"],
            "verification_commands": ["python3 -m unittest discover tests"],
        }
        fields.update(kwargs)
        return store.mirror_ticket(
            self.conn, self.project_id, linear_issue_id, **fields
        )

    def status_of(self, ticket_id):
        return self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()[0]

    def dependencies_of(self, ticket_id):
        return json.loads(self.conn.execute(
            "SELECT dependsOn FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()[0])

    def force_status(self, ticket_id, status):
        self.conn.execute(
            "UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id)
        )
        self.conn.commit()

    # --- §2 routing -----------------------------------------------------

    def test_a_ticket_with_no_verification_commands_is_needs_spec(self):
        ticket_id = self.mirror(verification_commands=[])

        self.assertEqual(self.status_of(ticket_id), "needs_spec")

    def test_a_ticket_with_no_acceptance_criteria_is_needs_spec(self):
        ticket_id = self.mirror(acceptance_criteria=[])

        self.assertEqual(self.status_of(ticket_id), "needs_spec")

    def test_a_re_mirror_that_says_nothing_about_dependencies_keeps_them(self):
        """`depends_on` omitted means "no opinion", not "none": the loop's
        claim-time re-mirror carries only the body, and the dependency list
        it does not carry must survive it or the gate reads an empty one."""
        ticket_id = self.mirror(depends_on=["iss_0"])

        self.mirror(title="edited body")

        self.assertEqual(self.dependencies_of(ticket_id), ["iss_0"])

    def test_a_re_mirror_that_names_dependencies_replaces_them(self):
        ticket_id = self.mirror(depends_on=["iss_0"])

        self.mirror(depends_on=[])

        self.assertEqual(self.dependencies_of(ticket_id), [])

    def test_a_fully_specced_ticket_is_ready_and_mirrors_its_fields(self):
        ticket_id = self.mirror(
            linear_identifier="HOL-142",
            title="Mirror a Linear issue",
            acceptance_criteria=["given x, when y, then z"],
            verification_commands=["python3 -m unittest discover tests"],
            time_box_ms=1_500_000,
            affinity="headless",
            depends_on=["iss_0"],
            now=1700,
        )

        row = self.conn.execute(
            "SELECT projectId, linearIssueId, linearIdentifier, title, status,"
            " acceptanceCriteria, verificationCommands, timeBoxMs, affinity,"
            " dependsOn, mirroredAt FROM tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        self.assertEqual(
            row,
            (
                self.project_id, "iss_1", "HOL-142", "Mirror a Linear issue",
                "ready", json.dumps(["given x, when y, then z"]),
                json.dumps(["python3 -m unittest discover tests"]), 1_500_000,
                "headless", json.dumps(["iss_0"]), 1700,
            ),
        )

    def test_re_mirroring_promotes_needs_spec_once_the_body_is_specced(self):
        ticket_id = self.mirror(acceptance_criteria=[], verification_commands=[])
        self.assertEqual(self.status_of(ticket_id), "needs_spec")

        again = self.mirror(title="now specced")

        self.assertEqual(again, ticket_id)  # upsert, not a second row
        self.assertEqual(self.status_of(ticket_id), "ready")

    def test_re_mirroring_demotes_a_ready_ticket_whose_body_lost_its_spec(self):
        # §2 is a data invariant: `ready` means the row carries both lists.
        # A ready ticket is nobody's work yet, so an edit that empties the
        # body in Linear follows the body back to `needs_spec` rather than
        # leaving a row that says ready about a contract it no longer holds.
        ticket_id = self.mirror()
        self.assertEqual(self.status_of(ticket_id), "ready")

        again = self.mirror(title="edited", acceptance_criteria=[],
                            verification_commands=[])

        self.assertEqual(again, ticket_id)
        self.assertEqual(self.status_of(ticket_id), "needs_spec")

    def test_re_mirroring_does_not_drag_a_running_ticket_backwards(self):
        # §1: Holophyte owns the in-flight substate. An edit that empties the
        # body in Linear must not demote a ticket a run is working on.
        ticket_id = self.mirror()
        self.force_status(ticket_id, "in_flight")

        self.mirror(title="edited", acceptance_criteria=[], verification_commands=[])

        self.assertEqual(self.status_of(ticket_id), "in_flight")

    def test_a_bare_string_of_criteria_is_refused_not_encoded(self):
        # "abc" is iterable and truthy, so encoding it would route an
        # under-specced ticket straight to ready.
        with self.assertRaises(ValueError):
            self.mirror(acceptance_criteria="given x, when y, then z")

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM tickets").fetchone(), (0,)
        )

    # --- §3 transitions -------------------------------------------------

    def test_the_diagrams_legal_path_walks_step_by_step(self):
        ticket_id = self.mirror(acceptance_criteria=[], verification_commands=[])

        walked = [self.status_of(ticket_id)]
        for step in ("ready", "in_flight", "merged"):
            self.assertEqual(store.transition(self.conn, ticket_id, step), walked[-1])
            walked.append(self.status_of(ticket_id))

        self.assertEqual(walked, ["needs_spec", "ready", "in_flight", "merged"])

    def test_a_merged_ticket_cannot_go_back_in_flight(self):
        ticket_id = self.mirror()
        self.force_status(ticket_id, "merged")

        with self.assertRaises(store.IllegalTransition):
            store.transition(self.conn, ticket_id, "in_flight")

        self.assertEqual(self.status_of(ticket_id), "merged")

    def test_every_pair_the_diagram_does_not_draw_is_refused(self):
        ticket_id = self.mirror()
        for from_status in sorted(STATUSES):
            for to_status in sorted(STATUSES | {"nonsense"}):
                if (from_status, to_status) in LEGAL_EDGES:
                    continue
                with self.subTest(edge=(from_status, to_status)):
                    self.force_status(ticket_id, from_status)
                    with self.assertRaises(store.IllegalTransition):
                        store.transition(self.conn, ticket_id, to_status)
                    self.assertEqual(self.status_of(ticket_id), from_status)

    def test_every_pair_the_diagram_draws_is_accepted(self):
        ticket_id = self.mirror()
        for from_status, to_status in sorted(LEGAL_EDGES):
            with self.subTest(edge=(from_status, to_status)):
                self.force_status(ticket_id, from_status)
                store.transition(self.conn, ticket_id, to_status)
                # Also proves the schema's CHECK accepts every §3 status: an
                # UPDATE to one it did not know would raise here instead.
                self.assertEqual(self.status_of(ticket_id), to_status)

    def test_transitioning_a_ticket_that_does_not_exist_is_refused(self):
        with self.assertRaises(store.IllegalTransition):
            store.transition(self.conn, 4242, "ready")

    # --- §1: composable with the delivery transaction -------------------

    def test_a_mirror_and_its_delivery_id_commit_as_one(self):
        # §1 records a delivery id in the same transaction as its effect, and
        # mirroring is what an inbound Linear issue webhook does. A writer
        # that opens its own transaction cannot be that effect at all.
        delivery = store.with_delivery(
            self.conn,
            "delivery_1",
            lambda conn: store.mirror_ticket(
                conn, self.project_id, "iss_9", "HOL-9", "webhook ticket",
                acceptance_criteria=["given/when/then"],
                verification_commands=["python3 -m unittest discover tests"],
            ),
        )

        self.assertFalse(delivery.replayed)
        self.assertEqual(self.status_of(delivery.result), "ready")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM linearDeliveries WHERE deliveryId = ?",
                ("delivery_1",),
            ).fetchone(),
            (1,),
        )

    def test_a_transition_and_its_delivery_id_commit_as_one(self):
        ticket_id = self.mirror()

        delivery = store.with_delivery(
            self.conn,
            "delivery_2",
            lambda conn: store.transition(conn, ticket_id, "in_flight"),
        )

        self.assertEqual(delivery, store.Delivery(replayed=False, result="ready"))
        self.assertEqual(self.status_of(ticket_id), "in_flight")

    def test_a_failed_effect_rolls_back_its_store_writes_and_its_id(self):
        # The other half of atomicity, and the reason a joined writer must not
        # commit at its own boundary: the effect mirrors a ticket and only
        # then makes an illegal move. Neither the new row nor the delivery id
        # may survive, or Linear's redelivery is swallowed as a replay of work
        # that was never done.
        def effect(conn):
            store.mirror_ticket(
                conn, self.project_id, "iss_9", "HOL-9", "webhook ticket",
                acceptance_criteria=["given/when/then"],
                verification_commands=["python3 -m unittest discover tests"],
            )
            store.transition(conn, 4242, "ready")  # no such ticket

        with self.assertRaises(store.IllegalTransition):
            store.with_delivery(self.conn, "delivery_3", effect)

        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM tickets WHERE linearIssueId = 'iss_9'"
            ).fetchone(),
            (0,),
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM linearDeliveries").fetchone(),
            (0,),
        )

    def test_a_failed_commit_leaves_the_connection_clean_for_the_retry(self):
        # `tickets.activeRunId` is DEFERRABLE INITIALLY DEFERRED, so a dangling
        # reference survives the INSERT and only fails at COMMIT, which SQLite
        # leaves the transaction open on. Uncaught there, the failed block's
        # writes stay pending and the next writer joins them instead of
        # starting clean — the retry would commit the state it was meant to
        # replace.
        with self.assertRaises(sqlite3.IntegrityError):
            with store._transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO tickets"
                    " (projectId, linearIssueId, linearIdentifier, title,"
                    "  status, affinity, mirroredAt, activeRunId)"
                    " VALUES (?, 'iss_9', 'HOL-9', 'a ticket', 'ready', 'any',"
                    "         0, 999999)",
                    (self.project_id,),
                )

        self.assertFalse(self.conn.in_transaction)

        ticket_id = self.mirror("iss_9")

        self.assertEqual(self.status_of(ticket_id), "ready")
        self.assertEqual(
            self.conn.execute(
                "SELECT activeRunId FROM tickets WHERE id = ?", (ticket_id,)
            ).fetchone(),
            (None,),
        )


if __name__ == "__main__":
    unittest.main()

"""Board edits can repair a refused mirror without starting a loop."""
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import T0, SweepTestCase  # noqa: E402
from test_provider import FakeLinear, ticket_body  # noqa: E402

import linear_provider  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.supervisor import board_ready  # noqa: E402


class FakeProvider:
    def __init__(self, body, updated_at):
        self.body = body
        self.updated_at = updated_at
        self.asks = 0

    def ready_issues(self):
        self.asks += 1
        task = linear_provider.parse_task({
            "identifier": "KO-1", "id": "issue-1", "title": "do the thing",
            "description": self.body})
        return [dict(task, updatedAt=self.updated_at)]


class SupervisorBoardTests(SweepTestCase):
    def seed(self, status="needs_spec"):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project, "issue-1", "KO-1", "do the thing",
            body="unfinished", now=T0)
        store.tickets.walk_ticket(self.conn, ticket, status)
        return ticket

    def row(self):
        return self.conn.execute("SELECT * FROM tickets").fetchone()

    def ask(self, provider, now=T0):
        out = io.StringIO()
        with patch("holophyte.supervisor.linear_budget_low", return_value=False):
            owed = board_ready(self.conn, self.project, provider, out,
                               now=now, board_ask_ms=1000)
        return owed, out.getvalue()

    def test_fixed_body_is_mirrored_and_owed_after_throttle(self):
        for status in ("needs_spec", "blocked_on_deps"):
            with self.subTest(status=status):
                self.conn.execute("DELETE FROM tickets")
                self.conn.execute("UPDATE projects SET boardAskedAt = NULL")
                self.conn.commit()
                self.seed(status)
                provider = FakeProvider("unfinished", T0)
                before = self.row()
                self.assertEqual(self.ask(provider), (0, ""))
                self.assertEqual(self.row(), before)
                provider.body = ticket_body()
                provider.updated_at = T0 + 1
                self.assertEqual(self.ask(provider, T0 + 500), (0, ""))
                self.assertEqual(provider.asks, 1)
                owed, output = self.ask(provider, T0 + 1000)
                self.assertEqual(owed, 1)
                self.assertEqual(
                    output.strip(),
                    f"[holo2] re-mirrored KO-1: {status} -> ready")
                self.assertEqual(self.conn.execute(
                    "SELECT status, body FROM tickets").fetchone(),
                    ("ready", provider.body))
                self.assertEqual(provider.asks, 2)

    def test_unchanged_and_owned_rows_are_untouched(self):
        for status, updated in (("needs_spec", T0), ("needs_spec", T0 - 1),
                                ("in_flight", T0 + 1),
                                ("blocked_on_operator", T0 + 1),
                                ("merged", T0 + 1)):
            with self.subTest(status=status, updated=updated):
                self.conn.execute("DELETE FROM tickets")
                self.conn.execute("UPDATE projects SET boardAskedAt = NULL")
                self.conn.commit()
                self.seed(status)
                before = self.row()
                self.assertEqual(
                    self.ask(FakeProvider(ticket_body(), updated)), (0, ""))
                self.assertEqual(self.row(), before)

    def test_ready_listing_exposes_updated_at_in_epoch_milliseconds(self):
        board = FakeLinear()
        board.add("KO-1", "do the thing", ticket_body())
        board.issues["KO-1"]["updatedAt"] = "2023-11-14T22:13:20.123Z"
        with patch.object(linear_provider, "_gql", board.gql):
            self.assertEqual(linear_provider.ready_issues("project")[0]["updatedAt"],
                             1_700_000_000_123)
        self.assertTrue(any("updatedAt" in query for query, _ in board.calls))

    def test_changed_body_still_failing_validation_is_not_owed(self):
        self.seed()
        body = ticket_body().replace("## Summary", "## Missing summary")
        owed, output = self.ask(FakeProvider(body, T0 + 1))
        self.assertEqual(owed, 0)
        self.assertIn("re-mirrored KO-1: needs_spec -> needs_spec", output)
        self.assertEqual(self.conn.execute(
            "SELECT status, body FROM tickets").fetchone(), ("needs_spec", body))

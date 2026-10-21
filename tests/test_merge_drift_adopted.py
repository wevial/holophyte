import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import LoopFixture  # noqa: E402

import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.steer_notes  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board.projection import merge_drift  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from provider import board_for  # noqa: E402
from tests.test_native_loop import NATIVE, no_linear  # noqa: E402
from tests.test_steer_revision import CLAIMED, REVISED  # noqa: E402
from tests.test_ticket_criteria_rows import ticket_body  # noqa: E402

V2 = "python3 -m unittest test_table_export\nruff check ."


class MergeDriftAdoptedTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(NATIVE)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        project = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)
        self.key = store.board.file_ticket(self.conn, project, "NAT",
                                           ticket_body(CLAIMED))
        (ticket,) = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (self.key,)).fetchone()
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run_id = store.claim(self.conn, project, ticket)
        edited = int(time.time() * 1000) + 1000
        revised = ticket_body(REVISED).replace(
            "python3 -m unittest test_orders_export\nruff check .", V2)
        store.board.edit_ticket(self.conn, project, self.key, revised,
                                expected_revision=1, now=edited)
        store.steer_notes.steer(
            self.conn, ticket, "Meet revision 2's acceptance criteria",
            "maintainer", now=edited + 1000)

    def test_a_run_on_its_adopted_revision_has_not_drifted(self):
        drift, live = merge_drift(self.conn, self.run_id, self.board, self.key)
        self.assertEqual(drift, ())
        self.assertEqual(live["criteria"], REVISED)

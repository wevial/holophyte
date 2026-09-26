"""KO-763: the host daemon files, moves and cancels a native ticket at
`POST /projects/NAME/tickets`, `.../tickets/ID/move` and
`.../tickets/ID/cancel`, behind the edit route's gate, authored `console`.

A native and a Linear project are registered under a temporary home, each
a real git repository with a real store, and answered by a real
`HostServer` on an ephemeral loopback port.

Run: python3 -m unittest discover -s tests -p 'test_serve_board_writes.py' -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.project import Project  # noqa: E402
from tests.host_fixture import HostFixture  # noqa: E402
from tests.test_serve_board import NATIVE, ServeBoardCase  # noqa: E402
from tests.test_store_board import body  # noqa: E402


class ServeBoardWriteTests(ServeBoardCase):
    """`native`, an empty native board keyed `NAT`, and `linear`."""

    def setUp(self):
        HostFixture.setUp(self)
        self.paths = {"native": self.repo("native"),
                      "linear": self.repo("linear")}
        Project.locate(self.paths["native"]).config_path.write_text(NATIVE)
        for path in self.paths.values():
            self.cli("project", "add", str(path))

    def post(self, path, payload, project="native", **kw):
        status, answer, _ = self.request(
            "POST", f"/projects/{project}/tickets{path}", payload, **kw)
        return status, answer

    def row(self, identifier):
        with self.store("native") as (conn, _):
            return conn.execute(
                "SELECT boardColumn, priority, revision FROM tickets"
                " WHERE linearIdentifier = ?", (identifier,)).fetchone()

    def test_a_filing_lands_in_its_column_as_console_or_is_422(self):
        self.start_host()

        self.assertEqual(
            self.post("", {"body": body("Console title"), "priority": 2},
                      revision=None),
            (201, {"ticket": "NAT-1", "revision": 1}))
        self.assertEqual(self.row("NAT-1"), ("ready", 2, 1))
        self.assertEqual(self.revisions("native", "NAT-1"),
                         [(1, "console", "Console title")])

        self.assertEqual(
            self.post("", {"body": body("Later"), "column": "backlog"},
                      revision=None),
            (201, {"ticket": "NAT-2", "revision": 1}))
        self.assertEqual(self.row("NAT-2")[0], "backlog")

        status, answer = self.post(
            "", {"body": body("Bad", what=False)}, revision=None)
        self.assertEqual(status, 422)
        self.assertTrue(answer["problems"])
        self.assertIsNone(self.row("NAT-3"))

    def test_a_move_at_the_read_revision_lands_and_a_stale_one_is_409(self):
        self.start_host()
        self.post("", {"body": body()}, revision=None)

        self.assertEqual(
            self.post("/NAT-1/move", {"column": "backlog"}, revision="1"),
            (200, {"ticket": "NAT-1", "revision": 2}))
        self.assertEqual(self.row("NAT-1")[0], "backlog")

        status, answer = self.post("/NAT-1/move", {"column": "backlog"},
                                   revision="1")
        self.assertEqual(status, 409)
        self.assertEqual(answer["current"], 2)
        self.assertEqual(self.post("/NAT-1/move", {"column": "ready"},
                                   revision=None)[0], 428)

    def test_a_cancel_answers_the_run_it_aborts_and_needs_a_note(self):
        self.start_host()
        for _ in range(3):
            self.post("", {"body": body()}, revision=None)
        with self.store("native") as (conn, project):
            (ticket,) = conn.execute(
                "SELECT id FROM tickets WHERE linearIdentifier = 'NAT-3'"
            ).fetchone()
            run = store.claim(conn, project, ticket)
            store.tickets.transition(conn, ticket, "in_flight")
            store.set_phase(conn, run, "working")

        self.assertEqual(self.post("/NAT-3/cancel", {}, revision="1")[0], 400)
        self.assertEqual(self.post("/NAT-3/cancel", {"note": " "},
                                   revision="1")[0], 400)
        self.assertEqual(self.row("NAT-3")[::2], ("ready", 1))

        self.assertEqual(
            self.post("/NAT-3/cancel", {"note": "wrong scope"}, revision="1"),
            (200, {"ticket": "NAT-3", "revision": 2, "run": run}))
        with self.store("native") as (conn, _):
            stop = conn.execute(
                'SELECT i."action", i.guidance FROM runs r JOIN interventions i'
                " ON i.id = r.stopRequested WHERE r.id = ?", (run,)).fetchone()
        self.assertEqual(stop, ("abort", "wrong scope"))

        self.assertEqual(
            self.post("/NAT-2/cancel", {"note": "dup"}, revision="1"),
            (200, {"ticket": "NAT-2", "revision": 2, "run": None}))

    def test_the_gate_refuses_before_anything_is_written(self):
        self.start_host()
        self.post("", {"body": body()}, revision=None)

        self.assertEqual(self.post("", {"body": body()}, token="wrong",
                                   revision=None), (401, {}))
        self.assertEqual(self.post("/NAT-1/move", {"column": "backlog"},
                                   token="wrong", revision="1"), (401, {}))
        self.assertEqual(self.post("", {"body": body()}, project="linear",
                                   revision=None)[0], 404)
        self.assertEqual(self.row("NAT-1"), ("ready", None, 1))
        self.assertIsNone(self.row("NAT-2"))

    def test_a_daemon_with_actions_off_is_404_and_writes_nothing(self):
        self.start_host(actions=False)

        self.assertEqual(self.post("", {"body": body()}, revision=None)[0],
                         404)
        with self.store("native") as (conn, _):
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM tickets").fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()

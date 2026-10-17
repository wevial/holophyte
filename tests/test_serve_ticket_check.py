import contextlib
import json
import unittest
from pathlib import Path

import store.board
import store.read
from holophyte.config.project import Project
from tests.host_fixture import HostFixture
from tests.test_serve_board import NATIVE, ServeBoardCase
from tests.test_store_board import body as ticket_body


def body(*args, **kwargs):
    return ticket_body(*args, **kwargs).replace(
        "python3 -m unittest test_orders_export", "echo ok")


def invalid_body():
    return (body("Steer notes show on the run detail card")
            .replace("Add a CSV export endpoint for the orders list.", "")
            .replace("## Verify command(s)", "- [ ]\n\n## Verify command(s)")
            .replace("Estimate: 25 min", "Estimate: 120 min")
            .replace("Endpoint lives beside the other order routes.",
                     "`holophyte/nope_missing.py`"))


class ServeTicketCheckTests(ServeBoardCase):
    def setUp(self):
        HostFixture.setUp(self)
        self.paths = {"native": self.repo("native"),
                      "linear": self.repo("linear")}
        Project.locate(self.paths["native"]).config_path.write_text(NATIVE)
        for path in self.paths.values():
            self.cli("project", "add", str(path))
        for name in self.paths:
            with self.store(name):
                pass

    def check(self, payload, project="native", **kwargs):
        status, answer, _ = self.request(
            "POST", f"/projects/{project}/tickets/check", payload,
            revision=None, **kwargs)
        return status, answer

    def snapshot(self, name="native"):
        with self.store(name) as (conn, project):
            return (conn.execute("SELECT COUNT(*) FROM tickets").fetchone(),
                    conn.execute("SELECT ticketSeq FROM projects WHERE id = ?",
                                 (project,)).fetchone(),
                    conn.execute("SELECT COUNT(*) FROM ticketRevisions")
                    .fetchone())

    def test_check_matches_pinned_problems_and_filing_without_writes(self):
        self.start_host()
        before = self.snapshot()
        status, answer = self.check({"body": invalid_body()})
        self.assertEqual(status, 200)
        fixture = Path(__file__).parent / "fixtures/serve/ticket-check.json"
        self.assertEqual(answer, json.loads(fixture.read_text()))
        self.assertIs(answer["blocks"], True)
        self.assertEqual(self.snapshot(), before)

        status, filed, _ = self.request(
            "POST", "/projects/native/tickets",
            {"body": invalid_body(), "column": "ready"}, revision=None)
        self.assertEqual(status, 422)
        self.assertEqual(filed["problems"], answer["problems"])
        self.assertEqual(self.snapshot(), before)

    def test_unknown_dependency_blocks_backlog_and_clean_body_does_not(self):
        self.start_host()
        before = self.snapshot()
        status, answer = self.check({"body": body(depends="NAT-9"),
                                     "column": "backlog"})
        self.assertEqual(status, 200)
        self.assertEqual(answer["problems"], [
            "'Depends on' names no ticket of this project: NAT-9",
        ])
        self.assertIs(answer["blocks"], True)
        self.assertEqual(self.check({"body": body()}),
                         (200, {"problems": [], "advisories": [],
                                "blocks": False}))
        self.assertEqual(self.snapshot(), before)

    def test_edit_uses_ticket_column_and_rejects_self_dependency(self):
        with self.store("native") as (conn, project):
            self.assertEqual(store.board.file_ticket(
                conn, project, "NAT", body(), column="backlog"), "NAT-1")
        self.start_host()
        before = self.snapshot()
        text = body().replace("Add a CSV export endpoint for the orders list.",
                              "")
        self.assertEqual(self.check({"body": text, "ticket": "NAT-1",
                                     "column": "ready"}),
                         (200, {"problems": ["'Summary' is empty"],
                                "advisories": [], "blocks": False}))
        status, answer = self.check({
            "body": text.replace("Depends on: none", "Depends on: NAT-1"),
            "ticket": "NAT-1"})
        self.assertEqual(status, 200)
        self.assertEqual(answer["problems"], [
            "'Summary' is empty",
            "'Depends on' names no ticket of this project: NAT-1",
        ])
        self.assertIs(answer["blocks"], True)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.revisions("native", "NAT-1"),
                         [(1, "cli", "Add export endpoint")])

    def test_gate_method_and_invalid_inputs_write_nothing(self):
        self.start_host()
        before = {name: self.snapshot(name) for name in self.paths}
        self.assertEqual(self.check({"body": body()}, token=None)[0], 401)
        self.assertEqual(self.check({"body": body()}, project="linear")[0],
                         404)
        for payload in ({}, {"body": " \n "},
                        {"body": body(), "column": "done"}):
            with self.subTest(payload=payload):
                self.assertEqual(self.check(payload)[0], 400)
        status, _, response = self.request(
            "GET", "/projects/native/tickets/check", revision=None)
        self.assertEqual(status, 405)
        self.assertEqual(response.getheader("Allow"), "POST")
        self.assertEqual({name: self.snapshot(name) for name in self.paths},
                         before)

    def test_disabled_actions_refuse_the_check_without_writes(self):
        self.start_host(actions=False)
        before = self.snapshot()
        self.assertEqual(self.check({"body": body()})[0], 404)
        self.assertEqual(self.snapshot(), before)

    def test_private_text_blocks_even_a_backlog_check(self):
        Project.locate(self.paths["native"]).config_path.write_text(
            NATIVE + '[merge]\nprivate_patterns = ["internal-marker"]\n')
        self.start_host()
        before = self.snapshot()
        status, answer = self.check({"body": body("internal-marker"),
                                     "column": "backlog"})
        self.assertEqual(status, 200)
        self.assertIs(answer["blocks"], True)
        self.assertEqual(answer["problems"], [
            "the title, line 1, holds text the project does not publish "
            "([merge] private_patterns #0)",
        ])
        self.assertNotIn("internal-marker", json.dumps(answer))
        self.assertEqual(self.snapshot(), before)

    def test_store_check_keeps_profile_advisory_without_a_transaction(self):
        target = Project.locate(self.paths["native"])
        with self.store("native") as (_, project):
            pass
        target.config_path.unlink()
        with contextlib.closing(store.read.open_readonly(target.store_path)) as conn:
            statements = []
            conn.set_trace_callback(statements.append)
            answer = store.board.check_ticket(
                conn, project, body(implementer="small"))
            self.assertEqual(answer, {
                "problems": [], "blocks": False,
                "advisories": [
                    "'Estimate & dependencies' line 'Implementer: small': "
                    "profile name not checked; the project has no config file",
                ],
            })
            self.assertFalse(conn.in_transaction)
            self.assertFalse(any(statement.lstrip().upper().startswith("BEGIN")
                                 for statement in statements))

    def test_missing_store_is_unavailable_and_is_not_created(self):
        target = Project.locate(self.paths["native"]).store_path
        target.unlink()
        self.start_host()
        self.assertEqual(self.check({"body": body()})[0], 503)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()

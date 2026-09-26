"""KO-755: `/board` serves each ticket's column, priority, labels and
revision, a `backlog` column first for a native project, and `editable`,
true only when a host daemon with `[serve] actions` on serves a native
project.

A native and a Linear project are registered under a temporary home and
answered by a real `HostServer`; the native store is also answered by a
project daemon with its own actions on.

Run: python3 -m unittest discover -s tests -p 'test_serve_board_fields.py' -v
"""
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holophyte.serve  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.host import Host, settings  # noqa: E402
from holophyte.project import Project  # noqa: E402
from holophyte.serve import BOARD_STATES  # noqa: E402
from holophyte.serve_host import HostServer, host_tokens  # noqa: E402
from tests.host_fixture import HostFixture  # noqa: E402

NOW = 1_750_000_000_000
MACHINE = "machine-token-value"
NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'


class BoardFieldsCase(HostFixture):
    """`native`, a native board, and `linear`, today's, both registered."""

    def setUp(self):
        super().setUp()
        self.paths = {"native": self.repo("native"),
                      "linear": self.repo("linear")}
        Project.locate(self.paths["native"]).config_path.write_text(NATIVE)
        for path in self.paths.values():
            self.cli("project", "add", str(path))
        self.seed("native", "native:NAT", "NAT")
        self.seed("linear", "team-linear", "KO")

    def seed(self, name, team, key):
        """`KEY-1` ready in column `ready`, re-prioritized 4, 3, 2 so its
        revision is 3; `KEY-2` ready in column `backlog`; `KEY-3` in flight
        in column `backlog`."""
        conn = store.open(str(Project.locate(self.paths[name]).store_path))
        try:
            project = store.tickets.ensure_project(conn, team,
                                                   self.paths[name])

            def mirror(n, at, **fields):
                return store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{key}-{n}",
                    linear_identifier=f"{key}-{n}", title=f"ticket {n}",
                    acceptance_criteria=["Given it, then it is worked"],
                    verification_commands=["echo ok"], now=NOW + at,
                    **fields)
            for at, priority in enumerate((4, 3, 2)):
                mirror(1, at, priority=priority, labels=["ui"],
                       board_column="ready")
            mirror(2, 0, board_column="backlog")
            store.tickets.transition(conn, mirror(3, 0, board_column="backlog"),
                                     "in_flight")
        finally:
            conn.close()

    def start_host(self):
        registry = self.home / "host.toml"
        token = self.home / "machine.token"
        token.write_text(MACHINE + "\n")
        token.chmod(0o600)
        registry.write_text(f'[serve]\nmachine_token_file = "{token}"\n'
                            'actions = true\n' + registry.read_text())
        host = Host.locate()
        knobs = settings(host)
        read, write = host_tokens(host, knobs, "127.0.0.1", host.projects())
        server = HostServer(host, knobs, ("127.0.0.1", 0),
                            console_dir=self.root / "no-console",
                            read_token=read, write_token=write)
        self.serve(server)
        self.assertTrue(server.actions)

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def board(self, path, token=MACHINE):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path,
                         headers={"Authorization": f"Bearer {token}"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            body = json.loads(response.read())
        finally:
            conn.close()
        return body, {column["state"]: [t["ticket"] for t in column["tickets"]]
                      for column in body["columns"]}

    @staticmethod
    def fields(body, identifier):
        ticket = next(t for c in body["columns"] for t in c["tickets"]
                      if t["ticket"] == identifier)
        return {key: ticket[key]
                for key in ("column", "priority", "labels", "revision")}


class BoardFieldsTests(BoardFieldsCase):
    def test_a_host_daemon_with_actions_serves_a_native_backlog_editable(self):
        self.start_host()

        body, columns = self.board("/projects/native/board")

        self.assertEqual([c["state"] for c in body["columns"]],
                         ["backlog", "needs_spec", "blocked_on_deps", "ready",
                          "blocked_on_operator", "in_flight"])
        self.assertEqual(columns["backlog"], ["NAT-2"])
        self.assertEqual(columns["ready"], ["NAT-1"])
        # A worked ticket stays under its status whatever its column.
        self.assertEqual(columns["in_flight"], ["NAT-3"])
        self.assertEqual(self.fields(body, "NAT-1"),
                         {"column": "ready", "priority": 2, "labels": ["ui"],
                          "revision": 3})
        self.assertIs(body["editable"], True)

    def test_a_linear_project_keeps_its_columns_and_is_not_editable(self):
        self.start_host()

        body, columns = self.board("/projects/linear/board")

        self.assertEqual([c["state"] for c in body["columns"]],
                         list(BOARD_STATES))
        self.assertEqual(columns["ready"], ["KO-1", "KO-2"])
        self.assertEqual(self.fields(body, "KO-2"),
                         {"column": "backlog", "priority": None, "labels": [],
                          "revision": 1})
        self.assertEqual(self.fields(body, "KO-1")["revision"], 3)
        self.assertIs(body["editable"], False)

    def test_a_project_daemon_with_actions_on_answers_not_editable(self):
        token = self.root / "serve.token"
        token.write_text("project-token\n")
        token.chmod(0o600)
        project = Project.locate(self.paths["native"])
        project.config_path.write_text(
            NATIVE + f'[serve]\ntoken_file = "{token}"\nactions = true\n')
        self.serve(holophyte.serve.make_server(
            project, "127.0.0.1", 0, console_dir=self.root / "no-console",
            token=holophyte.serve.resolve_token(project, "127.0.0.1")))

        body, columns = self.board("/board", token="project-token")

        self.assertEqual(columns["backlog"], ["NAT-2"])
        self.assertIs(body["editable"], False)


if __name__ == "__main__":
    unittest.main()

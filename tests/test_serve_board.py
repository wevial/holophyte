"""KO-758: the host daemon edits a native ticket at
`PUT /projects/NAME/tickets/ID`, behind the machine token alone, host
`[serve] actions` and a native board, at the revision `If-Match` names,
authored `console`; and the CORS preflight allows `If-Match`.

A native and a Linear project are registered under a temporary home, each
a real git repository with a real store, and answered by a real
`HostServer` on an ephemeral loopback port.

Run: python3 -m unittest discover -s tests -p 'test_serve_board.py' -v
"""
import contextlib
import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.host import Host, settings  # noqa: E402
from holophyte.project import Project  # noqa: E402
from holophyte.serve_host import HostServer, host_tokens  # noqa: E402
from tests.host_fixture import HostFixture  # noqa: E402
from tests.test_store_board import body  # noqa: E402

MACHINE = "machine-token-value"
OWN = "project-own-token"
NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'


class ServeBoardCase(HostFixture):
    """`native`, whose `NAT-1` is in column `ready` at revision 2, and
    `linear`, whose `KO-1` is at revision 1, both registered."""

    def setUp(self):
        super().setUp()
        self.paths = {"native": self.repo("native"),
                      "linear": self.repo("linear")}
        own = self.root / "own.token"
        own.write_text(OWN + "\n")
        own.chmod(0o600)
        Project.locate(self.paths["native"]).config_path.write_text(
            NATIVE + f'[serve]\ntoken_file = "{own}"\n')
        for path in self.paths.values():
            self.cli("project", "add", str(path))
        with self.store("native") as (conn, project):
            identifier = store.board.file_ticket(conn, project, "NAT",
                                                 body("First title"))
            store.board.edit_ticket(conn, project, identifier,
                                    body("Second title"), 1)
        with self.store("linear") as (conn, project):
            store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-KO-1",
                linear_identifier="KO-1", title="ticket 1",
                acceptance_criteria=["Given it, then it is worked"],
                verification_commands=["echo ok"])

    @contextlib.contextmanager
    def store(self, name):
        """`(conn, project id)` on the named project's store."""
        path = self.paths[name]
        team = {"native": "native:NAT", "linear": "team-linear"}[name]
        conn = store.open(str(Project.locate(path).store_path))
        try:
            yield conn, store.tickets.ensure_project(conn, team, path)
        finally:
            conn.close()

    def revisions(self, name, identifier):
        """`(revision, author, title)` for each of the ticket's revisions."""
        with self.store(name) as (conn, _):
            return conn.execute(
                "SELECT r.revision, r.author, r.title FROM ticketRevisions r"
                " JOIN tickets t ON t.id = r.ticketId"
                " WHERE t.linearIdentifier = ? ORDER BY r.revision",
                (identifier,)).fetchall()

    def start_host(self, actions=True):
        registry = self.home / "host.toml"
        token = self.home / "machine.token"
        token.write_text(MACHINE + "\n")
        token.chmod(0o600)
        registry.write_text(f'[serve]\nmachine_token_file = "{token}"\n'
                            f'actions = {str(actions).lower()}\n'
                            + registry.read_text())
        host = Host.locate()
        knobs = settings(host)
        read, write = host_tokens(host, knobs, "127.0.0.1", host.projects())
        server = HostServer(host, knobs, ("127.0.0.1", 0),
                            console_dir=self.root / "no-console",
                            read_token=read, write_token=write)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.assertEqual(server.actions, actions)

    def request(self, method, path, payload=None, token=MACHINE,
                revision="2"):
        headers = {"Authorization": f"Bearer {token}",
                   "Content-Type": "application/json"}
        if revision is not None:
            headers["If-Match"] = revision
        data = None if payload is None else json.dumps(payload)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            return (response.status, json.loads(raw) if raw else None,
                    response)
        finally:
            conn.close()

    def edit(self, project="native", identifier="NAT-1", text=None, **kw):
        payload = {"body": text or body("Console title"), "priority": 2,
                   "labels": ["ui"]}
        status, answer, _ = self.request(
            "PUT", f"/projects/{project}/tickets/{identifier}", payload, **kw)
        return status, answer


class ServeBoardTests(ServeBoardCase):
    def test_an_edit_at_the_read_revision_lands_as_console_and_is_gated(self):
        self.start_host()

        self.assertEqual(self.edit(), (200, {"ticket": "NAT-1",
                                             "revision": 3}))
        self.assertEqual(self.revisions("native", "NAT-1")[-1],
                         (3, "console", "Console title"))

        status, answer = self.edit()
        self.assertEqual(status, 409)
        self.assertEqual(answer["current"], 3)
        self.assertIn("error", answer)

        self.assertEqual(self.edit(revision=None)[0], 428)
        self.assertEqual(self.edit(token=OWN, revision="3"), (401, {}))
        self.assertEqual(len(self.revisions("native", "NAT-1")), 3)

    def test_a_linear_project_is_404_and_writes_nothing(self):
        self.start_host()

        status, _ = self.edit(project="linear", identifier="KO-1",
                              revision="1")

        self.assertEqual(status, 404)
        self.assertEqual([r[:2] for r in self.revisions("linear", "KO-1")],
                         [(1, "board")])

    def test_a_daemon_with_actions_off_is_404_and_writes_nothing(self):
        self.start_host(actions=False)

        self.assertEqual(self.edit()[0], 404)
        self.assertEqual([r[:2] for r in self.revisions("native", "NAT-1")],
                         [(1, "cli"), (2, "cli")])

    def corrupt(self, name):
        """Overwrite the named project's store with bytes SQLite refuses."""
        Project.locate(self.paths[name]).store_path.write_bytes(
            b"not a database" * 512)

    def test_a_linear_project_is_404_before_its_store_is_read(self):
        self.corrupt("linear")
        self.start_host()

        status, _ = self.edit(project="linear", identifier="KO-1",
                              revision="1")

        self.assertEqual(status, 404)

    def test_actions_off_is_404_before_the_store_is_read(self):
        self.corrupt("native")
        self.start_host(actions=False)

        self.assertEqual(self.edit()[0], 404)

    def test_a_body_with_a_blocking_problem_in_ready_is_422_unchanged(self):
        self.start_host()

        status, answer = self.edit(text=body("Console title", what=False))

        self.assertEqual(status, 422)
        self.assertTrue(answer["problems"])
        self.assertTrue(all(isinstance(p, str) for p in answer["problems"]))
        self.assertEqual(self.revisions("native", "NAT-1")[-1],
                         (2, "cli", "Second title"))

    def test_the_preflight_allows_if_match(self):
        self.start_host()

        status, _, response = self.request("OPTIONS",
                                           "/projects/native/tickets/NAT-1")

        self.assertEqual(status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Headers"),
                         "authorization, accept, content-type, if-match")


if __name__ == "__main__":
    unittest.main()

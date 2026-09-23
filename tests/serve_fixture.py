"""The daemon tests' shared fixture: one target, one seeded store, one
daemon on a loopback ephemeral port, read back over `http.client`.

`ServeTestCase` and the seed constants live here so every `test_serve*`
module imports one base class and none re-declares it.
"""
from __future__ import annotations

import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from time import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holophyte.project  # noqa: E402 - after the sys.path insert above
import holophyte.serve  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from tests.phase_fixture import finish_run

SEC = 1000
MIN = 60 * SEC
MERGE_SHA = "abc1234def5678901234567890abcdef12345678"


class ServeTestCase(unittest.TestCase):
    """One target, one store with a run and a supervisor beat, one daemon."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = self.root / "repo"
        self.target.mkdir()
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.db = holophyte.project.state_dir(self.target) / "store.db"
        self.db.parent.mkdir(parents=True)

    def seed(self):
        """One run in `working` beating 30 s ago; a supervisor beating 5 s ago."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db), migrate="owner")
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-7",
                linear_identifier="KO-7", title="ticket 7",
                acceptance_criteria=["Given ticket 7, then it is worked"],
                verification_commands=["echo ok"],
                time_box_ms=25 * MIN)
            store.tickets.transition(conn, ticket, "in_flight")
            self.run = store.claim(conn, project, ticket, now=self.now - 2 * MIN)
            store.set_phase(conn, self.run, "working", now=self.now - 2 * MIN)
            store.heartbeat(conn, self.run, now=self.now - 30 * SEC)
            store.record_supervisor_heartbeat(
                conn, 4242, self.now - MIN, now=self.now - 5 * SEC)
        finally:
            conn.close()

    def seed_ended(self):
        """Three ended runs: merged under estimate, failed over it, one with
        no estimate at all and two review rounds."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db), migrate="owner")
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            # KO-3 merged under a module that stamps the merge commit; KO-1
            # merged before the column existed and carries none.
            plan = (("KO-1", 20 * MIN, 10 * MIN, "merged", 1, None),
                    ("KO-2", 20 * MIN, 45 * MIN, "failed", 0, None),
                    ("KO-3", None, 15 * MIN, "merged", 2, MERGE_SHA))
            for n, (ident, box, took, outcome, rounds, sha) in enumerate(plan):
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{ident}",
                    linear_identifier=ident, title=f"ticket {ident}",
                    acceptance_criteria=[f"Given {ident}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=box)
                store.tickets.transition(conn, ticket, "in_flight")
                started = self.now - (10 - n) * 60 * MIN
                run = store.claim(conn, project, ticket, now=started)
                for number in range(1, rounds + 1):
                    store.record_review_round(
                        conn, run, number, "pass", "reviewer-model",
                        started_at=started + number * MIN)
                conn.execute("UPDATE runs SET workingMs = ? WHERE id = ?",
                             (took, run))
                conn.commit()
                finish_run(conn, run, outcome, now=started + took,
                              merge_sha=sha)
        finally:
            conn.close()

    def start(self, config=None, console_dir=None, host="127.0.0.1"):
        """Bind a fixture daemon on loopback with the configured console and token.
        An absent console build under the temporary root is the default."""
        if config is not None:
            (self.db.parent / "config.toml").write_text(config)
        self.project = holophyte.project.Project.locate(self.target)
        console_dir = console_dir or self.root / "console" / "dist"
        token = holophyte.serve.resolve_token(self.project, host)
        server = holophyte.serve.make_server(self.project, host, 0,
                                             console_dir=console_dir,
                                             token=token)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.host, self.port = server.server_address[:2]
        if self.host == "0.0.0.0":
            self.host = "127.0.0.1"

    def request(self, method, path, headers=None, body=None):
        """`(status, headers, decoded JSON body)` for one request."""
        status, headers, raw = self.fetch(method, path, headers, body)
        self.raw_body = raw.decode()
        return status, headers, json.loads(raw)

    def fetch(self, method, path, headers=None, body=None):
        """`(status, headers, raw bytes)` for one request; `body`, when
        given, is sent as JSON."""
        payload = None if body is None else json.dumps(body).encode()
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request(method, path, body=payload, headers=headers or {})
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        return response.status, dict(response.getheaders()), raw

    PREFLIGHT = {"Origin": "http://page.example:7710",
                 "Access-Control-Request-Method": "GET",
                 "Access-Control-Request-Headers": "authorization"}

    def assert_preflight_answered(self, path):
        """`OPTIONS path` is 204, empty, and grants the cross-origin GET
        with its `Authorization` header, reading nothing from the store."""
        with patch.object(store.read, "open_readonly") as opened:
            code, headers, raw = self.fetch("OPTIONS", path, self.PREFLIGHT)
        self.assertEqual(code, 204)
        self.assertEqual(raw, b"")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertEqual(headers["Access-Control-Allow-Methods"], "GET, POST, PUT")
        allowed = headers["Access-Control-Allow-Headers"].lower()
        self.assertIn("authorization", allowed)
        self.assertIn("content-type", allowed)
        self.assertEqual(headers["Access-Control-Max-Age"], "600")
        self.assertNotIn("Allow", headers)
        opened.assert_not_called()

    def null_host(self, run_id):
        """Age run `run_id` past the host column: a row with no recorded host."""
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE runs SET host = NULL WHERE id = ?", (run_id,))
            conn.commit()
        finally:
            conn.close()

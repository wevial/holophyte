"""`--serve HOST:PORT`: `/status` and `/runs` over a read-only connection
per request.

A seeded temporary store under a `HOLOPHYTE_HOME` of the test's own, served
on a loopback ephemeral port, and read back over `http.client`. The store is
written only through the public write API; the daemon is reached only over
the socket, so what is asserted is what a drawer on another host would see.

Run: python3 -m unittest discover -s tests -p 'test_serve*' -v
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from time import sleep, time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holophyte.cli  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.report  # noqa: E402 - after the sys.path insert above
import holophyte.serve  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

SEC = 1000
MIN = 60 * SEC
# How far the clock may move between seeding and the assertion: the daemon
# stamps its own `now`, so an age is "about" the seeded distance.
SLACK = 10 * SEC


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
        self.db = holophyte.target.state_dir(self.target) / "store.db"
        self.db.parent.mkdir(parents=True)

    def seed(self):
        """One run in `working` beating 30 s ago; a supervisor beating 5 s ago."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            ticket = store.mirror_ticket(
                conn, project, linear_issue_id="issue-7",
                linear_identifier="KO-7", title="ticket 7",
                acceptance_criteria=["Given ticket 7, then it is worked"],
                verification_commands=["echo ok"],
                time_box_ms=25 * MIN)
            store.transition(conn, ticket, "in_flight")
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
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            plan = (("KO-1", 20 * MIN, 10 * MIN, "merged", 1),
                    ("KO-2", 20 * MIN, 45 * MIN, "failed", 0),
                    ("KO-3", None, 15 * MIN, "merged", 2))
            for n, (ident, box, took, outcome, rounds) in enumerate(plan):
                ticket = store.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{ident}",
                    linear_identifier=ident, title=f"ticket {ident}",
                    acceptance_criteria=[f"Given {ident}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=box)
                store.transition(conn, ticket, "in_flight")
                started = self.now - (10 - n) * 60 * MIN
                run = store.claim(conn, project, ticket, now=started)
                for number in range(1, rounds + 1):
                    store.record_review_round(
                        conn, run, number, "pass", "reviewer-model",
                        started_at=started + number * MIN)
                store.release(conn, run, outcome, now=started + took)
        finally:
            conn.close()

    def start(self, config=None):
        """Bind the daemon for the target on a loopback ephemeral port."""
        if config is not None:
            (self.db.parent / "config.toml").write_text(config)
        self.tgt = holophyte.target.Target.locate(self.target)
        server = holophyte.serve.make_server(self.tgt, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.host, self.port = server.server_address[:2]

    def request(self, method, path):
        """`(status, headers, decoded JSON body)` for one request."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        self.raw_body = raw.decode()
        return response.status, dict(response.getheaders()), json.loads(raw)

    def null_host(self, run_id):
        """Age run `run_id` past the host column: a row with no recorded host."""
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE runs SET host = NULL WHERE id = ?", (run_id,))
            conn.commit()
        finally:
            conn.close()


class StatusTests(ServeTestCase):

    def test_status_lists_the_live_run_and_the_supervisor(self):
        self.seed()
        self.start()

        code, headers, body = self.request("GET", "/status")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["target"], str(self.target))
        self.assertGreaterEqual(body["now"], self.now)
        (run,) = body["runs"]
        self.assertEqual(run["id"], self.run)
        self.assertEqual(run["ticket"], "KO-7")
        self.assertEqual(run["phase"], "working")
        self.assertEqual(run["time_box_ms"], 25 * MIN)
        self.assertEqual(run["host"], socket.gethostname())
        self.assertTrue(30 * SEC <= run["heartbeat_age_ms"] < 30 * SEC + SLACK,
                        run)
        self.assertTrue(2 * MIN <= run["elapsed_ms"] < 2 * MIN + SLACK, run)
        supervisor = body["supervisor"]
        self.assertEqual(supervisor["state"], "live")
        self.assertEqual(supervisor["pid"], 4242)
        self.assertTrue(
            5 * SEC <= supervisor["heartbeat_age_ms"] < 5 * SEC + SLACK,
            supervisor)
        knobs = holophyte.config.sweep_config(self.tgt)
        self.assertEqual(body["thresholds"],
                         {"heartbeat_stale_ms": knobs.heartbeat_stale_ms,
                          "strikes": knobs.stale_strikes})

    def test_the_stale_threshold_is_a_json_integer(self):
        self.seed()
        self.start()

        _code, _headers, body = self.request("GET", "/status")

        stale = body["thresholds"]["heartbeat_stale_ms"]
        self.assertIs(type(stale), int)
        self.assertEqual(stale, 300000)
        self.assertIn('"heartbeat_stale_ms": 300000,', self.raw_body)

    def test_a_configured_host_label_is_every_host_the_network_sees(self):
        self.seed()
        self.start('[report]\nhost_label = "writer-1"\n')

        code, _headers, body = self.request("GET", "/status")

        self.assertEqual(code, 200)
        hosts = [body["host"], body["supervisor"]["host"],
                 *(run["host"] for run in body["runs"])]
        self.assertEqual(len(hosts), 3)
        self.assertEqual(set(hosts), {"writer-1"})
        self.assertNotIn(socket.gethostname(), json.dumps(body))

    def test_a_run_without_a_recorded_host_is_null_under_a_label(self):
        self.seed()
        self.null_host(self.run)
        self.start('[report]\nhost_label = "writer-1"\n')

        code, _headers, body = self.request("GET", "/status")

        self.assertEqual(code, 200)
        (run,) = body["runs"]
        self.assertIsNone(run["host"])
        self.assertEqual(body["host"], "writer-1")
        self.assertNotIn("?", json.dumps(body))

    def test_the_daemon_holds_no_write_lock_after_answering(self):
        self.seed()
        self.start()
        self.assertEqual(self.request("GET", "/status")[0], 200)

        # A writer's first move: the store must grant the reserved lock at
        # once, not wait on a connection the daemon left open.
        conn = sqlite3.connect(str(self.db), timeout=0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    def test_a_target_with_no_store_answers_503_and_creates_none(self):
        self.start()

        code, headers, body = self.request("GET", "/status")

        self.assertEqual(code, 503)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("no store", body["error"])
        self.assertIn(str(self.target), body["detail"])
        self.assertFalse(self.db.exists())

    def test_an_unknown_path_is_404_and_any_other_method_is_405(self):
        self.seed()
        self.start()

        code, headers, body = self.request("GET", "/nope")
        self.assertEqual(code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("error", body)
        self.assertEqual(body["path"], "/nope")

        # Every method but GET, the ones `http.server` would otherwise answer
        # with its own 501 HTML page included (OPTIONS, TRACE, an unknown one).
        for method in ("POST", "OPTIONS", "TRACE", "BREW"):
            with self.subTest(method=method):
                code, headers, body = self.request(method, "/status")
                self.assertEqual(code, 405)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(headers["Allow"], "GET")
                self.assertIn("error", body)
                self.assertEqual(body["method"], method)

        # HEAD too: `http.client` discards a HEAD body, so read the wire.
        with socket.create_connection((self.host, self.port), timeout=10) as s:
            s.sendall(b"HEAD /status HTTP/1.1\r\nHost: x\r\n"
                      b"Connection: close\r\n\r\n")
            raw = b""
            while chunk := s.recv(4096):
                raw += chunk
        head, _, payload = raw.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 405 "), head)
        self.assertIn(b"Content-Type: application/json", head)
        self.assertIn("error", json.loads(payload))


class RunsTests(ServeTestCase):

    def expected_rows(self):
        """The oracle: `report_rows()` over the same store, named by column."""
        conn = store.open(str(self.db))
        try:
            rows = holophyte.report.report_rows(conn)
        finally:
            conn.close()
        keys = ("ticket", "actual_min", "estimate_min", "ratio", "rounds",
                "outcome", "host")
        return [dict(zip(keys, row)) for row in rows]

    def test_runs_is_the_report_table_as_json(self):
        self.seed_ended()
        self.start()

        code, headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIsNone(body["limit"])
        expected = self.expected_rows()
        self.assertEqual(len(expected), 3)
        self.assertEqual(body["rows"], expected)
        # The three the seed planned, oldest first, so a wrong order or a
        # merged/failed mix-up would not pass on equality alone.
        self.assertEqual([r["ticket"] for r in body["rows"]],
                         ["KO-1", "KO-2", "KO-3"])
        self.assertEqual([r["outcome"] for r in body["rows"]],
                         ["merged", "failed", "merged"])
        self.assertEqual([r["rounds"] for r in body["rows"]], [1, 0, 2])
        self.assertIsNone(body["rows"][2]["estimate_min"])
        self.assertIsNone(body["rows"][2]["ratio"])
        self.assertAlmostEqual(body["rows"][0]["ratio"], 0.5)

    def test_limit_keeps_the_first_rows_and_a_bad_limit_is_400(self):
        self.seed_ended()
        self.start()

        code, _headers, body = self.request("GET", "/runs?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["rows"], self.expected_rows()[:2])

        for query in ("limit=0", "limit=abc", "limit=-1", "limit="):
            with self.subTest(query=query):
                code, headers, body = self.request("GET", f"/runs?{query}")
                self.assertEqual(code, 400)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertIn("error", body)
                self.assertNotIn("rows", body)

    def test_a_configured_host_label_is_every_host_in_the_rows(self):
        self.seed_ended()
        self.start('[report]\nhost_label = "writer-1"\n')

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        self.assertEqual(len(body["rows"]), 3)
        self.assertEqual({r["host"] for r in body["rows"]}, {"writer-1"})
        self.assertNotIn(socket.gethostname(), json.dumps(body))

    def test_a_row_without_a_recorded_host_is_null_under_a_label(self):
        self.seed_ended()
        self.null_host(2)
        self.start('[report]\nhost_label = "writer-1"\n')

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        self.assertEqual([r["host"] for r in body["rows"]],
                         ["writer-1", None, "writer-1"])
        self.assertNotIn("?", json.dumps(body))

    def test_runs_without_a_store_is_503_and_creates_none(self):
        self.start()

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 503)
        self.assertIn("no store", body["error"])
        self.assertFalse(self.db.exists())


class CliTests(ServeTestCase):

    def test_a_malformed_address_is_a_usage_error_naming_the_shape(self):
        for argv in (["--serve"], ["--serve", "localhost"],
                     ["--serve", "127.0.0.1:abc"]):
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), \
                        self.assertRaises(SystemExit) as raised:
                    holophyte.cli.cli([str(self.target), *argv])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("HOST:PORT", stderr.getvalue())
                self.assertFalse(self.db.exists())

    def test_serve_announces_the_bound_address_and_stops_on_sigterm(self):
        self.seed()
        out = io.StringIO()
        seen = {}

        def poll_then_stop():
            # The announcement names the port the kernel picked; poll it
            # once, then send the signal the operator's ^C or kill would.
            while "serving" not in out.getvalue():
                sleep(0.01)
            line = out.getvalue().splitlines()[0]
            host, port = line.split()[2].split(":")
            conn = http.client.HTTPConnection(host, int(port), timeout=10)
            conn.request("GET", "/status")
            seen["status"] = conn.getresponse().status
            conn.close()
            os.kill(os.getpid(), signal.SIGTERM)

        stopper = threading.Thread(target=poll_then_stop)
        stopper.start()
        self.addCleanup(stopper.join)
        with contextlib.redirect_stdout(out):
            code = holophyte.cli.cli([str(self.target), "--serve", "127.0.0.1:0"])
        stopper.join()

        self.assertEqual(code, 0)
        self.assertEqual(seen["status"], 200)
        first = out.getvalue().splitlines()[0]
        self.assertTrue(first.startswith("[holo2] serving 127.0.0.1:"), first)
        self.assertIn(f"read-only for {self.target}", first)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


if __name__ == "__main__":
    unittest.main()

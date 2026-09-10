"""`--serve PORT|HOST:PORT`: `/status` and `/runs` over a read-only connection
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
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path
from time import sleep, time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.cli  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.files  # noqa: E402 - after the sys.path insert above
import holophyte.report  # noqa: E402 - after the sys.path insert above
import holophyte.serve  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above

SEC = 1000
MIN = 60 * SEC
MERGE_SHA = "abc1234def5678901234567890abcdef12345678"
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
            # KO-3 merged under a module that stamps the merge commit; KO-1
            # merged before the column existed and carries none.
            plan = (("KO-1", 20 * MIN, 10 * MIN, "merged", 1, None),
                    ("KO-2", 20 * MIN, 45 * MIN, "failed", 0, None),
                    ("KO-3", None, 15 * MIN, "merged", 2, MERGE_SHA))
            for n, (ident, box, took, outcome, rounds, sha) in enumerate(plan):
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
                store.release(conn, run, outcome, now=started + took,
                              merge_sha=sha)
        finally:
            conn.close()

    def start(self, config=None, console_dir=None, host="127.0.0.1"):
        """Bind the daemon for the target on an ephemeral port at `host`,
        loopback by default, serving `/` from `console_dir` -- an absent
        directory under the test root by default, never the repository's
        own build. The token is what `serve()` would resolve for the bind:
        none on loopback, the configured file's contents otherwise."""
        if config is not None:
            (self.db.parent / "config.toml").write_text(config)
        self.tgt = holophyte.target.Target.locate(self.target)
        console_dir = console_dir or self.root / "console" / "dist"
        token = holophyte.serve.resolve_token(self.tgt, host)
        server = holophyte.serve.make_server(self.tgt, host, 0,
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


class PeersTests(ServeTestCase):
    """`GET /peers`: the target's `[console] daemons` beside the address
    this daemon bound, so a page loaded from it knows where to fan out."""

    def test_peers_is_the_configured_list_and_self_the_bound_address(self):
        self.seed()
        self.start('[console]\ndaemons = ["writer-2:7710", "writer-3:7710"]\n')

        code, headers, body = self.request("GET", "/peers")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, {"self": f"{self.host}:{self.port}",
                                "peers": ["writer-2:7710", "writer-3:7710"]})

    def test_no_console_table_is_an_empty_list_not_an_error(self):
        self.seed()
        self.start()

        code, _headers, body = self.request("GET", "/peers")

        self.assertEqual(code, 200)
        self.assertEqual(body["peers"], [])
        self.assertEqual(body["self"], f"{self.host}:{self.port}")


class TokenTests(ServeTestCase):
    """`[serve] token_file`: a non-loopback bind demands it, every JSON
    route but `/peers` is 401 without the exact bearer value, the console
    page and its files stay open, and a loopback bind ignores the key."""

    TOKEN = "s3cret-token-value"
    BEARER = {"Authorization": f"Bearer {TOKEN}"}

    def token_file(self, mode=0o600, text=None):
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n" if text is None else text)
        path.chmod(mode)
        return path

    def token_config(self, path):
        return f'[serve]\ntoken_file = "{path}"\n'

    def build(self):
        dist = self.root / "console" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_bytes(b"<!doctype html>")
        (dist / "app.js").write_bytes(b"console.log(1)")
        return dist

    def test_a_non_loopback_bind_without_a_token_file_is_a_startup_error(self):
        self.seed()
        tgt = holophyte.target.Target.locate(self.target)
        for address in ("0.0.0.0:0", "[::]:0", "10.0.0.1:0"):
            with self.subTest(address=address), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.serve.serve(tgt, address, out=io.StringIO())
            self.assertIn("[serve] token_file", str(raised.exception))
            self.assertNotEqual(raised.exception.code, 0)

    def test_the_cli_refuses_the_bind_before_serving(self):
        self.seed()
        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            holophyte.cli.cli([str(self.target), "--serve", "0.0.0.0:0"])
        self.assertIn("[serve] token_file", str(raised.exception))

    def test_status_is_401_without_the_token_and_200_with_it(self):
        self.seed()
        self.start(self.token_config(self.token_file()), host="0.0.0.0")
        for headers in (None, {"Authorization": "Bearer wrong"},
                        {"Authorization": f"Bearer {self.TOKEN}x"},
                        {"Authorization": f"Basic {self.TOKEN}"}):
            with self.subTest(headers=headers), \
                    patch.object(store.read, "open_readonly") as opened:
                code, response, body = self.request("GET", "/status", headers)
                self.assertEqual(code, 401)
                self.assertEqual(body, {})
                self.assertEqual(response["Content-Type"], "application/json")
                opened.assert_not_called()
        code, _, body = self.request("GET", "/status", self.BEARER)
        self.assertEqual(code, 200)
        self.assertEqual(body["target"], str(self.target))
        # Every store-reading route is behind it, the run routes included.
        for path in ("/runs", "/shipped", "/ledger?since=0", "/attention",
                     "/board", f"/runs/{self.run}", f"/runs/{self.run}/files",
                     f"/runs/{self.run}/ledger"):
            with self.subTest(path=path):
                code, _, _ = self.request("GET", path)
                self.assertEqual(code, 401)

    def test_a_preflight_without_the_bearer_is_204_not_401(self):
        self.seed()
        self.start(self.token_config(self.token_file()), host="0.0.0.0")

        # The browser asks before the GET that carries the token; the answer
        # needs none. And the 401 a bare GET earns stays readable cross-origin.
        self.assert_preflight_answered("/status")
        code, headers, _ = self.request("GET", "/status")
        self.assertEqual(code, 401)
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_the_page_its_files_and_peers_stay_open(self):
        self.seed()
        self.build()
        config = (self.token_config(self.token_file())
                  + '[console]\ndaemons = ["writer-2:7710"]\n')
        self.start(config, host="0.0.0.0")
        for path in ("/", "/app.js", "/peers"):
            with self.subTest(path=path):
                code, _, _ = self.fetch("GET", path)
                self.assertEqual(code, 200)
        # A file named after a JSON route does not open the route.
        (self.root / "console" / "dist" / "status").write_text("x")
        code, _, _ = self.fetch("GET", "/status")
        self.assertEqual(code, 401)

    def test_a_loopback_bind_ignores_the_key_and_answers_open(self):
        self.seed()
        for config in (None, self.token_config(self.token_file())):
            with self.subTest(config=config):
                self.start(config)
                code, _, body = self.request("GET", "/status")
                self.assertEqual(code, 200)
                self.assertIn("runs", body)

    def configured(self, path):
        """The target with `[serve] token_file` pointing at `path`."""
        (self.db.parent / "config.toml").write_text(self.token_config(path))
        return holophyte.target.Target.locate(self.target)

    def test_a_group_or_world_readable_token_file_is_refused(self):
        self.seed()
        for mode in (0o640, 0o604, 0o644):
            path = self.token_file(mode)
            tgt = self.configured(path)
            with self.subTest(mode=oct(mode)), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.serve.serve(tgt, "0.0.0.0:0", out=io.StringIO())
            message = str(raised.exception)
            self.assertIn(f"{mode:04o}", message)
            self.assertIn(str(path), message)
            self.assertNotIn(self.TOKEN, message)

    def test_a_missing_or_empty_token_file_is_refused_naming_it(self):
        self.seed()
        for path in (self.root / "absent.token", self.token_file(text="  \n")):
            tgt = self.configured(path)
            with self.subTest(path=path.name), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.serve.serve(tgt, "0.0.0.0:0", out=io.StringIO())
            self.assertIn(str(path), str(raised.exception))


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
        self.assertIs(body["actions"], False)

    def test_a_run_carries_title_start_round_and_strikes(self):
        # KO-263: what the console's floor row draws. A run in `reviewing`
        # with two ended rounds and one strike on file.
        self.seed()
        conn = store.open(str(self.db))
        try:
            store.set_phase(conn, self.run, "reviewing", now=self.now - MIN)
            for number in (1, 2):
                store.record_review_round(
                    conn, self.run, number, "changes_requested", "reviewer",
                    started_at=self.now - MIN + number,
                    ended_at=self.now - MIN + number + 1)
            store.record_strike(conn, self.run, stale=True,
                                heartbeat=self.now - 30 * SEC, now=self.now)
        finally:
            conn.close()
        self.start()

        _code, _headers, body = self.request("GET", "/status")

        (run,) = body["runs"]
        self.assertEqual(run["title"], "ticket 7")
        self.assertEqual(run["started_ms"], self.now - 2 * MIN)
        self.assertEqual(run["round"], 2)
        self.assertEqual(run["strikes"], 1)

    def test_a_run_not_under_suspicion_has_zero_strikes(self):
        self.seed()
        self.start()

        _code, _headers, body = self.request("GET", "/status")

        (run,) = body["runs"]
        self.assertEqual(run["strikes"], 0)
        self.assertEqual(run["round"], 0)
        self.assertIn('"strikes": 0', self.raw_body)

    def test_the_body_carries_the_daemon_and_project(self):
        before = int(time() * 1000)
        self.seed()
        self.start()

        _code, _headers, body = self.request("GET", "/status")

        self.assertEqual(body["daemon"]["pid"], os.getpid())
        self.assertTrue(
            before <= body["daemon"]["started_ms"] <= body["now"], body)
        self.assertEqual(body["project"], body["target"])
        self.assertEqual(body["project"], str(self.target))

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

        # Every method but GET and OPTIONS, the ones `http.server` would
        # otherwise answer with its own 501 HTML page included (TRACE, an
        # unknown one). OPTIONS is the CORS preflight, witnessed on its own.
        for method in ("POST", "TRACE", "BREW"):
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


    def test_a_preflight_on_a_loopback_daemon_is_204(self):
        self.seed()
        self.start()

        self.assert_preflight_answered("/peers")


class ConsoleTests(ServeTestCase):
    """`/` and the paths no JSON route claims: the console's built files."""

    INDEX = b"<!doctype html><title>holophyte</title>"
    APP = b"console.log('holophyte');\n"
    MANIFEST = b'{"name": "holophyte", "display": "standalone"}\n'
    BLOB = b"\x00\x01no extension the map knows\xff"

    def build(self):
        """A `dist/` with an `index.html`, `app.js`, `manifest.webmanifest`
        and a file of unknown extension under the test root."""
        dist = self.root / "console" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_bytes(self.INDEX)
        (dist / "app.js").write_bytes(self.APP)
        (dist / "manifest.webmanifest").write_bytes(self.MANIFEST)
        (dist / "blob.bin").write_bytes(self.BLOB)
        return dist

    def test_every_json_answer_allows_any_origin(self):
        """The console page, served by one daemon, fetches the others from
        the browser, which refuses a cross-origin answer without the header:
        a 200 and a 404 both carry it."""
        self.seed()
        self.start()

        for path, expected in (("/status", 200), ("/peers", 200),
                               ("/nope", 404)):
            with self.subTest(path=path):
                code, headers, _body = self.request("GET", path)
                self.assertEqual(code, expected)
                self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_root_and_a_file_answer_their_bytes_typed_and_uncached(self):
        self.seed()
        self.build()
        self.start()
        for path, payload, mime in (
                ("/", self.INDEX, "text/html"),
                ("/index.html", self.INDEX, "text/html"),
                ("/app.js", self.APP, "javascript"),
                ("/manifest.webmanifest", self.MANIFEST,
                 "application/manifest+json"),
                ("/blob.bin", self.BLOB, "application/octet-stream")):
            with self.subTest(path=path):
                code, headers, raw = self.fetch("GET", path)
                self.assertEqual(code, 200)
                self.assertEqual(raw, payload)
                self.assertIn(mime, headers["Content-Type"])
                self.assertEqual(headers["Cache-Control"], "no-store")

    def test_a_path_that_escapes_the_directory_is_404_json(self):
        self.seed()
        dist = self.build()
        # A real file just outside `dist/` and a symlink inside pointing at
        # it: neither must come back.
        secret = self.root / "console" / "secret.txt"
        secret.write_text("not for the network")
        (dist / "link.txt").symlink_to(secret)
        (dist / "escape").symlink_to(self.root / "console")
        self.start()
        for path in ("/../secret.txt", "/%2e%2e/secret.txt",
                     "/..%2fsecret.txt", "/link.txt", "/escape/secret.txt",
                     "/" + str(secret), "/missing.js"):
            with self.subTest(path=path):
                code, headers, body = self.request("GET", path)
                self.assertEqual(code, 404)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertEqual(body["error"], "not found")
                self.assertNotIn("not for the network", self.raw_body)

    def test_an_unbuilt_console_is_404_and_the_json_still_answers(self):
        self.seed()
        self.start()
        code, headers, body = self.request("GET", "/")
        self.assertEqual(code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("not built", body["detail"])
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertEqual(body["target"], str(self.target))

    def test_the_json_routes_take_precedence_over_files(self):
        self.seed()
        dist = self.build()
        # A file shadows each JSON route by name; none of them is served.
        for name in ("status", "runs", "attention"):
            (dist / name).write_text("a file named " + name)
        self.start()
        for path, key in (("/status", "runs"), ("/runs", "rows"),
                          ("/attention", "items")):
            with self.subTest(path=path):
                code, headers, body = self.request("GET", path)
                self.assertEqual(code, 200)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertIn(key, body)
                self.assertNotIn("a file named", self.raw_body)


class TicketTests(ServeTestCase):
    """`/tickets/KO-n`: one mirrored ticket with its body; 404 for one the
    store never mirrored; behind the token like `/board`."""

    BODY = "# A ticket\n\n## Summary\n\nThe body the loop read at claim.\n"

    def seed_ticket(self):
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            ticket = store.mirror_ticket(
                conn, project, linear_issue_id="issue-7",
                linear_identifier="KO-7", title="ticket 7",
                acceptance_criteria=["Given KO-7, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN,
                body=self.BODY, now=self.now - 5 * MIN)
            store.transition(conn, ticket, "in_flight")
            self.run = store.claim(conn, project, ticket, now=self.now - 2 * MIN)
        finally:
            conn.close()

    def test_a_mirrored_ticket_answers_its_nine_fields_and_body(self):
        self.seed_ticket()
        self.start()

        code, headers, body = self.request("GET", "/tickets/KO-7")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, {
            "ticket": "KO-7", "title": "ticket 7", "status": "in_flight",
            "body": self.BODY,
            "acceptance_criteria": ["Given KO-7, then it is worked"],
            "verification_commands": ["echo ok"],
            "time_box_ms": 25 * MIN, "run": self.run,
            "mirrored_ms": self.now - 5 * MIN})

    def test_an_identifier_never_mirrored_is_404_with_an_empty_object(self):
        self.seed_ticket()
        self.start()

        code, headers, body = self.request("GET", "/tickets/KO-9999")

        self.assertEqual(code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body, {})

    def test_the_route_is_behind_the_token_like_the_board(self):
        self.seed_ticket()
        token = self.root / "serve.token"
        token.write_text(TokenTests.TOKEN + "\n")
        token.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{token}"\n', host="0.0.0.0")

        with patch.object(store.read, "open_readonly") as opened:
            code, _, body = self.request("GET", "/tickets/KO-7")
        self.assertEqual(code, 401)
        self.assertEqual(body, {})
        opened.assert_not_called()
        code, _, _ = self.request("GET", "/board")
        self.assertEqual(code, 401)

        code, _, body = self.request("GET", "/tickets/KO-7", TokenTests.BEARER)
        self.assertEqual(code, 200)
        self.assertEqual(body["body"], self.BODY)


class AttentionTests(ServeTestCase):
    """`/attention`: the four item kinds, their order, the window, the level."""

    HOUR = 60 * MIN

    def seed_attention(self, failed_ago=2 * HOUR, stale=True, redirect=True,
                       attempts=1):
        """KO-8 parked with a question, its run parked `blocked_on_operator`
        3 h ago with a `redirect` intervention asking it 3 h ago when
        `redirect`; KO-9 failed `failed_ago` ago as attempt `attempts` and
        still `in_flight`; KO-7 live, beating 20 min ago when `stale`, else
        30 s ago; a supervisor beating 20 min ago when `stale`, else 5 s."""
        self.now = int(time() * 1000)
        self.asked = self.now - 3 * self.HOUR
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)

            def ticket(ident):
                return store.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{ident}",
                    linear_identifier=ident, title=f"ticket {ident}",
                    acceptance_criteria=[f"Given {ident}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=25 * MIN)

            blocked = ticket("KO-8")
            store.transition(conn, blocked, "in_flight")
            self.blocked_run = store.claim(conn, project, blocked,
                                           now=self.asked - 10 * MIN)
            store.set_phase(conn, self.blocked_run, "working",
                            now=self.asked - 10 * MIN)
            # The heartbeat is `asked_ms`'s fallback: a minute before the
            # redirect so the two are told apart.
            store.heartbeat(conn, self.blocked_run, now=self.asked - MIN)
            store.transition(conn, blocked, "blocked_on_operator")
            conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                         ("Which branch is canonical?", blocked))
            conn.commit()
            store.park(conn, self.blocked_run, "blocked_on_operator",
                       "asked the operator", now=self.asked - MIN)
            if redirect:
                store.record_intervention(
                    conn, self.blocked_run, "redirect", "asked the operator",
                    source="supervisor", trigger="off_criteria",
                    question="Which branch is canonical?", now=self.asked)

            self.failed_ticket = ticket("KO-9")
            store.transition(conn, self.failed_ticket, "in_flight")
            self.earlier_failed = []
            for n in range(attempts - 1, 0, -1):
                earlier = store.claim(conn, project, self.failed_ticket,
                                      now=self.now - failed_ago - n * self.HOUR)
                store.release(conn, earlier, "failed", reason="verify red",
                              now=self.now - failed_ago - n * self.HOUR + MIN)
                self.earlier_failed.append(earlier)
            self.failed = store.claim(conn, project, self.failed_ticket,
                                      now=self.now - failed_ago - 10 * MIN)
            store.release(conn, self.failed, "failed", reason="verify red",
                          now=self.now - failed_ago)

            # Claimed after KO-9 ended: the project lease is one run at a time.
            live = ticket("KO-7")
            store.transition(conn, live, "in_flight")
            self.run = store.claim(conn, project, live, now=self.now - 40 * MIN)
            store.set_phase(conn, self.run, "working", now=self.now - 40 * MIN)
            store.heartbeat(conn, self.run,
                            now=self.now - (20 * MIN if stale else 30 * SEC))
            store.record_supervisor_heartbeat(
                conn, 4242, self.now - self.HOUR,
                now=self.now - (20 * MIN if stale else 5 * SEC))
        finally:
            conn.close()

    def test_every_kind_in_order_and_the_level_is_attention(self):
        self.seed_attention()
        self.start()

        code, headers, body = self.request("GET", "/attention")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["level"], "attention")
        self.assertGreaterEqual(body["now"], self.now)
        kinds = [item["kind"] for item in body["items"]]
        self.assertEqual(kinds, ["blocked", "stale_run", "failed", "supervisor"])
        blocked, stale_run, failed, supervisor = body["items"]
        self.assertEqual(blocked, {"kind": "blocked", "ticket": "KO-8",
                                   "question": "Which branch is canonical?",
                                   "run": self.blocked_run,
                                   "asked_ms": self.asked, "pr_url": None,
                                   "level": "attention"})
        self.assertEqual(stale_run["run"], self.run)
        self.assertEqual(stale_run["ticket"], "KO-7")
        self.assertEqual(stale_run["phase"], "working")
        self.assertTrue(
            20 * MIN <= stale_run["heartbeat_age_ms"] < 20 * MIN + SLACK,
            stale_run)
        self.assertEqual(failed["run"], self.failed)
        self.assertEqual(failed["ticket"], "KO-9")
        self.assertEqual(failed["reason"], "verify red")
        self.assertEqual(failed["ended_ms"], self.now - 2 * self.HOUR)
        self.assertEqual(failed["attempt"], 1)
        self.assertEqual(supervisor["state"], "stale")
        self.assertTrue(
            20 * MIN <= supervisor["heartbeat_age_ms"] < 20 * MIN + SLACK,
            supervisor)
        for item in body["items"]:
            self.assertEqual(item["level"], "attention", item)

    def test_a_park_on_a_pull_request_is_pr_open_and_a_question_stays_blocked(self):
        """KO-8 is parked with a plain question and no PR; KO-10 is parked
        the way `_park_on_pr()` parks: `runs.prUrl` set and the ticket
        asking `PR open: URL` with the reason under it. Only KO-10 is
        `pr_open`, its `reason` the question without that first line."""
        self.seed_attention()
        url = "https://github.com/example/repo/pull/2170"
        conn = store.open(str(self.db))
        try:
            project = store.ensure_project(conn, "team-1", self.target)
            parked = store.mirror_ticket(
                conn, project, linear_issue_id="issue-KO-10",
                linear_identifier="KO-10", title="ticket KO-10",
                acceptance_criteria=["Given KO-10, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN)
            store.transition(conn, parked, "in_flight")
            run = store.claim(conn, project, parked, now=self.now - 5 * MIN)
            store.set_phase(conn, run, "working", now=self.now - 5 * MIN)
            store.heartbeat(conn, run, now=self.now - 2 * MIN)
            store.transition(conn, parked, "blocked_on_operator")
            conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                         (f"PR open: {url}\nreview requested from a coworker"
                          "\n1. src/x.py:3 by @coworker", parked))
            conn.commit()
            store.park(conn, run, "awaiting_merge_approval", "PR open",
                       candidate_sha="a" * 40, pr_url=url,
                       now=self.now - 2 * MIN)
        finally:
            conn.close()
        self.start()

        _, _, body = self.request("GET", "/attention")

        by_ticket = {item["ticket"]: item for item in body["items"]
                     if item["kind"] in ("blocked", "pr_open")}
        self.assertEqual(by_ticket["KO-8"]["kind"], "blocked")
        self.assertEqual(by_ticket["KO-8"]["question"],
                         "Which branch is canonical?")
        self.assertEqual(by_ticket["KO-10"], {
            "kind": "pr_open", "ticket": "KO-10", "run": run, "pr_url": url,
            "reason": "review requested from a coworker"
                      "\n1. src/x.py:3 by @coworker",
            "asked_ms": self.now - 2 * MIN, "level": "attention"})
        self.assertEqual(body["level"], "attention")

    def test_the_body_names_the_target_as_status_does(self):
        self.seed_attention()
        self.start()

        _, _, body = self.request("GET", "/attention")

        self.assertEqual(body["target"], str(self.target))
        self.assertEqual(body["project"], str(self.target))

    def test_asked_ms_falls_back_to_the_heartbeat_without_a_redirect(self):
        self.seed_attention(redirect=False)
        self.start()

        _, _, body = self.request("GET", "/attention")

        blocked = body["items"][0]
        self.assertEqual(blocked["kind"], "blocked")
        self.assertEqual(blocked["run"], self.blocked_run)
        self.assertEqual(blocked["asked_ms"], self.asked - MIN)

    def test_a_failed_item_carries_the_attempt_it_was(self):
        self.seed_attention(attempts=2)
        self.start()

        _, _, body = self.request("GET", "/attention")

        # Both attempts ended inside the window, so both are items; each
        # says which attempt it was, not how many the client has seen.
        failed = [(item["run"], item["attempt"])
                  for item in body["items"] if item["kind"] == "failed"]
        self.assertEqual(failed, [(self.earlier_failed[0], 1),
                                  (self.failed, 2)])

    def test_a_requeued_failure_drops_out(self):
        self.seed_attention()
        conn = store.open(str(self.db))
        try:
            store.requeue(conn, self.failed_ticket, "operator requeued")
        finally:
            conn.close()
        self.start()

        _, _, body = self.request("GET", "/attention")

        kinds = [item["kind"] for item in body["items"]]
        self.assertEqual(kinds, ["blocked", "stale_run", "supervisor"])

    def test_a_failure_older_than_a_day_drops_out(self):
        self.seed_attention(failed_ago=30 * self.HOUR)
        self.start()

        _, _, body = self.request("GET", "/attention")

        kinds = [item["kind"] for item in body["items"]]
        self.assertEqual(kinds, ["blocked", "stale_run", "supervisor"])

    def test_nothing_wrong_is_working_with_a_live_run_else_none(self):
        self.seed()
        self.start()

        _, _, body = self.request("GET", "/attention")

        self.assertEqual(body, {"level": "working", "items": [],
                                "now": body["now"],
                                "target": str(self.target),
                                "project": str(self.target)})

        conn = store.open(str(self.db))
        try:
            store.release(conn, self.run, "merged")
        finally:
            conn.close()

        _, _, body = self.request("GET", "/attention")

        self.assertEqual(body["items"], [])
        self.assertEqual(body["level"], "none")

    def test_a_target_with_no_store_answers_503(self):
        self.start()

        code, _, body = self.request("GET", "/attention")

        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "no store")
        self.assertFalse(self.db.exists())


class BoardTests(ServeTestCase):
    """`/board`: the five columns in path order, what each ticket waits on."""

    def seed_board(self):
        """One ticket per open state plus a merged one: KO-1 needs a spec,
        KO-2 is ready, KO-3 is blocked on KO-2 and on an issue the store
        never mirrored, KO-4 is parked with a question, KO-5 is in flight
        under a live run, KO-6 merged (KO-3 also names it, so a dependency
        on a merged ticket is no wait)."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)

            def ticket(n, specced=True, depends_on=None):
                return store.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{n}",
                    linear_identifier=f"KO-{n}", title=f"ticket {n}",
                    acceptance_criteria=[f"Given KO-{n}, then it is worked"]
                    if specced else [],
                    verification_commands=["echo ok"] if specced else [],
                    time_box_ms=25 * MIN, depends_on=depends_on,
                    now=self.now - 5 * MIN)

            ticket(1, specced=False)
            ticket(2)
            blocked_on_deps = ticket(
                3, depends_on=["issue-2", "issue-6", "issue-never-seen"])
            store.transition(conn, blocked_on_deps, "blocked_on_deps")
            parked = ticket(4)
            store.transition(conn, parked, "in_flight")
            store.transition(conn, parked, "blocked_on_operator")
            conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                         ("Which branch is canonical?", parked))
            conn.commit()
            merged = ticket(6)
            store.transition(conn, merged, "in_flight")
            run = store.claim(conn, project, merged, now=self.now - 20 * MIN)
            store.release(conn, run, "merged", now=self.now - 10 * MIN,
                          merge_sha=MERGE_SHA)
            store.transition(conn, merged, "merged")
            live = ticket(5)
            store.transition(conn, live, "in_flight")
            self.run = store.claim(conn, project, live, now=self.now - 2 * MIN)
            store.set_phase(conn, self.run, "working", now=self.now - 2 * MIN)
        finally:
            conn.close()

    def test_five_columns_in_path_order_and_the_merged_ticket_absent(self):
        self.seed_board()
        self.start()

        code, headers, body = self.request("GET", "/board")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual([column["state"] for column in body["columns"]],
                         ["needs_spec", "blocked_on_deps", "ready",
                          "blocked_on_operator", "in_flight"])
        self.assertEqual(
            [[ticket["ticket"] for ticket in column["tickets"]]
             for column in body["columns"]],
            [["KO-1"], ["KO-3"], ["KO-2"], ["KO-4"], ["KO-5"]])
        self.assertNotIn("KO-6", self.raw_body)
        self.assertAlmostEqual(body["now"], self.now, delta=SLACK)

    def test_a_ticket_carries_its_run_question_and_what_it_waits_on(self):
        self.seed_board()
        self.start()

        _, _, body = self.request("GET", "/board")

        by_state = {column["state"]: column["tickets"]
                    for column in body["columns"]}
        self.assertEqual(by_state["blocked_on_deps"], [
            {"ticket": "KO-3", "title": "ticket 3", "time_box_ms": 25 * MIN,
             "run": None, "question": None,
             "waits_on": ["KO-2", "issue-never-seen"],
             "mirrored_ms": self.now - 5 * MIN}])
        self.assertEqual(by_state["in_flight"], [
            {"ticket": "KO-5", "title": "ticket 5", "time_box_ms": 25 * MIN,
             "run": self.run, "question": None, "waits_on": [],
             "mirrored_ms": self.now - 5 * MIN}])
        self.assertEqual(by_state["blocked_on_operator"][0]["question"],
                         "Which branch is canonical?")
        self.assertIsNone(by_state["needs_spec"][0]["run"])

    def test_a_target_with_no_store_answers_503(self):
        self.start()

        code, _, body = self.request("GET", "/board")

        self.assertEqual(code, 503)
        self.assertEqual(body["error"], "no store")
        self.assertFalse(self.db.exists())


class RunsTests(ServeTestCase):

    def expected_rows(self):
        """The oracle: `report_rows()` over the same store, named by column."""
        conn = store.open(str(self.db))
        try:
            rows = holophyte.report.report_rows(conn)
        finally:
            conn.close()
        keys = ("ticket", "actual_min", "estimate_min", "ratio", "rounds",
                "outcome", "host", "ended_ms", "merge_sha")
        return [dict(zip(keys, row + (ended, sha)))
                for row, ended, sha in zip(rows, self.ended_at(),
                                           self.merge_shas())]

    def ended_at(self):
        """The oracle for `ended_ms`: `runs.endedAt` itself, in the report's
        order, read straight from the table rather than through the report."""
        return self.column("endedAt")

    def merge_shas(self):
        """The oracle for `merge_sha`: `runs.mergeSha` itself, same order."""
        return self.column("mergeSha")

    def column(self, name):
        conn = store.open(str(self.db))
        try:
            return [value for (value,) in conn.execute(
                f"SELECT {name} FROM runs WHERE endedAt IS NOT NULL"
                " ORDER BY endedAt, id")]
        finally:
            conn.close()

    def test_a_merged_run_carries_its_full_merge_sha(self):
        self.seed_ended()
        self.start()

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        # The sha the seed released KO-3 with, whole, not the seven
        # characters FINDINGS prints; KO-1 merged without one and the failed
        # KO-2 never has one.
        self.assertEqual([r["merge_sha"] for r in body["rows"]],
                         [None, None, MERGE_SHA])
        self.assertEqual(len(MERGE_SHA), 40)

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

    def test_each_run_carries_its_end_as_ended_ms(self):
        self.seed_ended()
        self.start()

        code, _headers, body = self.request("GET", "/runs")

        self.assertEqual(code, 200)
        # The seed released each run `took` after its start: KO-1 ten
        # minutes after starting ten hours ago, and so on -- integers, in
        # epoch milliseconds, equal to the table's own `endedAt`.
        ended = [r["ended_ms"] for r in body["rows"]]
        self.assertEqual(ended, self.ended_at())
        self.assertTrue(all(isinstance(ms, int) for ms in ended), ended)
        expected = [self.now - (10 - n) * 60 * MIN + took
                    for n, took in enumerate((10 * MIN, 45 * MIN, 15 * MIN))]
        self.assertEqual(ended, expected)

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


class ShippedTests(ServeTestCase):
    """`/shipped`: the merge ledger newest end first, paged by `before`."""

    FINDING = {"path": "holophyte/serve.py", "line": 1, "severity": "p2",
               "criterion": None, "message": "a finding"}

    def seed_shipped(self):
        """Three merged runs and one failed: KO-1 merged first with one
        round of two findings; KO-2 failed; KO-3 merged last with two
        rounds of one and three findings; KO-4 merged between KO-1 and
        KO-3 by end though claimed after KO-3, with no round at all, so
        the ledger's order is not the id order."""
        self.now = int(time() * 1000)
        H = 60 * MIN
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            plan = (("KO-1", 10 * H, 9 * H, "merged", (2,), MERGE_SHA),
                    ("KO-2", 8 * H, 7 * H, "failed", (), None),
                    ("KO-3", 6 * H, 1 * H, "merged", (1, 3), "b" * 40),
                    ("KO-4", 5 * H, 4 * H, "merged", (), "c" * 40))
            self.runs = {}
            for ident, started_ago, ended_ago, outcome, findings, sha in plan:
                ticket = store.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{ident}",
                    linear_identifier=ident, title=f"ticket {ident}",
                    acceptance_criteria=[f"Given {ident}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=30 * MIN)
                store.transition(conn, ticket, "in_flight")
                started = self.now - started_ago
                run = store.claim(conn, project, ticket, now=started)
                for number, count in enumerate(findings, start=1):
                    store.record_review_round(
                        conn, run, number, "changes_requested",
                        "reviewer-model", findings=[self.FINDING] * count,
                        started_at=started + number * MIN)
                store.release(conn, run, outcome, now=self.now - ended_ago,
                              merge_sha=sha)
                self.runs[ident] = run
        finally:
            conn.close()

    def test_merged_runs_newest_end_first_with_findings_counted(self):
        self.seed_shipped()
        self.start()

        code, headers, body = self.request("GET", "/shipped")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["limit"], 50)
        self.assertIsNone(body["next_before"])
        self.assertEqual([r["ticket"] for r in body["rows"]],
                         ["KO-3", "KO-4", "KO-1"])
        self.assertEqual([r["id"] for r in body["rows"]],
                         [self.runs["KO-3"], self.runs["KO-4"],
                          self.runs["KO-1"]])
        self.assertEqual([r["findings"] for r in body["rows"]], [4, 0, 2])
        self.assertEqual([r["rounds"] for r in body["rows"]], [2, 0, 1])
        self.assertEqual([r["merge_sha"] for r in body["rows"]],
                         ["b" * 40, "c" * 40, MERGE_SHA])
        self.assertNotIn("KO-2", json.dumps(body))
        newest = body["rows"][0]
        self.assertEqual(newest["title"], "ticket KO-3")
        self.assertEqual(newest["started_ms"], self.now - 6 * 60 * MIN)
        self.assertEqual(newest["ended_ms"], self.now - 60 * MIN)
        self.assertEqual(newest["actual_min"], 300.0)
        self.assertEqual(newest["estimate_min"], 30.0)
        self.assertEqual(newest["host"], socket.gethostname())

    def test_a_client_pages_to_the_end_with_next_before(self):
        self.seed_shipped()
        self.start()

        code, _headers, first = self.request("GET", "/shipped?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(first["limit"], 2)
        self.assertEqual([r["ticket"] for r in first["rows"]],
                         ["KO-3", "KO-4"])
        self.assertEqual(first["next_before"], self.runs["KO-4"])

        code, _headers, second = self.request(
            "GET", f"/shipped?limit=2&before={first['next_before']}")
        self.assertEqual(code, 200)
        self.assertEqual([r["ticket"] for r in second["rows"]], ["KO-1"])
        self.assertIsNone(second["next_before"])

    def test_bad_parameters_are_400_and_an_unknown_before_is_empty(self):
        self.seed_shipped()
        self.start()

        for query, name in (("limit=0", "limit"), ("limit=x", "limit"),
                            ("before=x", "before")):
            with self.subTest(query=query):
                code, headers, body = self.request("GET", f"/shipped?{query}")
                self.assertEqual(code, 400)
                self.assertEqual(headers["Content-Type"], "application/json")
                self.assertIn(name, body["error"])
                self.assertNotIn("rows", body)

        # An integer no run has is an empty page, whichever side of the id
        # range it falls on: negative, or past what SQLite can bind.
        for cursor in ("99999", "-1", "0", str(2 ** 63), str(-(2 ** 63) - 1)):
            with self.subTest(before=cursor):
                code, _headers, body = self.request(
                    "GET", f"/shipped?before={cursor}")
                self.assertEqual(code, 200)
                self.assertEqual(body["rows"], [])
                self.assertIsNone(body["next_before"])

        code, _headers, body = self.request("GET", "/shipped?limit=500")
        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 200)

    def test_shipped_without_a_store_is_503_and_creates_none(self):
        self.start()

        code, _headers, body = self.request("GET", "/shipped")

        self.assertEqual(code, 503)
        self.assertIn("no store", body["error"])
        self.assertFalse(self.db.exists())


class RunDetailTests(ServeTestCase):
    """`/runs/N`: one run's row, rounds with findings as objects, and the
    narrative half of its event stream."""

    FINDINGS = [
        {"path": "holophyte/serve.py", "line": 12, "severity": "p1",
         "criterion": "AC1", "message": "the route is unmatched"},
        {"path": "docs/reference/http.md", "line": None, "severity": "nit",
         "criterion": None, "message": "no example"},
    ]

    def seed_reviewed(self, cap=None):
        """One merged run: two ended rounds, `changes_requested` with two
        findings then `pass`; three narrative events and one detail event.
        `cap` is the review-round cap the loop gave the run; None leaves the
        row as a run recorded before the store carried one."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            ticket = store.mirror_ticket(
                conn, project, linear_issue_id="issue-9",
                linear_identifier="KO-9", title="ticket 9",
                acceptance_criteria=["Given ticket 9, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN)
            store.transition(conn, ticket, "in_flight")
            started = self.now - 30 * MIN
            self.run = store.claim(conn, project, ticket, now=started)
            # `claim()` writes the first narrative rows itself; the seed's
            # own are appended after and are what the test names.
            self.seeded_events = [
                ("verify", "verify passed", "narrative"),
                ("tool_use", "ran ruff", "detail"),
                ("review", "round 1 asked for changes", "narrative"),
                ("review", "round 2 passed", "narrative"),
            ]
            for n, (kind, summary, level) in enumerate(self.seeded_events):
                store.record_event(conn, self.run, kind, summary, level=level,
                                   now=started + (n + 1) * MIN)
            store.record_review_round(
                conn, self.run, 1, "changes_requested", "reviewer-a",
                findings=self.FINDINGS, started_at=started + 5 * MIN,
                ended_at=started + 8 * MIN)
            store.record_review_round(
                conn, self.run, 2, "pass", "reviewer-b",
                started_at=started + 10 * MIN, ended_at=started + 12 * MIN)
            if cap is not None:
                store.set_review_round_cap(conn, self.run, cap)
            store.release(conn, self.run, "merged", now=started + 20 * MIN,
                          merge_sha=MERGE_SHA)
        finally:
            conn.close()

    def stored_events(self, level):
        """The oracle: the run's `runEvents` rows of `level` in `seq` order,
        read straight from the table."""
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(
                "SELECT at, kind, summary FROM runEvents"
                " WHERE runId = ? AND level = ? ORDER BY seq",
                (self.run, level)).fetchall()
        finally:
            conn.close()

    def test_rounds_oldest_first_with_findings_as_objects(self):
        self.seed_reviewed()
        self.start()

        code, headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual([r["round"] for r in body["rounds"]], [1, 2])
        self.assertEqual([r["verdict"] for r in body["rounds"]],
                         ["changes_requested", "pass"])
        self.assertEqual([r["reviewer_model"] for r in body["rounds"]],
                         ["reviewer-a", "reviewer-b"])
        # Objects, not the stored JSON string, and the two the seed wrote.
        self.assertEqual(body["rounds"][0]["findings"], self.FINDINGS)
        self.assertEqual(body["rounds"][1]["findings"], [])
        self.assertLess(body["rounds"][0]["started_ms"],
                        body["rounds"][0]["ended_ms"])
        self.assertLess(body["rounds"][0]["ended_ms"],
                        body["rounds"][1]["started_ms"])

    def test_events_are_the_narrative_rows_oldest_first_without_detail(self):
        self.seed_reviewed()
        self.start()

        code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        got = [(e["at"], e["kind"], e["summary"]) for e in body["events"]]
        self.assertEqual(got, self.stored_events("narrative"))
        # The three the seed wrote are there, in order, among the
        # `phase_change` rows `claim()` and `release()` write themselves; the
        # detail one is not: a store with detail rows still answers the
        # narrative ones.
        narrative = [(k, s) for k, s, level in self.seeded_events
                     if level == "narrative"]
        self.assertEqual([(k, s) for _at, k, s in got if k != "phase_change"],
                         narrative)
        self.assertEqual(len(self.stored_events("detail")), 1)
        self.assertNotIn("ran ruff", [s for _at, _k, s in got])

    def test_the_run_is_the_row_joined_to_its_ticket(self):
        self.seed_reviewed()
        self.start()

        _code, _headers, body = self.request("GET", f"/runs/{self.run}")

        run = body["run"]
        self.assertEqual(run["id"], self.run)
        self.assertEqual(run["ticket"], "KO-9")
        self.assertEqual(run["title"], "ticket 9")
        self.assertEqual(run["phase"], "done")
        self.assertEqual(run["attempt"], 1)
        self.assertEqual(run["outcome"], "merged")
        self.assertEqual(run["time_box_ms"], 25 * MIN)
        self.assertEqual(run["merge_sha"], MERGE_SHA)
        self.assertEqual(run["started_ms"], self.now - 30 * MIN)
        self.assertEqual(run["ended_ms"], self.now - 10 * MIN)
        # No cap stored: a run recorded before the store carried one
        # answers the loop's constant.
        self.assertEqual(run["max_rounds"], holophyte.serve.MAX_ROUNDS)
        self.assertIsInstance(run["max_rounds"], int)
        self.assertIn("branch", run)

    def test_max_rounds_is_the_cap_the_loop_gave_the_run(self):
        """A run the loop gave four rounds answers `max_rounds` 4, not the
        module constant, so the console's timeline is divided by the cap
        this run had (KO-321)."""
        self.seed_reviewed(cap=4)
        self.start()

        _code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(body["run"]["max_rounds"], 4)
        self.assertNotEqual(body["run"]["max_rounds"],
                            holophyte.serve.MAX_ROUNDS)

    def test_a_live_run_has_a_heartbeat_age_and_an_ended_one_null(self):
        self.seed()  # KO-7, live in `working`, beating 30 s ago
        self.start()

        code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        self.assertIsNone(body["run"]["ended_ms"])
        self.assertIsNone(body["run"]["outcome"])
        age = body["run"]["heartbeat_age_ms"]
        self.assertGreaterEqual(age, 30 * SEC)
        self.assertLess(age, 30 * SEC + SLACK)

    def test_an_ended_run_has_no_heartbeat_age(self):
        self.seed_reviewed()
        self.start()

        code, _headers, body = self.request("GET", f"/runs/{self.run}")

        self.assertEqual(code, 200)
        self.assertIsNotNone(body["run"]["ended_ms"])
        self.assertIsNone(body["run"]["heartbeat_age_ms"])

    def test_no_such_run_is_404_and_a_non_integer_is_400(self):
        self.seed()
        self.start()

        code, headers, body = self.request("GET", "/runs/999")
        self.assertEqual(code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("error", body)
        self.assertEqual(body["run"], 999)

        code, headers, body = self.request("GET", "/runs/abc")
        self.assertEqual(code, 400)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("error", body)

        # Integers no run can have are still integers: 404 carrying `run`
        # as typed, not 400, and not a crash past SQLite's INTEGER range.
        code, _headers, body = self.request("GET", "/runs/-1")
        self.assertEqual(code, 404)
        self.assertEqual(body["run"], "-1")
        code, _headers, body = self.request("GET", "/runs/9223372036854775808")
        self.assertEqual(code, 404)
        self.assertEqual(body["run"], "9223372036854775808")

        # `/runs` and `/runs?limit=N` answer as before.
        code, _headers, body = self.request("GET", "/runs")
        self.assertEqual(code, 200)
        self.assertEqual(body["rows"], [])
        code, _headers, body = self.request("GET", "/runs?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 2)

    def test_a_run_id_of_thousands_of_digits_answers_not_disconnects(self):
        # Regression: `int()` refuses strings past Python's digit limit
        # (4300 by default), and the handler used to die on the ValueError
        # and drop the connection. Leading zeros are normalized away, so
        # the padded existing id is that run; an id of that many
        # significant digits is 404 like any other absent one.
        self.seed()
        self.start()
        padding = "0" * 5000

        code, _headers, body = self.request(
            "GET", f"/runs/{padding}{self.run}")
        self.assertEqual(code, 200)
        self.assertEqual(body["run"]["id"], self.run)

        code, headers, body = self.request("GET", f"/runs/1{padding}")
        self.assertEqual(code, 404)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["run"], f"1{padding}")

    def test_a_target_with_no_store_answers_503(self):
        self.start()

        code, _headers, body = self.request("GET", "/runs/1")

        self.assertEqual(code, 503)
        self.assertIn("error", body)
        self.assertFalse(self.db.exists())


class PrUrlTests(ServeTestCase):
    """`pr_url` on `/runs/N`, `/attention` and `/shipped`: the pull request
    the run parked on (`runs.prUrl`), null for a run that opened none."""

    PR_URL = "https://github.com/o/r/pull/2170"

    def seed_pr(self):
        """KO-8 parked `blocked_on_operator` on the pull request; KO-9 parked
        the same way with none. Both parked runs are `lastRunId`."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            self.runs = {}
            for ident, url in (("KO-8", self.PR_URL), ("KO-9", None)):
                ticket = store.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{ident}",
                    linear_identifier=ident, title=f"ticket {ident}",
                    acceptance_criteria=[f"Given {ident}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=25 * MIN)
                store.transition(conn, ticket, "in_flight")
                run = store.claim(conn, project, ticket, now=self.now - 20 * MIN)
                store.set_phase(conn, run, "working", now=self.now - 20 * MIN)
                store.transition(conn, ticket, "blocked_on_operator")
                store.park(conn, run, "blocked_on_operator", "parked on the PR",
                           candidate_sha=MERGE_SHA, pr_url=url,
                           now=self.now - 10 * MIN)
                self.runs[ident] = run
            store.record_supervisor_heartbeat(
                conn, 4242, self.now - MIN, now=self.now - 5 * SEC)
        finally:
            conn.close()

    def merge_parked(self):
        """Both parked runs end `merged`, as the shepherd ends one whose PR
        landed."""
        conn = store.open(str(self.db))
        try:
            for run in self.runs.values():
                store.release(conn, run, "merged", now=self.now - MIN,
                              merge_sha=MERGE_SHA)
        finally:
            conn.close()

    def test_run_detail_and_attention_carry_the_parked_runs_pr_url(self):
        self.seed_pr()
        self.start()

        code, _, with_pr = self.request("GET", f"/runs/{self.runs['KO-8']}")
        self.assertEqual(code, 200)
        self.assertEqual(with_pr["run"]["pr_url"], self.PR_URL)
        code, _, without = self.request("GET", f"/runs/{self.runs['KO-9']}")
        self.assertEqual(code, 200)
        self.assertIsNone(without["run"]["pr_url"])

        code, _, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        by_ticket = {item["ticket"]: item for item in body["items"]
                     if item["kind"] == "blocked"}
        self.assertEqual(by_ticket["KO-8"]["run"], self.runs["KO-8"])
        self.assertEqual(by_ticket["KO-8"]["pr_url"], self.PR_URL)
        self.assertIsNone(by_ticket["KO-9"]["pr_url"])

    def test_shipped_carries_the_pr_url_once_the_run_merges(self):
        self.seed_pr()
        self.merge_parked()
        self.start()

        code, _, body = self.request("GET", "/shipped")

        self.assertEqual(code, 200)
        by_ticket = {row["ticket"]: row for row in body["rows"]}
        self.assertEqual(set(by_ticket), {"KO-8", "KO-9"})
        self.assertEqual(by_ticket["KO-8"]["pr_url"], self.PR_URL)
        self.assertIsNone(by_ticket["KO-9"]["pr_url"])


class RunLedgerTests(ServeTestCase):
    """`/runs/N/ledger`: the run's narrative as the store holds it, oldest
    first, with its kinds; 404 for a run the store has not seen."""

    def seed_ledger(self):
        """One merged run with three ledger entries, written newest-kind
        last but with the middle one stamped oldest, so the order the
        daemon answers is the store's `at` order and not insertion order."""
        self.now = int(time() * 1000)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            ticket = store.mirror_ticket(
                conn, project, linear_issue_id="issue-11",
                linear_identifier="KO-11", title="ticket 11",
                acceptance_criteria=["Given ticket 11, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN)
            store.transition(conn, ticket, "in_flight")
            started = self.now - 30 * MIN
            self.run = store.claim(conn, project, ticket, now=started)
            self.seeded = [
                ("round", "Round 1: changes_requested", "loop",
                 started + 5 * MIN),
                ("note", "operator looked in", "operator", started + 2 * MIN),
                ("merge", "MERGED to main", "loop", started + 20 * MIN),
            ]
            for kind, text, source, at in self.seeded:
                store.record_ledger(conn, self.run, kind, text, source=source,
                                    now=at)
            store.release(conn, self.run, "merged", now=started + 20 * MIN,
                          merge_sha=MERGE_SHA)
        finally:
            conn.close()

    def test_entries_come_back_oldest_first_with_their_kinds(self):
        self.seed_ledger()
        self.start()

        code, headers, body = self.request("GET", f"/runs/{self.run}/ledger")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["run_id"], self.run)
        self.assertEqual(body["ticket"], "KO-11")
        self.assertEqual(
            body["entries"],
            [{"at": at, "kind": kind, "text": text, "source": source}
             for kind, text, source, at in sorted(self.seeded,
                                                  key=lambda e: e[3])])
        self.assertEqual([e["kind"] for e in body["entries"]],
                         ["note", "round", "merge"])

    def test_no_such_run_is_404_and_a_non_integer_is_400(self):
        self.seed_ledger()
        self.start()

        code, _, body = self.request("GET", f"/runs/{self.run + 1}/ledger")
        self.assertEqual(code, 404)
        self.assertEqual(body["run"], self.run + 1)
        self.assertIn("error", body)

        code, _, body = self.request("GET", "/runs/eleven/ledger")
        self.assertEqual(code, 400)
        self.assertIn("error", body)


class LedgerWindowTests(ServeTestCase):
    """`/ledger?since=MS`: the ledger across runs newest first, narrowed by
    `kind` or `ticket`; 400 naming a bad parameter."""

    def seed_ledger(self):
        """Two merged runs on two tickets with one entry each of `merge`,
        `intervention` and `round` at T1 < T2 < T3, the newest written
        first so the answer's order is the store's `at` order."""
        self.now = int(time() * 1000)
        started = self.now - 60 * MIN
        self.t1, self.t2, self.t3 = (started + 5 * MIN, started + 10 * MIN,
                                     started + 20 * MIN)
        conn = store.open(str(self.db))
        try:
            store.init(conn)
            project = store.ensure_project(conn, "team-1", self.target)
            self.runs = {}
            self.seeded = []
            # One lease per project: each run is claimed, written and
            # released before the next; the entries themselves are stamped
            # newest first within a run, so the answer's order is `at`.
            for n, entries in ((12, [(self.t3, "round", "Round 1: pass",
                                      "loop")]),
                               (11, [(self.t2, "intervention",
                                      "answered: ship it", "operator"),
                                     (self.t1, "merge", "MERGED to main",
                                      "loop")])):
                ticket = store.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{n}",
                    linear_identifier=f"KO-{n}", title=f"ticket {n}",
                    acceptance_criteria=[f"Given ticket {n}, then worked"],
                    verification_commands=["echo ok"], time_box_ms=25 * MIN)
                store.transition(conn, ticket, "in_flight")
                run = store.claim(conn, project, ticket, now=started)
                for at, kind, text, source in entries:
                    store.record_ledger(conn, run, kind, text, source=source,
                                        now=at)
                    self.seeded.append((at, run, f"KO-{n}", kind, text,
                                        source))
                store.release(conn, run, "merged", now=started + 30 * MIN,
                              merge_sha=MERGE_SHA)
        finally:
            conn.close()

    @staticmethod
    def entry(at, run, ticket, kind, text, source):
        body = {"at": at, "run": run, "ticket": ticket, "kind": kind,
                "source": source, "text": text}
        if kind == "intervention":
            # KO-308: nothing waited before this seeded step, so the two
            # fields ride along as null.
            body.update(cleared=None, waited_ms=None)
        return body

    def test_window_is_newest_first_and_leaves_out_older_rows(self):
        self.seed_ledger()
        self.start()

        code, headers, body = self.request("GET", f"/ledger?since={self.t2}")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["since"], self.t2)
        self.assertEqual(body["limit"], 200)
        self.assertEqual(body["entries"],
                         [self.entry(*self.seeded[0]),
                          self.entry(*self.seeded[1])])
        self.assertNotIn(self.t1, [e["at"] for e in body["entries"]])

    def test_kind_and_ticket_narrow_the_window(self):
        self.seed_ledger()
        self.start()

        code, _, body = self.request(
            "GET", f"/ledger?since={self.t1}&kind=intervention")
        self.assertEqual(code, 200)
        self.assertEqual(body["entries"], [self.entry(*self.seeded[1])])

        code, _, body = self.request(
            "GET", f"/ledger?since={self.t1}&ticket=KO-11")
        self.assertEqual(code, 200)
        self.assertEqual(body["entries"], [self.entry(*self.seeded[1]),
                                           self.entry(*self.seeded[2])])

        code, _, body = self.request(
            "GET", f"/ledger?since={self.t1}&limit=1")
        self.assertEqual(code, 200)
        self.assertEqual(body["limit"], 1)
        self.assertEqual(body["entries"], [self.entry(*self.seeded[0])])

    def test_bad_parameters_are_400_naming_them(self):
        self.seed_ledger()
        self.start()

        for query, name in (("", "since"), ("since=x", "since"),
                            (f"since={self.t1}&limit=0", "limit"),
                            (f"since={self.t1}&kind=nope", "kind")):
            with self.subTest(query=query):
                code, _, body = self.request("GET", f"/ledger?{query}")
                self.assertEqual(code, 400)
                self.assertIn(name, body["error"])


class LedgerWaitTests(ServeTestCase):
    """KO-308: an `intervention` entry of either ledger read says what the
    operator's step cleared and how long that waited, from the entry's own
    run: the newest `redirect` strictly before it or the run's `endedAt`,
    whichever is newer; other kinds carry neither field."""

    def open_store(self):
        conn = store.open(str(self.db))
        store.init(conn)
        self.project = store.ensure_project(conn, "team-1", self.target)
        return conn

    def claim(self, conn, n, now):
        ticket = store.mirror_ticket(
            conn, self.project, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            acceptance_criteria=[f"Given ticket {n}, then worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MIN)
        store.transition(conn, ticket, "in_flight")
        return ticket, store.claim(conn, self.project, ticket, now=now)

    @staticmethod
    def ask(conn, run, now):
        store.record_intervention(
            conn, run, "redirect", "asked the operator", source="supervisor",
            trigger="off_criteria", question="Which flag name?", now=now)

    @staticmethod
    def interventions(entries):
        return [e for e in entries if e["kind"] == "intervention"]

    def test_a_resume_clears_the_question_and_the_redirect_pairs_with_nothing(self):
        now = int(time() * 1000)
        started = now - 60 * MIN
        t1, t2 = started + 5 * MIN, started + 18 * MIN
        conn = self.open_store()
        try:
            _, run = self.claim(conn, 21, started)
            self.ask(conn, run, t1)
            store.record_intervention(conn, run, "resume", "answered: keep it",
                                      now=t2)
        finally:
            conn.close()
        self.start()

        code, _, body = self.request("GET", "/ledger?since=0")

        self.assertEqual(code, 200)
        resume, redirect = self.interventions(body["entries"])
        self.assertEqual(resume["at"], t2)
        self.assertEqual(resume["cleared"], "question")
        self.assertEqual(resume["waited_ms"], t2 - t1)
        self.assertEqual(redirect["at"], t1)
        self.assertIsNone(redirect["cleared"])
        self.assertIsNone(redirect["waited_ms"])

    def test_a_requeue_after_a_failure_clears_the_failure(self):
        now = int(time() * 1000)
        started = now - 60 * MIN
        t1, t2 = started + 9 * MIN, started + 40 * MIN
        conn = self.open_store()
        try:
            ticket, run = self.claim(conn, 22, started)
            store.release(conn, run, "failed", reason="verify red", now=t1)
            store.requeue(conn, ticket, "operator requeued", now=t2)
        finally:
            conn.close()
        self.start()

        code, _, body = self.request("GET", f"/runs/{run}/ledger")

        self.assertEqual(code, 200)
        (requeue,) = self.interventions(body["entries"])
        self.assertEqual(requeue["at"], t2)
        self.assertEqual(requeue["cleared"], "failed")
        self.assertEqual(requeue["waited_ms"], t2 - t1)

    def test_the_newer_mark_wins_and_other_kinds_carry_neither_field(self):
        now = int(time() * 1000)
        started = now - 60 * MIN
        t1, t2, t3 = started + 5 * MIN, started + 12 * MIN, started + 30 * MIN
        conn = self.open_store()
        try:
            # KO-23 asked at T1, failed at T2: the failure is the newer mark.
            ticket_a, run_a = self.claim(conn, 23, started)
            store.record_ledger(conn, run_a, "round", "Round 1: pass",
                                now=started + MIN)
            self.ask(conn, run_a, t1)
            store.release(conn, run_a, "failed", reason="verify red", now=t2)
            store.requeue(conn, ticket_a, "operator requeued", now=t3)
            # KO-24 failed at T1, asked at T2: the question is the newer mark.
            ticket_b, run_b = self.claim(conn, 24, started + MIN)
            store.release(conn, run_b, "failed", reason="verify red", now=t1)
            self.ask(conn, run_b, t2)
            store.requeue(conn, ticket_b, "operator requeued", now=t3)
            # KO-25 asked and failed in the same millisecond T2: the
            # redirect is not strictly newer, so the failure wins.
            ticket_c, run_c = self.claim(conn, 25, started + 2 * MIN)
            self.ask(conn, run_c, t2)
            store.release(conn, run_c, "failed", reason="verify red", now=t2)
            store.requeue(conn, ticket_c, "operator requeued", now=t3)
        finally:
            conn.close()
        self.start()

        for run, cleared, mark in ((run_a, "failed", t2),
                                   (run_b, "question", t2),
                                   (run_c, "failed", t2)):
            with self.subTest(run=run, cleared=cleared):
                code, _, body = self.request("GET", f"/runs/{run}/ledger")
                self.assertEqual(code, 200)
                requeue = [e for e in self.interventions(body["entries"])
                           if e["at"] == t3]
                self.assertEqual(len(requeue), 1)
                self.assertEqual(requeue[0]["cleared"], cleared)
                self.assertEqual(requeue[0]["waited_ms"], t3 - mark)

        code, _, body = self.request("GET", f"/runs/{run_a}/ledger")
        others = [e for e in body["entries"] if e["kind"] != "intervention"]
        self.assertIn("round", [e["kind"] for e in others])
        for entry in others:
            self.assertNotIn("cleared", entry)
            self.assertNotIn("waited_ms", entry)


class RunFilesTests(ServeTestCase):
    """`/runs/N/files`: the paths a run touched, from git in the target's
    checkout, for a live branch and for a landed merge."""

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.target, check=True,
                              capture_output=True, text=True).stdout.strip()

    def build_repo(self):
        """`main` with two files; `task/ko-7` adding `new.txt` (2 lines)
        and rewriting one of `kept.txt`'s three lines; a binary blob too,
        on the branch, so its zero counts are witnessed."""
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "factory")
        (self.target / "kept.txt").write_text("one\ntwo\nthree\n")
        (self.target / "other.txt").write_text("untouched\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "base")
        self.branch = "task/ko-7"
        self.git("checkout", "-q", "-b", self.branch)
        (self.target / "new.txt").write_text("alpha\nbeta\n")
        (self.target / "kept.txt").write_text("one\nTWO\nthree\n")
        (self.target / "blob.bin").write_bytes(bytes(range(256)))
        self.git("add", ".")
        self.git("commit", "-q", "-m", "work")
        self.git("checkout", "-q", "main")
        # main moves on after the branch was cut: a live run's range must
        # start at the merge base, not at main's head.
        (self.target / "other.txt").write_text("main moved\n")
        self.git("commit", "-q", "-am", "main moves on")

    EXPECTED = [
        {"path": "blob.bin", "status": "A", "added": 0, "deleted": 0},
        {"path": "kept.txt", "status": "M", "added": 1, "deleted": 1},
        {"path": "new.txt", "status": "A", "added": 2, "deleted": 0},
    ]

    def set_branch(self, branch):
        conn = sqlite3.connect(str(self.db))
        try:
            conn.execute("UPDATE runs SET branch = ? WHERE id = ?",
                         (branch, self.run))
            conn.commit()
        finally:
            conn.close()

    def merge(self):
        """Land the branch on main with `--no-ff` and release the run as
        merged with the merge commit's sha, as the loop does."""
        self.git("merge", "--no-ff", "-q", "-m", "land ko-7", self.branch)
        sha = self.git("rev-parse", "HEAD")
        conn = store.open(str(self.db))
        try:
            store.release(conn, self.run, "merged", now=self.now,
                          merge_sha=sha)
        finally:
            conn.close()
        return sha

    def setUp(self):
        super().setUp()
        self.build_repo()
        self.seed()
        self.set_branch(self.branch)
        self.start()

    def test_a_live_run_lists_its_branch_against_the_merge_base(self):
        code, headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 200, body)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["files"], self.EXPECTED)
        self.assertEqual(body["run"], self.run)
        self.assertEqual(body["base"], self.git("merge-base", "main", self.branch))
        self.assertEqual(body["head"], self.git("rev-parse", self.branch))
        self.assertEqual(body["total_added"], 3)
        self.assertEqual(body["total_deleted"], 1)
        self.assertFalse(body["truncated"])

    def test_a_merged_run_lists_the_merge_against_its_first_parent(self):
        sha = self.merge()
        # The branch is gone, as a close-out may leave it: the merge sha
        # alone must carry the answer.
        self.git("branch", "-D", self.branch)
        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["head"], sha)
        self.assertEqual(body["base"], self.git("rev-parse", f"{sha}^1"))
        self.assertEqual(body["files"], self.EXPECTED)
        self.assertEqual((body["total_added"], body["total_deleted"]), (3, 1))

    def test_a_deleted_branch_is_409_naming_it_and_no_run_is_404(self):
        self.git("branch", "-D", self.branch)
        code, headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 409, body)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn(self.branch, body["error"])

        self.set_branch(None)
        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 409, body)
        self.assertIn("error", body)

        code, _headers, body = self.request("GET", "/runs/999/files")
        self.assertEqual(code, 404)
        self.assertEqual(body, {"error": "no such run", "run": 999})
        code, _headers, body = self.request("GET", "/runs/abc/files")
        self.assertEqual(code, 400)

    def test_a_deleted_branch_is_409_even_when_a_same_name_tag_survives(self):
        # A bare name would fall through to the tag and answer 200; the
        # run's branch is gone and the endpoint must say so.
        self.git("tag", self.branch, self.branch)
        self.git("branch", "-D", self.branch)
        code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 409, body)
        self.assertIn(self.branch, body["error"])

    def test_the_list_is_capped_and_truncated_past_it(self):
        with patch.object(holophyte.files, "MAX_FILES", 2):
            code, _headers, body = self.request("GET", f"/runs/{self.run}/files")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["files"], self.EXPECTED[:2])
        self.assertTrue(body["truncated"])
        # The totals still count the whole diff.
        self.assertEqual((body["total_added"], body["total_deleted"]), (3, 1))


class ActionsTests(ServeTestCase):
    """`POST /actions/...` (KO-348): 404 on every daemon without `[serve]
    actions = true`; with it, the unit actions run `systemctl --user`
    against the `[serve] name` instance behind the token -- on a loopback
    bind as much as any other -- after their interventions row, a failed
    `systemctl` is `ok: false` carrying its stderr, and `requeue` is the
    store's own requeue with its interventions row."""

    TOKEN = TokenTests.TOKEN
    BEARER = TokenTests.BEARER

    def token_config(self, extra=""):
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        return f'[serve]\ntoken_file = "{path}"\n{extra}'

    def completed(self, argv, returncode=0, stderr=""):
        return subprocess.CompletedProcess(argv, returncode, stdout="",
                                           stderr=stderr)

    def test_without_the_opt_in_every_actions_route_is_404_with_the_token(self):
        self.seed()
        self.start(self.token_config(), host="0.0.0.0")
        with patch.object(subprocess, "run") as run:
            for action in ("restart-supervisor", "launch-loop", "requeue"):
                with self.subTest(action=action):
                    code, _, body = self.request(
                        "POST", f"/actions/{action}", self.BEARER,
                        body={"ticket": "KO-7"})
                    self.assertEqual(code, 404)
                    self.assertEqual(body["error"], "not found")
        run.assert_not_called()
        conn = store.read.open_readonly(self.db)
        try:
            self.assertEqual(store.read.ledger(conn, self.run), [])
        finally:
            conn.close()

    def test_restart_supervisor_runs_systemctl_against_the_named_instance(self):
        self.seed()
        self.start(self.token_config('actions = true\nname = "writer-a"\n'),
                   host="0.0.0.0")
        with patch.object(subprocess, "run") as run:
            code, _, body = self.request("POST", "/actions/restart-supervisor")
            self.assertEqual(code, 401)
            self.assertEqual(body, {})
            run.assert_not_called()

            run.side_effect = lambda argv, **kw: self.completed(argv)
            code, _, body = self.request("POST", "/actions/restart-supervisor",
                                         self.BEARER)
        self.assertEqual(code, 200)
        self.assertEqual(body["action"], "restart-supervisor")
        self.assertIs(body["ok"], True)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["systemctl", "--user", "restart",
                                "holophyte-supervise@writer-a"])
        self.assertEqual(run.call_args.kwargs["timeout"], 20)
        self.assertEqual(body["recorded"], self.run)
        # The row lands before the unit is touched: a human
        # `restart_supervisor` intervention on the store's newest run, its
        # ledger copy naming the unit and the route.
        conn = store.read.open_readonly(self.db)
        try:
            rows = conn.execute(
                'SELECT runId, source, "trigger", "action" FROM interventions'
            ).fetchall()
            entries = store.read.ledger(conn, self.run)
        finally:
            conn.close()
        self.assertEqual(rows, [(self.run, "human", "manual",
                                 "restart_supervisor")])
        self.assertEqual([(e.kind, e.source) for e in entries],
                         [("intervention", "operator")])
        self.assertIn("holophyte-supervise@writer-a", entries[0].text)
        self.assertIn("restart-supervisor", entries[0].text)

    def test_a_preflight_for_an_action_grants_the_post_and_its_json_body(self):
        # The console on another daemon's page asks before posting an
        # action with the bearer and a JSON Content-Type; a preflight
        # that named only GET would have the browser block the click.
        self.seed()
        self.start(self.token_config("actions = true\n"), host="0.0.0.0")

        with patch.object(store.read, "open_readonly") as opened:
            code, headers, raw = self.fetch(
                "OPTIONS", "/actions/restart-supervisor",
                {"Origin": "http://page.example:7710",
                 "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers":
                     "authorization, content-type"})
        self.assertEqual(code, 204)
        self.assertEqual(raw, b"")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("POST", headers["Access-Control-Allow-Methods"])
        allowed = headers["Access-Control-Allow-Headers"].lower()
        self.assertIn("authorization", allowed)
        self.assertIn("content-type", allowed)
        opened.assert_not_called()

    def test_status_advertises_the_opt_in(self):
        # KO-349: the console reads `actions` before a click, so a daemon
        # without the routes draws its buttons disabled instead of
        # posting into a 404.
        self.seed()
        self.start(self.token_config('actions = true\nname = "writer-a"\n'))
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertIs(body["actions"], True)

    def test_a_loopback_bind_demands_the_token_for_actions(self):
        """The bind address guards reads, not the units: on loopback `/status`
        stays open while `POST /actions/...` is 401 without the bearer and
        runs nothing, then 200 with it."""
        self.seed()
        self.start(self.token_config('actions = true\nname = "writer-a"\n'))
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertIn("runs", body)
        with patch.object(subprocess, "run") as run:
            for headers in (None, {"Authorization": "Bearer wrong"}):
                with self.subTest(headers=headers):
                    code, _, body = self.request(
                        "POST", "/actions/restart-supervisor", headers)
                    self.assertEqual(code, 401)
                    self.assertEqual(body, {})
            run.assert_not_called()
            run.side_effect = lambda argv, **kw: self.completed(argv)
            code, _, body = self.request("POST", "/actions/restart-supervisor",
                                         self.BEARER)
        self.assertEqual(code, 200)
        self.assertIs(body["ok"], True)
        self.assertEqual(run.call_args.args[0],
                         ["systemctl", "--user", "restart",
                          "holophyte-supervise@writer-a"])
        conn = store.read.open_readonly(self.db)
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM interventions").fetchone()
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_actions_without_a_token_file_are_a_startup_error_on_loopback(self):
        self.seed()
        (self.db.parent / "config.toml").write_text("[serve]\nactions = true\n")
        tgt = holophyte.target.Target.locate(self.target)
        with self.assertRaises(SystemExit) as raised:
            holophyte.serve.serve(tgt, "127.0.0.1:0", out=io.StringIO())
        message = str(raised.exception)
        self.assertIn("[serve] token_file", message)
        self.assertIn("actions", message)

    def test_a_unit_action_with_no_run_to_record_against_does_not_run(self):
        """A store with no run has no row to hang the intervention on, and
        record-before-acting means the unit is left alone: `ok: false`
        naming why, `systemctl` never called. A target with no store is the
        same answer."""
        conn = store.open(str(self.db))
        try:
            store.init(conn)
        finally:
            conn.close()
        self.start(self.token_config("actions = true\n"))
        with patch.object(subprocess, "run") as run:
            code, _, body = self.request("POST", "/actions/launch-loop",
                                         self.BEARER)
            run.assert_not_called()
        self.assertEqual(code, 200)
        self.assertIs(body["ok"], False)
        self.assertIsNone(body["recorded"])
        self.assertIn("no run to record", body["detail"])
        # The same config, a second daemon, and no store at all.
        self.db.unlink()
        self.start()
        with patch.object(subprocess, "run") as run:
            code, _, body = self.request("POST", "/actions/launch-loop",
                                         self.BEARER)
            run.assert_not_called()
        self.assertEqual(code, 200)
        self.assertIs(body["ok"], False)

    def test_launch_loop_reports_a_failed_systemctl_as_ok_false(self):
        self.seed()
        self.start(self.token_config("actions = true\n"))
        stderr = "Failed to start holophyte-loop@repo.service: Unit not found."
        with patch.object(subprocess, "run") as run:
            run.side_effect = lambda argv, **kw: self.completed(
                argv, returncode=1, stderr=stderr + "\n")
            code, _, body = self.request("POST", "/actions/launch-loop",
                                         self.BEARER)
        self.assertEqual(code, 200)
        self.assertEqual(body["action"], "launch-loop")
        self.assertIs(body["ok"], False)
        self.assertIn(stderr, body["detail"])
        # The instance defaults to the target directory's name.
        self.assertEqual(run.call_args.args[0],
                         ["systemctl", "--user", "start", "holophyte-loop@repo"])

    def test_requeue_walks_a_failed_ticket_to_ready_with_its_intervention(self):
        self.seed_ended()
        self.start(self.token_config("actions = true\n"), host="0.0.0.0")
        code, _, body = self.request("POST", "/actions/requeue", self.BEARER,
                                     body={"ticket": "KO-2",
                                           "note": "the verify was flaky"})
        self.assertEqual(code, 200)
        self.assertEqual(body["action"], "requeue")
        self.assertIs(body["ok"], True)
        conn = store.read.open_readonly(self.db)
        try:
            ticket = store.read.ticket_by_identifier(conn, "KO-2")
            entries = store.read.ledger_since(conn, 0, kind="intervention",
                                              ticket="KO-2")
        finally:
            conn.close()
        self.assertEqual(ticket.status, "ready")
        self.assertEqual(len(entries), 1)
        self.assertIn("requeue", entries[0].text)
        self.assertIn("the verify was flaky", entries[0].text)
        self.assertEqual(entries[0].source, "operator")

        # A merged ticket is refused by the store, and that is `ok: false`
        # with the refusal, not a 500; an unmirrored one the same.
        for ticket_id, fragment in (("KO-1", "merged"), ("KO-99", "no such")):
            with self.subTest(ticket=ticket_id):
                code, _, body = self.request("POST", "/actions/requeue",
                                             self.BEARER,
                                             body={"ticket": ticket_id})
                self.assertEqual(code, 200)
                self.assertIs(body["ok"], False)
                self.assertIn(fragment, body["detail"])
        code, _, body = self.request("POST", "/actions/requeue", self.BEARER,
                                     body={})
        self.assertEqual(code, 400)
        self.assertIn("ticket", body["error"])

    def test_requeue_refuses_an_identifier_the_store_holds_twice(self):
        # The CLI's `--requeue` refuses to pick one of two tickets named
        # alike; the route must refuse the same way, and neither may move.
        self.seed_ended()
        conn = store.open(str(self.db))
        try:
            project = store.ensure_project(conn, "team-1", self.target)
            twin = store.mirror_ticket(
                conn, project, linear_issue_id="issue-KO-2-twin",
                linear_identifier="KO-2", title="ticket KO-2 again",
                acceptance_criteria=["Given KO-2, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=20 * MIN)
            store.transition(conn, twin, "in_flight")
            run = store.claim(conn, project, twin, now=self.now - 30 * MIN)
            store.release(conn, run, "failed", now=self.now - 20 * MIN)
        finally:
            conn.close()

        def twins():
            conn = store.read.open_readonly(self.db)
            try:
                statuses = [row[0] for row in conn.execute(
                    "SELECT status FROM tickets WHERE linearIdentifier = 'KO-2'"
                    " ORDER BY id")]
                entries = store.read.ledger_since(conn, 0, kind="intervention",
                                                  ticket="KO-2")
            finally:
                conn.close()
            return statuses, entries

        before = twins()
        self.assertEqual(len(before[0]), 2)
        self.assertNotIn("ready", before[0])
        self.start(self.token_config("actions = true\n"), host="0.0.0.0")
        code, _, body = self.request("POST", "/actions/requeue", self.BEARER,
                                     body={"ticket": "KO-2", "note": "retry"})
        self.assertEqual(code, 200)
        self.assertIs(body["ok"], False)
        self.assertIn("2 tickets", body["detail"])
        after = twins()
        self.assertEqual(after, before)
        self.assertNotIn("ready", after[0])
        self.assertEqual(after[1], [])


class ConfigEditTests(ServeTestCase):
    """`GET /config` and `PUT /config` (KO-356): 404 without `[serve]
    config_edit = true`; with it, behind the token on every bind, the file
    redacted on the way out, the secret put back and the document held to
    the loader on the way in, the previous text kept beside it. `[serve]`
    accepts no secret value of its own, so the secret sits in a table the
    loader leaves alone, as a later version's key would."""

    TOKEN = TokenTests.TOKEN
    BEARER = TokenTests.BEARER
    SECRET = "lin_api_0123456789abcdef"

    HOOK_TOKENS = ("hook_0123", "hook_4567")

    def config(self, extra="", loop="[loop]\nworkers = 2\n"):
        """A file `config.check_document()` accepts as written: `[serve]`
        and `[loop]` are loader-read tables, `[linear]` and `[[hooks]]`
        (an array of tables) are left alone by this version and hold the
        secrets, `[worktree] setup` is a table the loader parses."""
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        return (f'[serve]\ntoken_file = "{path}"\n{extra}'
                f'\n{loop}\n[worktree]\nsetup = ["make deps"]\n'
                f'\n[linear]\napi_key = "{self.SECRET}"  # board\n'
                f'\n[[hooks]]\ntoken = "{self.HOOK_TOKENS[0]}"\n'
                f'[[hooks]]\ntoken = "{self.HOOK_TOKENS[1]}"\n')

    def redacted(self, text):
        """`text` with every secret the fixture placed replaced, the
        expected reply computed from the fixture's own literal secrets
        rather than from the redaction under test."""
        for secret in (self.SECRET, *self.HOOK_TOKENS):
            text = text.replace(f'"{secret}"', '"[redacted]"')
        return text

    def assert_loader_valid(self, text):
        """`text`, on disk, is a document the loop's startup accepts."""
        (self.db.parent / "config.toml").write_text(text)
        tgt = holophyte.target.Target.locate(self.target)
        self.assertIsNone(holophyte.config.check_document(tgt))

    def on_disk(self):
        return (self.db.parent / "config.toml").read_text()

    def test_without_the_opt_in_both_routes_are_404_with_the_token(self):
        self.seed()
        before = self.config()
        self.start(before, host="0.0.0.0")
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual((code, body["error"]), (404, "not found"))
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": "[loop]\nworkers = 3\n"})
        self.assertEqual((code, body["error"]), (404, "not found"))
        self.assertEqual(self.on_disk(), before)
        self.assertEqual(list(self.db.parent.glob("config.toml.bak-*")), [])

    def test_get_redacts_secret_values_and_keeps_the_token_file_path(self):
        """The route over a file startup accepts: the reply is the file
        with the two secret shapes -- a nested key and an array-of-tables
        entry -- redacted and the `token_file` path, comment and layout
        byte for byte as written."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.assert_loader_valid(before)
        self.start(before)
        code, _, body = self.request("GET", "/config")
        self.assertEqual((code, body), (401, {}))
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual(code, 200)
        for secret in (self.SECRET, *self.HOOK_TOKENS, self.TOKEN):
            self.assertNotIn(secret, self.raw_body, secret)
        expected = self.redacted(before)
        self.assertNotEqual(expected, before)
        self.assertEqual(body["text"], expected)
        self.assertIn(f'token_file = "{self.root / "serve.token"}"',
                      body["text"])
        self.assertEqual(body["text"].count("[redacted]"), 3)
        self.assertEqual(body["path"], str(self.db.parent / "config.toml"))
        self.assertEqual(body["applies"], "next loop start")

    def test_a_document_startup_refuses_for_its_carry_is_400(self):
        """`[worktree] carry = ["../outside"]` passes no startup; the
        first candidate omitted the check and wrote it (review, P1)."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before.replace('setup = ["make deps"]',
                              'setup = ["make deps"]\ncarry = ["../outside"]')
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400, body)
        self.assertIn("[worktree] carry", body["error"])
        self.assertEqual(self.on_disk(), before)

    def test_a_table_that_is_not_a_table_is_400_naming_it(self):
        """`worktree = "invalid"` reached the loader's first `.get()` as
        a string: a traceback in the handler and a dropped connection
        instead of the 400 (review, P2). Now the loader's own sentence."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        for text, table in (
            ('worktree = "invalid"\n'
             + before.replace('[worktree]\nsetup = ["make deps"]\n', ""),
             "[worktree]"),
            ("agents = 3\n" + before, "[agents]"),
        ):
            with self.subTest(table=table):
                code, _, body = self.request("PUT", "/config", self.BEARER,
                                             body={"text": text})
                self.assertEqual(code, 400, body)
                self.assertIn(f"{table} must be a table", body["error"])
                self.assertEqual(self.on_disk(), before)

    def test_a_document_the_loader_refuses_is_400_and_leaves_the_file(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before, host="0.0.0.0")
        code, _, body = self.request(
            "PUT", "/config", self.BEARER,
            body={"text": before.replace("workers = 2", "workers = 0")})
        self.assertEqual(code, 400)
        self.assertIs(body["ok"], False)
        self.assertIn("[loop] workers", body["error"])
        self.assertEqual(self.on_disk(), before)
        self.assertEqual(list(self.db.parent.glob("config.toml.bak-*")), [])
        conn = store.read.open_readonly(self.db)
        try:
            self.assertEqual(store.read.ledger(conn, self.run), [])
        finally:
            conn.close()

    def test_a_valid_put_keeps_the_secret_backs_up_and_records(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        _, _, shown = self.request("GET", "/config", self.BEARER)
        edited = shown["text"].replace("workers = 2", "workers = 3")
        self.assertIn("[redacted]", edited)
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        self.assertEqual(code, 200, body)
        self.assertIs(body["ok"], True)
        after = self.on_disk()
        self.assertIn("workers = 3", after)
        self.assertIn(f'api_key = "{self.SECRET}"  # board', after)
        self.assertNotIn("[redacted]", after)
        backup = Path(body["backup"])
        self.assertEqual(backup.parent, self.db.parent)
        self.assertTrue(backup.name.startswith("config.toml.bak-"))
        self.assertEqual(backup.read_text(), before)
        conn = store.read.open_readonly(self.db)
        try:
            rows = conn.execute(
                'SELECT runId, source, "trigger", "action" FROM interventions'
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(self.run, "human", "manual", "config_edit")])
        self.assertEqual(body["recorded"], self.run)
        # And the written file is what the loop will read at its next start.
        tgt = holophyte.target.Target.locate(self.target)
        self.assertEqual(holophyte.config.loop_config(tgt).workers, 3)

    def test_a_redacted_value_the_file_never_held_is_400(self):
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before + '\n[other]\ntoken = "[redacted]"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400)
        self.assertIn("[other] token", body["error"])
        self.assertEqual(self.on_disk(), before)

    def shapes_config(self):
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        return (
            f'[serve]\ntoken_file = "{path}"\nconfig_edit = true\n'
            '[plain]\ntoken = "S-serve"\n'
            '[quoted]\n"api key" = "S-quoted" # comment\n'
            '[dotted]\nkeep.name = "shown"\n'
            "[inline]\nboard = { api_key = 'S-inline', team = \"t\" }\n"
            '[multi]\ntoken = """\nline one\nline two"""\n'
            "[literal]\nkey = 'S-literal'\n"
            '[[many]]\ntoken = "S-first"\n[[many]]\ntoken = "S-second"\n')

    def test_get_redacts_every_toml_shape_a_secret_can_take(self):
        """Quoted, dotted and inline-table keys, multi-line and literal
        strings, arrays of tables: `tomllib` over the shown text is the
        oracle -- every secret leaf reads `[redacted]`, nothing else moved."""
        self.seed()
        before = self.shapes_config().replace(
            'keep.name = "shown"', 'keep.name = "shown"\nkeep.api_key = "S-dotted"')
        self.assert_loader_valid(before)
        self.start(before)
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual(code, 200, body)
        for secret in ("S-serve", "S-quoted", "S-dotted", "S-inline",
                       "line one", "S-literal", "S-first", "S-second",
                       self.TOKEN):
            self.assertNotIn(secret, self.raw_body, secret)
        shown = tomllib.loads(body["text"])
        expected = tomllib.loads(before)
        self.assertEqual(shown["plain"]["token"], "[redacted]")
        self.assertEqual(shown["quoted"]["api key"], "[redacted]")
        self.assertEqual(shown["dotted"]["keep"]["api_key"], "[redacted]")
        self.assertEqual(shown["inline"]["board"]["api_key"], "[redacted]")
        self.assertEqual(shown["multi"]["token"], "[redacted]")
        self.assertEqual(shown["literal"]["key"], "[redacted]")
        self.assertEqual([m["token"] for m in shown["many"]],
                         ["[redacted]", "[redacted]"])
        # The rest of the document is untouched, comment included.
        self.assertEqual(shown["serve"]["token_file"],
                         expected["serve"]["token_file"])
        self.assertEqual(shown["inline"]["board"]["team"], "t")
        self.assertEqual(shown["dotted"]["keep"]["name"], "shown")
        self.assertIn('"api key" = "[redacted]" # comment', body["text"])

    def test_a_placeholder_with_a_comment_or_other_quoting_is_restored(self):
        """`api_key = "[redacted]" # kept` and `'[redacted]'` are the
        placeholder too: what is written carries the secret, not them."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        edited = before.replace(
            f'api_key = "{self.SECRET}"  # board',
            "api_key = '[redacted]' # kept")
        edited += '\n[extra]\nnote = "x"\n'
        edited = edited.replace('\n[extra]', '\n[linear.more]\n'
                                'key = "[redacted]" # also kept\n[extra]')
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        # `[linear.more] key` was never held: refused, named, nothing written.
        self.assertEqual(code, 400, body)
        self.assertIn("[linear.more] key", body["error"])
        self.assertEqual(self.on_disk(), before)
        edited = before.replace(
            f'api_key = "{self.SECRET}"  # board',
            "api_key = '[redacted]' # kept")
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        self.assertEqual(code, 200, body)
        after = self.on_disk()
        self.assertIn(f'api_key = "{self.SECRET}" # kept', after)
        self.assertNotIn("[redacted]", after)
        self.assertEqual(tomllib.loads(after)["linear"]["api_key"],
                         self.SECRET)

    def test_a_document_startup_refuses_for_its_board_is_400(self):
        """`[board] project_id = 123` passes no startup; it passes no PUT."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before + '\n[board]\nproject_id = 123\nteam = "T"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400, body)
        self.assertIn("[board] project_id", body["error"])
        self.assertEqual(self.on_disk(), before)

    def test_a_secret_inside_an_array_is_redacted_and_restored_in_place(self):
        """`items = [{token = "S"}, 1]` is a secret in an array element; the
        earlier walk stepped over arrays and served it. Redacted on the way
        out, and put back by its position on the way in, whatever the value
        beside it became."""
        self.seed()
        before = self.config("config_edit = true\n") + (
            '\n[extra]\nitems = [{token = "S-array", n = 1}, 1,'
            ' [{key = "S-nested"}]]\n')
        self.start(before)
        code, _, body = self.request("GET", "/config", self.BEARER)
        self.assertEqual(code, 200, body)
        self.assertNotIn("S-array", self.raw_body)
        self.assertNotIn("S-nested", self.raw_body)
        shown = tomllib.loads(body["text"])["extra"]["items"]
        self.assertEqual(shown[0], {"token": "[redacted]", "n": 1})
        self.assertEqual(shown[2], [{"key": "[redacted]"}])
        edited = body["text"].replace("n = 1", "n = 2")
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        self.assertEqual(code, 200, body)
        after = tomllib.loads(self.on_disk())["extra"]["items"]
        self.assertEqual(after, [{"token": "S-array", "n": 2}, 1,
                                 [{"key": "S-nested"}]])

    def test_a_placeholder_in_an_array_of_tables_takes_its_own_entry(self):
        """Two `[[many]]` entries, the first token rewritten by hand and the
        second left as the placeholder: the second gets its own secret back,
        not the first's. Values are matched by array position, not by the
        order the placeholders happen to appear."""
        self.seed()
        before = self.config("config_edit = true\n") + (
            '\n[[many]]\ntoken = "S-first"\n[[many]]\ntoken = "S-second"\n')
        self.start(before)
        edited = before.replace('token = "S-first"', 'token = "S-new"').replace(
            'token = "S-second"', 'token = "[redacted]"')
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": edited})
        self.assertEqual(code, 200, body)
        self.assertEqual([m["token"] for m in tomllib.loads(self.on_disk())["many"]],
                         ["S-new", "S-second"])

    def test_an_unquotable_agent_command_is_400_naming_the_key(self):
        """`implementer = "echo '"` has no closing quotation: 400 with the
        key in the sentence, nothing written -- not a request that dies."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before + "\n[agents]\nimplementer = \"echo '\"\n"
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400, body)
        self.assertIn("[agents] implementer", body["error"])
        self.assertIn("quotation", body["error"])
        self.assertEqual(self.on_disk(), before)

    def test_a_relative_agent_command_path_is_400_as_at_startup(self):
        """Startup refuses `./worker` (rounds run in a worktree that does not
        exist yet); the document check holds the PUT to the same rule."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        text = before + '\n[agents]\nimplementer = "./worker --fast"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 400, body)
        self.assertIn("[agents] implementer", body["error"])
        self.assertIn("relative", body["error"])
        self.assertEqual(self.on_disk(), before)

    def implementer_script(self, body):
        """A real route the daemon really runs for the probe: the fake
        under test is not the command, so the verdict is the process's."""
        path = self.root / "implementer.sh"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
        return path

    def test_a_changed_implementer_is_probed_and_the_reply_says_it_answered(self):
        """`PUT /config` setting `[agents] implementer` runs the startup
        probe on the written document (KO-357): `probe.ok` with the exact
        command beside the write. A write that leaves the key alone carries
        `probe: null` -- nothing ran."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        path = self.implementer_script('echo "ready"\n')
        text = before + f'\n[agents]\nimplementer = "{path}"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 200, body)
        self.assertIs(body["probe"]["ok"], True)
        self.assertEqual(body["probe"]["command"],
                         [str(path), holophyte.agents.PROBE_GOAL])
        self.assertEqual(self.on_disk(), text)
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text.replace(
                                         "workers = 2", "workers = 3")})
        self.assertEqual(code, 200, body)
        self.assertIsNone(body["probe"])

    def test_a_route_that_does_not_answer_is_reported_but_the_write_lands(self):
        """The probe reports, it does not gate: the file and its backup are
        already in place, and the reply carries the exit code and the
        route's last lines so the operator can fix the key or restore."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        path = self.implementer_script("echo broken harness >&2\nexit 1\n")
        text = before + f'\n[agents]\nimplementer = "{path}"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 200, body)
        self.assertIs(body["ok"], True)
        self.assertIs(body["probe"]["ok"], False)
        self.assertEqual(body["probe"]["returncode"], 1)
        self.assertIs(body["probe"]["timed_out"], False)
        self.assertIn("broken harness", "\n".join(body["probe"]["output"]))
        self.assertEqual(self.on_disk(), text)
        self.assertEqual(Path(body["backup"]).read_text(), before)

    def test_a_route_that_cannot_start_is_reported_but_the_write_lands(self):
        """A command that does not exist passes the document check (it is
        absolute) and fails only at launch. The write has already landed,
        so the reply must still carry `probe` -- naming the launch error --
        rather than the request failing after the file was replaced."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        missing = self.root / "no-such-harness"
        text = before + f'\n[agents]\nimplementer = "{missing}"\n'
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": text})
        self.assertEqual(code, 200, body)
        self.assertIs(body["ok"], True)
        self.assertIs(body["probe"]["ok"], False)
        self.assertIs(body["probe"]["timed_out"], False)
        self.assertIsNone(body["probe"]["returncode"])
        self.assertEqual(body["probe"]["command"],
                         [str(missing), holophyte.agents.PROBE_GOAL])
        self.assertIn("No such file", body["probe"]["launch_error"])
        self.assertEqual(self.on_disk(), text)
        self.assertEqual(Path(body["backup"]).read_text(), before)

    def test_the_backup_keeps_the_file_s_mode(self):
        """A mode-0600 file's backup holds the same secrets, so it is
        created 0600 too, whatever the umask says for a new file."""
        self.seed()
        before = self.config("config_edit = true\n")
        self.start(before)
        path = self.db.parent / "config.toml"
        path.chmod(0o600)
        was = os.umask(0o022)
        self.addCleanup(os.umask, was)
        code, _, body = self.request("PUT", "/config", self.BEARER,
                                     body={"text": before})
        self.assertEqual(code, 200, body)
        mode = stat.S_IMODE(Path(body["backup"]).stat().st_mode)
        self.assertEqual(oct(mode), oct(0o600))
        self.assertEqual(oct(stat.S_IMODE(path.stat().st_mode)), oct(0o600))

    def test_two_writes_in_one_second_keep_two_backups(self):
        """`write_config()` twice with the same clock: each previous text
        is in a backup of its own and the file is the second write's."""
        self.seed()
        first = self.config("config_edit = true\n")
        (self.db.parent / "config.toml").write_text(first)
        tgt = holophyte.target.Target.locate(self.target)
        when = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
        second = first.replace("workers = 2", "workers = 3")
        third = first.replace("workers = 2", "workers = 4")
        code, one = holophyte.serve.write_config(tgt, {"text": second}, when)
        self.assertEqual(code, 200, one)
        code, two = holophyte.serve.write_config(tgt, {"text": third}, when)
        self.assertEqual(code, 200, two)
        self.assertNotEqual(one["backup"], two["backup"])
        self.assertEqual(Path(one["backup"]).read_text(), first)
        self.assertEqual(Path(two["backup"]).read_text(), second)
        self.assertEqual(self.on_disk(), third)
        self.assertEqual(
            sorted(p.name for p in self.db.parent.glob("config.toml.*")),
            ["config.toml.bak-20260910T120000Z",
             "config.toml.bak-20260910T120000Z-2"])

    def test_config_edit_without_a_token_file_is_a_startup_error(self):
        self.seed()
        (self.db.parent / "config.toml").write_text(
            "[serve]\nconfig_edit = true\n")
        tgt = holophyte.target.Target.locate(self.target)
        with self.assertRaises(SystemExit) as raised:
            holophyte.serve.serve(tgt, "127.0.0.1:0", out=io.StringIO())
        message = str(raised.exception)
        self.assertIn("[serve] token_file", message)
        self.assertIn("config_edit", message)


class ParseAddressTests(unittest.TestCase):

    def test_a_bare_port_binds_loopback(self):
        self.assertEqual(holophyte.serve.parse_address("7710"),
                         ("127.0.0.1", 7710))
        self.assertEqual(holophyte.serve.parse_address("0"), ("127.0.0.1", 0))

    def test_host_port_is_the_pair_as_typed(self):
        self.assertEqual(holophyte.serve.parse_address("100.64.0.9:7710"),
                         ("100.64.0.9", 7710))
        self.assertEqual(holophyte.serve.parse_address("::1:8787"),
                         ("::1", 8787))

    def test_anything_else_is_refused_naming_both_shapes(self):
        for text in (":7710", "host:", "abc", "-1", "7710 ", "host:7a"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as raised:
                    holophyte.serve.parse_address(text)
                message = str(raised.exception)
                self.assertIn("PORT|HOST:PORT", message)
                self.assertIn(repr(text), message)


class CliTests(ServeTestCase):

    def test_a_malformed_address_is_a_usage_error_naming_the_shape(self):
        for argv in (["--serve"], ["--serve", "localhost"],
                     ["--serve", "127.0.0.1:abc"], ["--serve", ":7710"]):
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), \
                        self.assertRaises(SystemExit) as raised:
                    holophyte.cli.cli([str(self.target), *argv])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("PORT|HOST:PORT", stderr.getvalue())
                self.assertFalse(self.db.exists())

    def test_a_bare_port_serves_status_on_loopback(self):
        self.seed()
        out = io.StringIO()
        seen = {}

        done = threading.Event()

        def poll_then_stop():
            # A refused address returns before anything is served; stop
            # polling then rather than hang the test on a usage error.
            while "serving" not in out.getvalue() and not done.is_set():
                sleep(0.01)
            if done.is_set():
                return
            host, port = out.getvalue().splitlines()[0].split()[2].split(":")
            seen["host"] = host
            conn = http.client.HTTPConnection(host, int(port), timeout=10)
            conn.request("GET", "/status")
            seen["status"] = conn.getresponse().status
            conn.close()
            os.kill(os.getpid(), signal.SIGTERM)

        stopper = threading.Thread(target=poll_then_stop)
        stopper.start()
        self.addCleanup(stopper.join)
        self.addCleanup(done.set)
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()):
                code = holophyte.cli.cli([str(self.target), "--serve", "0"])
        finally:
            done.set()
        stopper.join()

        self.assertEqual(code, 0)
        self.assertEqual(seen["host"], "127.0.0.1")
        self.assertEqual(seen["status"], 200)

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

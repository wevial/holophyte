"""`--serve`: read APIs over a loopback ephemeral HTTP daemon.
Fixtures use temporary HOLOPHYTE_HOME stores written through the public API.
Assertions read over the socket, as an operator's console would.
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
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from time import monotonic, sleep, time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# `-m unittest tests.<name>` resolves the sibling fixtures as discovery does.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import holophyte.cli  # noqa: E402 - after the sys.path insert above
import holophyte.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
import holophyte.serve  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.schema  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte.serve_config import TOMLKIT_MISSING  # noqa: E402
from tests.host_fixture import factory_checkout, git
from tests.phase_fixture import advance_phase, finish_run, park_run
from tests.serve_fixture import MERGE_SHA, MIN, SEC, ServeTestCase  # noqa: E402

# How far the clock may move between seeding and the assertion: the daemon
# stamps its own `now`, so an age is "about" the seeded distance.
SLACK = 10 * SEC


class DecimalConfigPatchTests(ServeTestCase):
    """KO-415: decimal patches retain their TOML and JSON number types."""

    def start_config(self, settings):
        self.seed()
        token = self.root / "serve.token"
        token.write_text("config-test-token\n")
        token.chmod(0o600)
        self.bearer = {"Authorization": "Bearer config-test-token"}
        self.before = (f'[serve]\ntoken_file = "{token}"\n'
                       f'config_edit = true\n{settings}')
        self.start(self.before)

    def test_decimal_and_integer_patches_preserve_a_float_key(self):
        self.start_config("[agents]\nbudget_scale = 1.0  # scale\n")
        for incoming, expected in ((1.5, "1.5"), (2, "2.0"), (2.0, "2.0")):
            with self.subTest(incoming=repr(incoming)):
                code, _, body = self.request(
                    "PUT", "/config", self.bearer,
                    body={"patch": {"agents.budget_scale": incoming}})
                self.assertEqual(code, 200, body)
                self.assertEqual((self.db.parent / "config.toml").read_text(),
                                 self.before.replace("1.0", expected))
                code, _, body = self.request("GET", "/config", self.bearer)
                self.assertEqual(code, 200, body)
                value = body["values"]["agents"]["budget_scale"]
                self.assertEqual(value, float(expected))
                self.assertIsInstance(value, float)

    def test_float_patch_cannot_change_an_integer_key_type(self):
        self.start_config("[loop]\nworkers = 2\n")
        code, _, body = self.request(
            "PUT", "/config", self.bearer,
            body={"patch": {"loop.workers": 1.5}})
        self.assertEqual(code, 400, body)
        for word in ("loop.workers", "integer", "float"):
            self.assertIn(word, body["error"])
        self.assertEqual((self.db.parent / "config.toml").read_text(), self.before)
        self.assertEqual(list(self.db.parent.glob("config.toml.bak-*")), [])


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
        project = holophyte.project.Project.locate(self.target)
        for address in ("0.0.0.0:0", "[::]:0", "10.0.0.1:0"):
            with self.subTest(address=address), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.serve.serve(project, address, out=io.StringIO())
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
        for path in ("/status", "/attention"):  # KO-634: the path once, as project
            code, _, body = self.request("GET", path, self.BEARER)
            self.assertEqual((code, body.get("project")), (200, str(self.target)))
            self.assertNotIn("target", body, path)
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
        return holophyte.project.Project.locate(self.target)

    def test_a_group_or_world_readable_token_file_is_refused(self):
        self.seed()
        for mode in (0o640, 0o604, 0o644):
            path = self.token_file(mode)
            project = self.configured(path)
            with self.subTest(mode=oct(mode)), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.serve.serve(project, "0.0.0.0:0", out=io.StringIO())
            message = str(raised.exception)
            self.assertIn(f"{mode:04o}", message)
            self.assertIn(str(path), message)
            self.assertNotIn(self.TOKEN, message)

    def test_a_missing_or_empty_token_file_is_refused_naming_it(self):
        self.seed()
        for path in (self.root / "absent.token", self.token_file(text="  \n")):
            project = self.configured(path)
            with self.subTest(path=path.name), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.serve.serve(project, "0.0.0.0:0", out=io.StringIO())
            self.assertIn(str(path), str(raised.exception))

    MACHINE_TOKEN = "machine-wide-token-value"

    def machine_token_file(self):
        path = self.root / "machine.token"
        path.write_text(self.MACHINE_TOKEN + "\n")
        path.chmod(0o600)
        return path

    def test_either_the_project_or_the_machine_token_is_accepted(self):
        self.seed()
        config = (self.token_config(self.token_file())
                  + f'machine_token_file = "{self.machine_token_file()}"\n')
        self.start(config, host="0.0.0.0")
        for token in (self.TOKEN, self.MACHINE_TOKEN):
            with self.subTest(token=token):
                code, _, body = self.request(
                    "GET", "/status", {"Authorization": f"Bearer {token}"})
                self.assertEqual(code, 200)
                self.assertEqual(body["project"], str(self.target))
        for headers in (None, {"Authorization": "Bearer wrong"},
                        {"Authorization": f"Bearer {self.MACHINE_TOKEN}x"},
                        {"Authorization": f"Basic {self.MACHINE_TOKEN}"}):
            with self.subTest(headers=headers):
                code, _, body = self.request("GET", "/status", headers)
                self.assertEqual((code, body), (401, {}))

    def test_without_the_machine_key_only_the_project_token_is_accepted(self):
        self.seed()
        self.machine_token_file()  # on disk, but no key names it
        self.start(self.token_config(self.token_file()), host="0.0.0.0")
        code, _, _ = self.request("GET", "/status", self.BEARER)
        self.assertEqual(code, 200)
        code, _, _ = self.request(
            "GET", "/status", {"Authorization": f"Bearer {self.MACHINE_TOKEN}"})
        self.assertEqual(code, 401)


class StatusTests(ServeTestCase):

    def test_status_lists_the_live_run_and_the_supervisor(self):
        self.seed()
        self.start()

        code, headers, body = self.request("GET", "/status")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body["project"], str(self.target))
        self.assertNotIn("target", body)
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
        knobs = holophyte.config_tables.sweep_config(self.project)
        self.assertEqual(body["thresholds"],
                         {"heartbeat_stale_ms": knobs.heartbeat_stale_ms,
                          "strikes": knobs.stale_strikes,
                          "run_cap": knobs.run_cap})
        self.assertIs(body["actions"], False)
        self.assertIs(body["config_edit"], False)

    def test_a_scaled_budget_serves_the_scaled_box(self):
        """`[agents] budget_scale` stretches the box a run is counted
        against; /status and /runs/N serve the scaled figure, so the
        console's time-box bar and timeline draw the box the loop armed."""
        self.seed()
        self.start(config="[agents]\nbudget_scale = 2\n")

        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200)
        (run,) = body["runs"]
        self.assertEqual(run["time_box_ms"], 50 * MIN)

        code, _, detail = self.request("GET", f"/runs/{self.run}")
        self.assertEqual(code, 200)
        self.assertEqual(detail["run"]["time_box_ms"], 50 * MIN)

    def test_a_run_carries_title_start_round_and_strikes(self):
        # KO-263: what the console's floor row draws. A run in `reviewing`
        # with two ended rounds and one strike on file.
        self.seed()
        conn = store.open(str(self.db))
        try:
            advance_phase(conn, self.run, "reviewing", now=self.now - MIN)
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
        # KO-634: the body names the project and nothing else.
        self.assertEqual(body["project"], str(self.target))
        self.assertNotIn("target", body)
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
        self.assertEqual(body["project"], str(self.target))

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
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-7",
                linear_identifier="KO-7", title="ticket 7",
                acceptance_criteria=["Given KO-7, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN,
                body=self.BODY, now=self.now - 5 * MIN)
            store.tickets.transition(conn, ticket, "in_flight")
            self.run = store.claim(conn, project, ticket, now=self.now - 2 * MIN)
        finally:
            conn.close()

    def test_a_mirrored_ticket_answers_its_nine_fields_and_body(self):
        self.seed_ticket()
        self.start()

        code, headers, body = self.request("GET", "/tickets/KO-7")

        self.assertEqual(code, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        # Mirrored once and claimed since: one revision, current and claimed.
        revision = {"revision": 1, "at": self.now - 5 * MIN, "author": "board",
                    "title": "ticket 7", "body": self.BODY, "priority": None,
                    "labels": [], "column": None}
        self.assertEqual(body, {
            "ticket": "KO-7", "ticket_url": None,
            "title": "ticket 7", "status": "in_flight",
            "body": self.BODY,
            "acceptance_criteria": ["Given KO-7, then it is worked"],
            "verification_commands": ["echo ok"],
            "time_box_ms": 25 * MIN, "run": self.run,
            "mirrored_ms": self.now - 5 * MIN,
            "current": revision, "claimed": revision,
            "revisions": [{"revision": 1, "at": self.now - 5 * MIN,
                           "author": "board"}],
            "notes": []})

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
            project = store.tickets.ensure_project(conn, "team-1", self.target)

            def ticket(ident):
                return store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{ident}",
                    linear_identifier=ident, title=f"ticket {ident}",
                    acceptance_criteria=[f"Given {ident}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=25 * MIN)

            blocked = ticket("KO-8")
            store.tickets.transition(conn, blocked, "in_flight")
            self.blocked_run = store.claim(conn, project, blocked,
                                           now=self.asked - 10 * MIN)
            store.set_phase(conn, self.blocked_run, "working",
                            now=self.asked - 10 * MIN)
            # The heartbeat is `asked_ms`'s fallback: a minute before the
            # redirect so the two are told apart.
            store.heartbeat(conn, self.blocked_run, now=self.asked - MIN)
            store.tickets.transition(conn, blocked, "blocked_on_operator")
            conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                         ("Which branch is canonical?", blocked))
            conn.commit()
            park_run(conn, self.blocked_run, "blocked_on_operator",
                       "asked the operator", now=self.asked - MIN)
            if redirect:
                store.record_intervention(
                    conn, self.blocked_run, "redirect", "asked the operator",
                    source="supervisor", trigger="off_criteria",
                    question="Which branch is canonical?", now=self.asked)

            self.failed_ticket = ticket("KO-9")
            store.tickets.transition(conn, self.failed_ticket, "in_flight")
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
            store.tickets.transition(conn, live, "in_flight")
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
                                   "ticket_url": None,
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

    def park_on_pr(self, url, pr_seen=None):
        """KO-10 parked the way `_park_on_pr()` parks: `runs.prUrl` set,
        the ticket asking `PR open: URL` with the reason under it, and
        `pr_seen` recorded as what the park's read saw; the run id."""
        conn = store.open(str(self.db))
        try:
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            parked = store.tickets.mirror_ticket(
                conn, project, linear_issue_id="issue-KO-10",
                linear_identifier="KO-10", title="ticket KO-10",
                acceptance_criteria=["Given KO-10, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN)
            store.tickets.transition(conn, parked, "in_flight")
            run = store.claim(conn, project, parked, now=self.now - 5 * MIN)
            store.set_phase(conn, run, "working", now=self.now - 5 * MIN)
            store.heartbeat(conn, run, now=self.now - 2 * MIN)
            store.tickets.transition(conn, parked, "blocked_on_operator")
            conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                         (f"PR open: {url}\nreview requested from a coworker"
                          "\n1. src/x.py:3 by @coworker", parked))
            conn.commit()
            park_run(conn, run, "awaiting_merge_approval", "PR open",
                       candidate_sha="a" * 40, pr_url=url, park_kind="pull_request",
                       now=self.now - 2 * MIN, pr_seen=pr_seen)
        finally:
            conn.close()
        return run

    def test_a_park_on_a_pull_request_is_pr_open_and_a_question_stays_blocked(self):
        """KO-8 is parked with a plain question and no PR; KO-10 is parked
        the way `_park_on_pr()` parks, by a read that never came back.
        Only KO-10 is `pr_open`, its `reason` the question without that
        first line, its `pr` the number from the URL with the three facts
        null: never polled (KO-368)."""
        self.seed_attention()
        url = "https://github.com/example/repo/pull/2170"
        run = self.park_on_pr(url)
        self.start()

        _, _, body = self.request("GET", "/attention")

        by_ticket = {item["ticket"]: item for item in body["items"]
                     if item["kind"] in ("blocked", "pr_open")}
        self.assertEqual(by_ticket["KO-8"]["kind"], "blocked")
        self.assertEqual(by_ticket["KO-8"]["question"],
                         "Which branch is canonical?")
        self.assertEqual(by_ticket["KO-10"], {
            "kind": "pr_open", "ticket": "KO-10", "ticket_url": None,
            "title": "ticket KO-10", "run": run, "pr_url": url,
            "reason": "review requested from a coworker"
                      "\n1. src/x.py:3 by @coworker",
            "asked_ms": self.now - 2 * MIN,
            "pr": {"number": 2170, "checks": None, "review": None,
                   "threads": None, "title": None},
            "level": "attention"})
        self.assertEqual(body["level"], "attention")

    def test_a_pr_open_item_carries_what_the_reconcile_saw_on_the_pull_request(
            self):
        """KO-368: the run's park recorded the checks rollup, the review
        decision and the thread count its read saw; the item's `pr`
        carries them beside the number."""
        self.seed_attention()
        url = "https://github.com/example/repo/pull/2170"
        self.park_on_pr(url, pr_seen=("2026-09-10T10:00:00Z", 3, "failure",
                                      "changes_requested", None))
        self.start()

        _, _, body = self.request("GET", "/attention")

        item = next(item for item in body["items"]
                    if item["kind"] == "pr_open")
        self.assertEqual(item["pr"], {"number": 2170, "checks": "failure",
                                      "review": "changes_requested",
                                      "threads": 3, "title": None})

    def test_the_body_names_the_project_as_status_does(self):
        self.seed_attention()
        self.start()

        _, _, body = self.request("GET", "/attention")

        self.assertEqual(body["project"], str(self.target))
        self.assertNotIn("target", body)

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

        # Only the latest attempt needs attention, with its original number.
        failed = [(item["run"], item["attempt"])
                  for item in body["items"] if item["kind"] == "failed"]
        self.assertEqual(failed, [(self.failed, 2)])

    def test_a_requeued_failure_stays_until_a_new_attempt(self):
        self.seed_attention()
        conn = store.open(str(self.db))
        try:
            store.requeue(conn, self.failed_ticket, "operator requeued")
        finally:
            conn.close()
        self.start()

        _, _, body = self.request("GET", "/attention")

        kinds = [item["kind"] for item in body["items"]]
        self.assertEqual(kinds, ["blocked", "stale_run", "failed", "supervisor"])

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
                                "project": str(self.target)})

        conn = store.open(str(self.db))
        try:
            finish_run(conn, self.run, "merged")
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
            project = store.tickets.ensure_project(conn, "team-1", self.target)

            def ticket(n, specced=True, depends_on=None):
                return store.tickets.mirror_ticket(
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
            store.tickets.transition(conn, blocked_on_deps, "blocked_on_deps")
            parked = ticket(4)
            store.tickets.transition(conn, parked, "in_flight")
            store.tickets.transition(conn, parked, "blocked_on_operator")
            conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                         ("Which branch is canonical?", parked))
            conn.commit()
            merged = ticket(6)
            store.tickets.transition(conn, merged, "in_flight")
            run = store.claim(conn, project, merged, now=self.now - 20 * MIN)
            finish_run(conn, run, "merged", now=self.now - 10 * MIN,
                          merge_sha=MERGE_SHA)
            store.tickets.transition(conn, merged, "merged")
            live = ticket(5)
            store.tickets.transition(conn, live, "in_flight")
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
            {"ticket": "KO-3", "ticket_url": None,
             "title": "ticket 3", "time_box_ms": 25 * MIN,
             "run": None, "question": None,
             "waits_on": ["KO-2", "issue-never-seen"],
             "mirrored_ms": self.now - 5 * MIN}])
        self.assertEqual(by_state["in_flight"], [
            {"ticket": "KO-5", "ticket_url": None,
             "title": "ticket 5", "time_box_ms": 25 * MIN,
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
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            self.runs = {}
            for ident, url in (("KO-8", self.PR_URL), ("KO-9", None)):
                ticket = store.tickets.mirror_ticket(
                    conn, project, linear_issue_id=f"issue-{ident}",
                    linear_identifier=ident, title=f"ticket {ident}",
                    acceptance_criteria=[f"Given {ident}, then it is worked"],
                    verification_commands=["echo ok"], time_box_ms=25 * MIN)
                store.tickets.transition(conn, ticket, "in_flight")
                run = store.claim(conn, project, ticket, now=self.now - 20 * MIN)
                store.set_phase(conn, run, "working", now=self.now - 20 * MIN)
                store.tickets.transition(conn, ticket, "blocked_on_operator")
                park_run(conn, run, "blocked_on_operator", "parked on the PR",
                           candidate_sha=MERGE_SHA, pr_url=url,
                           now=self.now - 10 * MIN)
                self.runs[ident] = run
            store.record_supervisor_heartbeat(
                conn, 4242, self.now - MIN, now=self.now - 5 * SEC)
        finally:
            conn.close()

    def merge_parked(self):
        """Both parked runs end `merged`, as the babysitter ends one whose PR
        landed."""
        conn = store.open(str(self.db))
        try:
            for run in self.runs.values():
                finish_run(conn, run, "merged", now=self.now - MIN,
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


class ActionFailureTests(ServeTestCase):
    """KO-649: an action handler that raises is a 500 JSON answer the
    console can read, not a dropped connection it calls "Failed to fetch"."""

    def send_back_raising(self, failure):
        """`(status, headers, body, stderr)` of a send-back raising `failure`."""
        self.seed()
        token = self.root / "serve.token"
        token.write_text(TokenTests.TOKEN + "\n")
        token.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{token}"\nactions = true\n')
        out = io.StringIO()
        with contextlib.redirect_stderr(out), patch.object(
                holophyte.serve, "send_back_action", side_effect=failure):
            return (*self.request("POST", "/actions/send-back", TokenTests.BEARER,
                                  {"run": self.run}), out.getvalue())

    def test_a_raising_handler_answers_500_json_and_the_daemon_serves_on(self):
        # The 2026-09-22 incident: a `SystemExit` subclass, not an Exception.
        code, headers, body, logged = self.send_back_raising(
            store.schema.SchemaNewer("store.db", 99))
        self.assertEqual((code, headers["Access-Control-Allow-Origin"]), (500, "*"))
        self.assertIn("SchemaNewer: store.db: store schema version 99", body["error"])
        self.assertEqual(logged.count("Traceback"), 1, logged)
        self.assertEqual(self.request("GET", "/status")[0], 200)

    def test_a_registered_secret_in_the_message_is_redacted(self):
        self.enterContext(patch("holophyte.redact._environment_values", frozenset()))
        holophyte.redact.register_values(["ko649-registered-secret"])
        _, _, body, logged = self.send_back_raising(
            RuntimeError("could not open with ko649-registered-secret"))
        self.assertEqual(body["error"], "RuntimeError: could not open with [redacted]")
        self.assertNotIn("ko649-registered-secret", self.raw_body + logged)


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

    def serve_until_sigterm(self, address):
        self.seed()
        out = io.StringIO()
        seen = {}
        done = threading.Event()

        def poll_then_stop():
            # Stop polling if startup is refused before the announcement.
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
                code = holophyte.cli.cli([str(self.target), "--serve", address])
        except SystemExit as refused:
            raise self.failureException(str(refused)) from None
        finally:
            done.set()
        stopper.join()
        self.assertEqual(code, 0)
        return out.getvalue(), seen

    def test_a_bare_port_serves_status_on_loopback(self):
        _, seen = self.serve_until_sigterm("0")
        self.assertEqual(seen["host"], "127.0.0.1")
        self.assertEqual(seen["status"], 200)

    def test_serve_announces_the_bound_address_and_stops_on_sigterm(self):
        out, seen = self.serve_until_sigterm("127.0.0.1:0")
        self.assertEqual(seen["status"], 200)
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("[holo2] serving 127.0.0.1:"), first)
        self.assertIn(f"read-only for {self.target}", first)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


class RefusedStartTests(unittest.TestCase):
    def test_cli_tests_report_refusal_and_leave_no_helper_thread(self):
        for method in (
                "test_a_bare_port_serves_status_on_loopback",
                "test_serve_announces_the_bound_address_and_stops_on_sigterm"):
            with self.subTest(method=method):
                before = set(threading.enumerate())
                result = unittest.TestResult()
                started = monotonic()
                with patch.object(holophyte.serve, "require_tomlkit",
                                  side_effect=SystemExit(TOMLKIT_MISSING)), \
                        patch("os.kill") as kill:
                    CliTests(method).run(result)
                self.assertLess(monotonic() - started, 5)
                self.assertEqual(set(threading.enumerate()) - before, set())
                kill.assert_not_called()
                self.assertEqual(result.testsRun, 1)
                problems = result.failures + result.errors
                self.assertEqual(len(problems), 1)
                self.assertEqual(problems[0][1].count(TOMLKIT_MISSING), 1)


# A server for a handler run on a socket pair: every request is counted in.
COUNTER = types.SimpleNamespace(begin=lambda: True, done=lambda: None)


class DisconnectedClientTests(unittest.TestCase):
    def test_closed_client_logs_one_line_without_traceback(self):
        server_side, client_side = socket.socketpair()
        self.addCleanup(server_side.close)
        client_side.sendall(b"GET /missing\x1b[31m HTTP/1.0\r\n\r\n")
        client_side.close()
        out = io.StringIO()
        with contextlib.redirect_stderr(out), \
                patch.object(holophyte.serve.StatusHandler, "do_GET",
                             lambda handler: handler.answer(404, {})):
            holophyte.serve.StatusHandler(server_side, ("local", 0), COUNTER)
        self.assertEqual(out.getvalue(),
                         "[holo2] client disconnected: '/missing\\x1b[31m'\n")
        self.assertNotIn("Traceback", out.getvalue())

    def test_reset_during_body_write_logs_one_line(self):
        server_side, client_side = socket.socketpair()
        self.addCleanup(server_side.close)
        self.addCleanup(client_side.close)
        client_side.sendall(b"GET /status HTTP/1.0\r\n\r\n")
        out = io.StringIO()
        with contextlib.redirect_stderr(out), \
                patch.object(holophyte.serve.StatusHandler, "do_GET",
                             lambda handler: handler.answer(200, {})), \
                patch("socketserver._SocketWriter.write",
                      side_effect=[None, ConnectionResetError()]):
            holophyte.serve.StatusHandler(server_side, ("local", 0), COUNTER)
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertIn("/status", out.getvalue())
        self.assertNotIn("Traceback", out.getvalue())


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FollowsCodeProcessTests(ServeTestCase):
    """KO-648 end to end: a real daemon run from its own git checkout of the
    factory re-executes when that checkout's `HEAD` moves, and the fresh
    process answers on the address the old one held."""

    CHECK = 0.2  # the copy's check interval, seconds
    TOLERANCE = 1.0  # scheduling slack on a loaded host, seconds

    def factory_checkout(self):
        """A copy of this factory committed as commit A in its own git
        repository, with the check interval cut so the test is quick."""
        return factory_checkout(self, self.root / "factory", self.CHECK)

    def get_status(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", "/status")
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_a_moved_checkout_re_executes_and_answers_on_the_same_address(self):
        self.seed()
        checkout = self.factory_checkout()
        first = git(checkout, "rev-parse", "HEAD")
        port = free_port()
        daemon = subprocess.Popen(
            [sys.executable, "-u", str(checkout / "factory.py"),
             str(self.target), "--serve", f"127.0.0.1:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines, stamps = [], []

        def read():
            for line in daemon.stdout:
                stamps.append(monotonic())
                lines.append(line)
        reader = threading.Thread(target=read)
        reader.start()
        self.addCleanup(daemon.stdout.close)
        self.addCleanup(reader.join)
        self.addCleanup(daemon.wait, 10)
        self.addCleanup(daemon.kill)
        announced = f"[holo2] serving 127.0.0.1:{port} "

        def wait_for(count):
            deadline = monotonic() + 20
            while monotonic() < deadline and daemon.poll() is None:
                if sum(line.startswith(announced) for line in lines) >= count:
                    return
                sleep(0.05)
            self.fail(f"no serving line #{count}: {''.join(lines)}")

        wait_for(1)
        # A client that connects and never sends a request: accepted ahead
        # of the `/status` below, it must not hold the re-exec back.
        idle = socket.create_connection(("127.0.0.1", port), timeout=10)
        self.addCleanup(idle.close)
        code, before = self.get_status(port)
        self.assertEqual(code, 200, before)

        git(checkout, "commit", "-q", "--allow-empty", "-m", "B")
        committed = monotonic()
        second = git(checkout, "rev-parse", "HEAD")
        wait_for(2)

        moved = [n for n, line in enumerate(lines)
                 if "factory code moved" in line]
        self.assertEqual(len(moved), 1, "".join(lines))
        self.assertIn(f"factory code moved from {first} to {second};"
                      " serve re-executing", lines[moved[0]])
        self.assertLess(stamps[moved[0]] - committed,
                        self.CHECK + self.TOLERANCE)
        self.assertEqual(idle.recv(1), b"")  # dropped by the exec
        code, after = self.get_status(port)
        self.assertEqual(code, 200, after)
        # The same pid (an exec, not a child) answering as a fresh daemon.
        self.assertIsNone(daemon.poll())
        self.assertGreater(after["daemon"]["started_ms"],
                           before["daemon"]["started_ms"])


class FollowsCodeTests(ServeTestCase):
    """KO-648 in process: `serve()` on this thread with the revision read
    and the `EXEC` seam patched, a client on a helper thread."""

    INTERVAL = 0.05

    def serve_with(self, revision, client):
        """Run `serve()` until it re-executes or `client(port)` returns and
        a SIGTERM stops it; `(printed, events)` where `events` holds "EXEC"
        once the seam was called."""
        self.seed()
        project = holophyte.project.Project.locate(self.target)
        out = io.StringIO()
        self.events = []
        returned = threading.Event()

        def drive():
            deadline = monotonic() + 10
            while "serving" not in out.getvalue() and monotonic() < deadline:
                sleep(0.01)
            port = int(out.getvalue().split()[2].rsplit(":", 1)[1])
            try:
                client(port)
            finally:
                if not returned.wait(5):
                    os.kill(os.getpid(), signal.SIGTERM)
        helper = threading.Thread(target=drive)
        helper.start()
        with patch.object(holophyte.serve, "factory_revision", revision), \
                patch.object(holophyte.serve, "EXEC",
                             lambda *_: self.events.append("EXEC")), \
                patch.object(sys, "orig_argv", ["python3", "factory.py"]):
            try:
                code = holophyte.serve.serve(project, "127.0.0.1:0", out=out,
                                             interval=self.INTERVAL)
            finally:
                returned.set()
                helper.join()
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_a_request_in_flight_is_answered_before_the_re_exec(self):
        moved, checked = threading.Event(), threading.Event()
        answers = []

        def revision():
            if moved.is_set():
                checked.set()
                return "bbb"
            return "aaa"

        def slow_status(target, started_ms=None):
            moved.set()
            self.assertTrue(checked.wait(10))  # the check ran mid-request
            sleep(0.3)
            self.events.append("answered")
            return 200, {"slow": True}

        def client(port):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/status")
            response = conn.getresponse()
            answers.append((response.status, json.loads(response.read())))
            conn.close()

        with patch.object(holophyte.serve, "status", slow_status):
            printed = self.serve_with(revision, client)

        self.assertEqual(answers, [(200, {"slow": True})])
        self.assertEqual(self.events, ["answered", "EXEC"])
        self.assertIn("[holo2] factory code moved from aaa to bbb;"
                      " serve re-executing", printed)

    def test_an_unreadable_head_keeps_serving_and_logs_once(self):
        reads = []
        answers = []

        def revision():
            reads.append(None)
            return "aaa" if len(reads) == 1 else None

        def client(port):
            deadline = monotonic() + 10
            while len(reads) < 4 and monotonic() < deadline:
                sleep(0.01)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", "/status")
            answers.append(conn.getresponse().status)
            conn.close()

        printed = self.serve_with(revision, client)

        self.assertGreaterEqual(len(reads), 4)
        self.assertEqual(answers, [200])
        self.assertEqual(self.events, [])
        self.assertEqual(printed.count("cannot read the factory checkout's"
                                       " HEAD"), 1, printed)
        self.assertIn("serve stopping on signal", printed)



if __name__ == "__main__":
    unittest.main()

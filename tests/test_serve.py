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
import subprocess
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

    def start(self, config=None, console_dir=None):
        """Bind the daemon for the target on a loopback ephemeral port,
        serving `/` from `console_dir` -- an absent directory under the
        test root by default, never the repository's own build."""
        if config is not None:
            (self.db.parent / "config.toml").write_text(config)
        self.tgt = holophyte.target.Target.locate(self.target)
        console_dir = console_dir or self.root / "console" / "dist"
        server = holophyte.serve.make_server(self.tgt, "127.0.0.1", 0,
                                             console_dir=console_dir)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.host, self.port = server.server_address[:2]

    def request(self, method, path):
        """`(status, headers, decoded JSON body)` for one request."""
        status, headers, raw = self.fetch(method, path)
        self.raw_body = raw.decode()
        return status, headers, json.loads(raw)

    def fetch(self, method, path):
        """`(status, headers, raw bytes)` for one request."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        return response.status, dict(response.getheaders()), raw

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


class ConsoleTests(ServeTestCase):
    """`/` and the paths no JSON route claims: the console's built files."""

    INDEX = b"<!doctype html><title>holophyte</title>"
    APP = b"console.log('holophyte');\n"

    def build(self):
        """A `dist/` with an `index.html` and `app.js` under the test root."""
        dist = self.root / "console" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_bytes(self.INDEX)
        (dist / "app.js").write_bytes(self.APP)
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
        for path, payload, mime in (("/", self.INDEX, "text/html"),
                                    ("/index.html", self.INDEX, "text/html"),
                                    ("/app.js", self.APP, "javascript")):
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
                                   "asked_ms": self.asked,
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

    def seed_reviewed(self):
        """One merged run: two ended rounds, `changes_requested` with two
        findings then `pass`; three narrative events and one detail event."""
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
        self.assertEqual(run["max_rounds"], holophyte.serve.MAX_ROUNDS)
        self.assertIsInstance(run["max_rounds"], int)
        self.assertIn("branch", run)

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

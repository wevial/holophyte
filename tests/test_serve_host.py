"""The host daemon: `factory.py --serve` with no project (consolidation
stage 1).

Two real projects registered through `project add` under a temporary home,
each with a real store seeded through the public API -- run ids and ticket
identifiers collide across them on purpose -- and one `HostServer` on a
loopback ephemeral port, read back over `http.client`. The socket handoff
test runs a real `factory.py --serve` from a committed copy of this factory
on a listening socket the test holds, the way the service manager does.

Run: python3 -m unittest discover -s tests -p 'test_serve_host*' -v
"""
import http.client
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import types
import unittest
from pathlib import Path
from time import monotonic
from unittest.mock import Mock, patch

import holophyte.serve_host
import holophyte.serve_watch
import store
import store.read
import store.schema
import store.tickets
from holophyte.host import Host, settings
from holophyte.project import Project
from holophyte.serve_host import (
    HostServer,
    host_attention,
    host_status,
    host_tokens,
    serve_host,
)
from holophyte.serve_watch import adopted_socket
from store.schema import SCHEMA_VERSION
from tests.host_fixture import HostFixture, factory_checkout, git

NOW = 1_750_000_000_000
SEC = 1000
MIN = 60 * SEC
FIXTURES = Path(__file__).parent / "fixtures" / "serve"
MACHINE = "machine-token-value"
ALPHA_TOKEN = "alpha-project-token"
# The drawer's and the tray's request limit, the tightest client's.
DRAWER_LIMIT_SEC = 2


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


class HostServeCase(HostFixture):
    """Two registered projects, `alpha` and `beta`, each with a run on
    ticket KO-7 (run 1 in both stores) and a supervisor beat."""

    def setUp(self):
        super().setUp()
        self.paths = {name: self.repo(name) for name in ("alpha", "beta")}
        # alpha's heartbeat is fresh, beta's is half an hour old.
        for name, ago in (("alpha", 30 * SEC), ("beta", 30 * MIN)):
            self.cli("project", "add", str(self.paths[name]))
            self.seed(name, ago)

    def seed(self, name, heartbeat_ago):
        path = self.paths[name]
        conn = store.open(str(Project.locate(path).store_path))
        try:
            project = store.tickets.ensure_project(conn, f"team-{name}", path)
            ticket = store.tickets.mirror_ticket(
                conn, project, linear_issue_id=f"issue-{name}",
                linear_identifier="KO-7", title=f"{name} ticket",
                acceptance_criteria=["Given the ticket, then it is worked"],
                verification_commands=["echo ok"], time_box_ms=25 * MIN)
            store.tickets.transition(conn, ticket, "in_flight")
            run = store.claim(conn, project, ticket, now=NOW - 2 * MIN)
            store.set_phase(conn, run, "working", now=NOW - 2 * MIN)
            store.heartbeat(conn, run, now=NOW - heartbeat_ago)
            store.record_supervisor_heartbeat(conn, 4242, NOW - MIN,
                                              now=NOW - 5 * SEC)
        finally:
            conn.close()

    def config(self, name, text):
        """Append `text` to project `name`'s config."""
        path = Project.locate(self.paths[name]).config_path
        path.write_text(path.read_text() + text)

    def token_file(self, path, value):
        path.write_text(value + "\n")
        path.chmod(0o600)
        return path

    def host_config(self, **serve):
        """Put `[serve]` keys ahead of the registry's `[[project]]` list."""
        registry = self.home / "host.toml"
        lines = ["[serve]", *(f"{key} = {json.dumps(value)}"
                             for key, value in serve.items())]
        registry.write_text("\n".join(lines) + "\n" + registry.read_text())

    def machine(self):
        return str(self.token_file(self.home / "machine.token", MACHINE))

    def start(self, bind="127.0.0.1"):
        host = Host.locate()
        knobs = settings(host)
        read, write = host_tokens(host, knobs, bind, host.projects())
        self.server = HostServer(host, knobs, (bind, 0),
                                 console_dir=self.root / "no-console",
                                 read_token=read, write_token=write)
        thread = threading.Thread(target=self.server.serve_forever,
                                  daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, headers=None, body=None):
        """`(status, decoded JSON body)` for one request."""
        payload = None if body is None else json.dumps(body).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=payload, headers=headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()


class HostRoutesTests(HostServeCase):
    def test_each_prefix_answers_its_own_project_and_the_root_lists_both(self):
        self.start()
        for name in ("alpha", "beta"):
            with self.subTest(project=name):
                code, body = self.request("GET", f"/projects/{name}/status")
                self.assertEqual(code, 200, body)
                self.assertEqual(body["project"], str(self.paths[name]))
                self.assertEqual([run["ticket"] for run in body["runs"]],
                                 ["KO-7"])
                # Run 1 exists in both stores; each prefix reads its own.
                code, body = self.request("GET", f"/projects/{name}/runs/1")
                self.assertEqual((code, body["run"]["title"]),
                                 (200, f"{name} ticket"))
        code, body = self.request("GET", "/status")
        self.assertEqual(code, 200, body)
        self.assertEqual([(p["name"], p["error"], p["schema_version"],
                           p["project_row"]) for p in body["projects"]],
                         [("alpha", None, SCHEMA_VERSION, 1),
                          ("beta", None, SCHEMA_VERSION, 1)])
        code, body = self.request("GET", "/attention")
        self.assertEqual(code, 200, body)
        self.assertTrue(all("project" in item for item in body["items"]))
        self.assertEqual(body["items"][0]["kind"], "sweep_stale")
        self.assertEqual({item["project"] for item in body["items"]
                          if item["kind"] == "stale_run"}, {"alpha", "beta"})

    def test_a_name_outside_the_registry_is_404_before_any_store_opens(self):
        self.start()
        with patch.object(store.read, "open_readonly") as opened:
            for path in ("/projects/gamma/status", "/projects/%2e%2e/status",
                         "/projects/../projects/alpha/status",
                         "/projects//status", "/runs", "/projects/alpha"):
                with self.subTest(path=path):
                    code, _ = self.request("GET", path)
                    self.assertEqual(code, 404)
            opened.assert_not_called()
        # A removed project stops answering at the next request, no restart.
        self.assertEqual(self.request("GET", "/projects/beta/status")[0], 200)
        self.cli("project", "remove", "beta")
        self.assertEqual(self.request("GET", "/projects/beta/status")[0], 404)
        _, body = self.request("GET", "/status")
        self.assertEqual([p["name"] for p in body["projects"]], ["alpha"])

    def test_a_name_holding_a_space_routes_percent_encoded(self):
        # A `[serve] name` may hold a space; a client must encode it on
        # the request line, and the registry is asked the decoded name.
        self.config("beta", '[serve]\nname = "my project"\n')
        self.start()
        code, body = self.request("GET", "/projects/my%20project/status")
        self.assertEqual((code, body.get("project")),
                         (200, str(self.paths["beta"])), body)
        _, root = self.request("GET", "/status")
        self.assertEqual([p["name"] for p in root["projects"]],
                         ["alpha", "my project"])
        # A decoded slash names no entry.
        self.assertEqual(self.request("GET", "/projects/my%2Fproject/status")[0],
                         404)


class HostTokenTests(HostServeCase):
    def test_the_machine_token_answers_everywhere_a_project_token_its_prefix(self):
        self.config("alpha", '[serve]\ntoken_file = "{}"\n'.format(
            self.token_file(self.root / "alpha.token", ALPHA_TOKEN)))
        self.host_config(machine_token_file=self.machine())
        self.start(bind="0.0.0.0")
        expected = {
            None: {"/status": 401, "/attention": 401,
                   "/projects/alpha/status": 401,
                   "/projects/gamma/status": 401, "/peers": 200},
            MACHINE: {"/status": 200, "/attention": 200,
                      "/projects/alpha/status": 200,
                      "/projects/beta/status": 200,
                      "/projects/gamma/status": 404},
            ALPHA_TOKEN: {"/projects/alpha/status": 200,
                          "/projects/alpha/runs/1": 200,
                          "/projects/beta/status": 401,
                          "/status": 401, "/attention": 401},
        }
        for token, paths in expected.items():
            for path, status in paths.items():
                with self.subTest(token=token, path=path):
                    headers = None if token is None else bearer(token)
                    self.assertEqual(self.request("GET", path, headers)[0],
                                     status)

    def test_writes_demand_the_machine_token_on_loopback(self):
        self.config("beta", "[serve]\nconfig_edit = true\n")
        self.host_config(machine_token_file=self.machine(), actions=True)
        self.start()
        hold = {"note": "stage 1 test"}
        self.assertEqual(self.request("GET", "/projects/alpha/status")[0], 200)
        self.assertEqual(self.request(
            "POST", "/projects/alpha/actions/hold", body=hold)[0], 401)
        code, body = self.request("POST", "/projects/alpha/actions/hold",
                                  bearer(MACHINE), hold)
        self.assertEqual((code, body["ok"]), (200, True), body)
        _, root = self.request("GET", "/status")
        self.assertEqual([p["admission"] for p in root["projects"]],
                         ["held", "enabled"])
        # The supervisor unit is retired on a host daemon.
        self.assertEqual(self.request(
            "POST", "/projects/alpha/actions/restart-supervisor",
            bearer(MACHINE))[0], 404)
        text = Project.locate(self.paths["beta"]).config_path.read_text()
        for headers, status in ((None, 401), (bearer(MACHINE), 200)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(
                    "GET", "/projects/beta/config", headers)[0], status)
                code, body = self.request("PUT", "/projects/beta/config",
                                          headers, {"text": text})
                self.assertEqual(code, status, body)
        # alpha has not opted in: 404 whatever the token.
        self.assertEqual(self.request("PUT", "/projects/alpha/config",
                                      bearer(MACHINE), {"text": ""})[0], 404)
        self.assertEqual(self.request("PUT", "/config", bearer(MACHINE),
                                      {"text": ""})[0], 405)

    def test_what_needs_the_machine_token_is_refused_at_start_without_it(self):
        cases = (("0.0.0.0:0", {}, "", "beyond loopback"),
                 ("127.0.0.1:0", {"actions": True}, "", "[serve] actions"),
                 ("127.0.0.1:0", {}, "[serve]\nconfig_edit = true\n",
                  "config_edit in beta"))
        registry = self.home / "host.toml"
        original = registry.read_text()
        config = Project.locate(self.paths["beta"]).config_path
        before = config.read_text()
        for address, serve, beta, needs in cases:
            with self.subTest(needs=needs):
                registry.write_text(original)
                config.write_text(before + beta)
                if serve:
                    self.host_config(**serve)
                with self.assertRaises(SystemExit) as raised:
                    serve_host(Host.locate(), address)
                self.assertIn(needs, str(raised.exception))
                self.assertIn("[serve] machine_token_file", str(raised.exception))

    def test_a_host_without_a_bind_or_a_socket_is_refused_naming_the_key(self):
        with self.assertRaisesRegex(SystemExit, r"\[serve\] bind"):
            self.cli("--serve")


class HostFaultTests(HostServeCase):
    def hold_lock(self, name):
        """Lock project `name`'s store against readers until cleanup: an
        exclusive-mode connection inside a write transaction."""
        holder = sqlite3.connect(Project.locate(self.paths[name]).store_path,
                                 isolation_level=None)
        holder.execute("PRAGMA locking_mode = EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        self.addCleanup(holder.close)
        self.addCleanup(holder.execute, "ROLLBACK")

    def timed(self, path):
        """`(status, body)` for `GET path`, failing when the answer takes
        longer than the drawer's and the tray's request limit."""
        began = monotonic()
        answer = self.request("GET", path)
        self.assertLess(monotonic() - began, DRAWER_LIMIT_SEC, path)
        return answer

    def test_locked_stores_are_their_projects_503_within_a_clients_limit(self):
        # Two stores really locked, the store's own lock wait unpatched:
        # each is its project's error, the healthy one is whole, and no
        # answer outlives the tightest client's limit.
        self.paths["gamma"] = self.repo("gamma")
        self.cli("project", "add", str(self.paths["gamma"]))
        self.seed("gamma", 30 * SEC)
        self.start()
        self.server.code_check = Mock(started_from="aaa")
        self.hold_lock("alpha")
        self.hold_lock("gamma")
        code, body = self.timed("/projects/alpha/status")
        self.assertEqual(code, 503, body)
        self.assertIn("locked", body["error"])
        self.assertEqual(self.timed("/projects/beta/status")[0], 200)
        code, root = self.timed("/status")
        self.assertEqual(code, 200)
        rows = {row["name"]: row for row in root["projects"]}
        for name in ("alpha", "gamma"):
            self.assertIn("locked", rows[name]["error"])
        self.assertEqual((rows["beta"]["error"], len(rows["beta"]["runs"])),
                         (None, 1))
        code, body = self.timed("/attention")
        self.assertEqual(code, 200)
        self.assertEqual({item["project"] for item in body["items"]
                          if item["kind"] == "project_error"},
                         {"alpha", "gamma"})
        self.server.code_check.check_now.assert_not_called()

    def test_a_store_stamped_newer_is_its_projects_503_and_the_root_stays_whole(self):
        self.start()
        self.server.code_check = Mock(started_from="aaa")
        beta = Project.locate(self.paths["beta"]).store_path
        stamp = sqlite3.connect(beta)
        stamp.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        stamp.close()
        code, body = self.request("GET", "/projects/beta/attention")
        self.assertEqual(code, 503, body)
        self.assertIn("schema newer than build", body["error"])
        # A newer stamp is the hint the checkout moved: the watch looks now.
        self.server.code_check.check_now.assert_called()
        code, root = self.request("GET", "/status")
        self.assertEqual(code, 200)
        self.assertIsNone(root["projects"][0]["error"])
        self.assertIn("schema newer than build", root["projects"][1]["error"])
        code, body = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        self.assertIn("project_error", [item["kind"] for item in body["items"]
                                        if item["project"] == "beta"])

    def test_an_action_the_store_refuses_to_lock_is_its_projects_503(self):
        # A writer holds alpha's WAL write lock: reads, the pre-check
        # included, still pass; the action's own write cannot.
        self.host_config(machine_token_file=self.machine(), actions=True)
        self.start()
        self.enterContext(patch.object(store.schema, "BUSY_TIMEOUT_S", 0.2))
        writer = sqlite3.connect(Project.locate(self.paths["alpha"]).store_path,
                                 isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN IMMEDIATE")
        self.addCleanup(writer.execute, "ROLLBACK")
        self.assertEqual(self.request("GET", "/projects/alpha/status")[0], 200)
        code, body = self.request("POST", "/projects/alpha/actions/hold",
                                  bearer(MACHINE), {"note": "while locked"})
        self.assertEqual(code, 503, body)
        self.assertIn("locked", body["error"])
        self.assertEqual(body["project"], "alpha")
        code, body = self.request("POST", "/projects/beta/actions/hold",
                                  bearer(MACHINE), {"note": "beta is free"})
        self.assertEqual((code, body["ok"]), (200, True), body)


class RowlessProjectTests(HostServeCase):
    def test_a_store_without_the_projects_row_is_503_until_a_hold_writes_it(self):
        # The store recreated after registration: schema, no rows.
        path = Project.locate(self.paths["alpha"]).store_path
        for stale in path.parent.glob(path.name + "*"):
            stale.unlink()
        store.open(str(path)).close()
        self.host_config(machine_token_file=self.machine(), actions=True)
        self.start()
        code, body = self.request("GET", "/projects/alpha/status")
        self.assertEqual((code, body["error"]), (503, "no project row"), body)
        self.assertIn("project add", body["detail"])
        self.assertEqual(self.request("GET", "/projects/beta/status")[0], 200)
        _, root = self.request("GET", "/status")
        self.assertEqual([(p["name"], p["error"], p["project_row"])
                          for p in root["projects"]],
                         [("alpha", None, None), ("beta", None, 1)])
        # A hold may create the row, as `--hold` does.
        code, body = self.request("POST", "/projects/alpha/actions/hold",
                                  bearer(MACHINE), {"note": "rowless"})
        self.assertEqual((code, body["ok"]), (200, True), body)
        code, body = self.request("GET", "/projects/alpha/status")
        self.assertEqual((code, body["admission"]), (200, "held"), body)


class RunSweepTests(HostServeCase):
    def test_the_ledger_row_is_written_before_systemctl_is_asked(self):
        self.host_config(machine_token_file=self.machine(), actions=True)
        self.start()
        ledger = self.home / "host-actions.jsonl"
        seen = []

        def systemctl(verb, unit, *options):
            seen.append((verb, unit, options, ledger.read_text()))
            return True, "started"
        with patch.object(holophyte.serve_host, "systemctl_user", systemctl):
            self.assertEqual(self.request(
                "POST", "/actions/run-sweep", body={})[0], 401)
            code, body = self.request("POST", "/actions/run-sweep",
                                      bearer(MACHINE), {"note": "look now"})
        self.assertEqual((code, body["ok"], body["unit"]),
                         (200, True, "holophyte-sweep.service"), body)
        ((verb, unit, options, written),) = seen
        self.assertEqual((verb, unit, options),
                         ("start", "holophyte-sweep.service", ("--no-block",)))
        (row,) = [json.loads(line) for line in written.splitlines()]
        self.assertEqual((row["action"], row["note"]), ("run_sweep", "look now"))
        # A row that cannot be written runs nothing.
        ledger.unlink()
        ledger.mkdir()
        with patch.object(holophyte.serve_host, "systemctl_user") as called:
            code, body = self.request("POST", "/actions/run-sweep",
                                      bearer(MACHINE), {})
        self.assertEqual((code, body["ok"]), (200, False), body)
        called.assert_not_called()


def normalize(value, root, key=""):
    """Paths under the temporary root, pids and revisions made stable."""
    if isinstance(value, dict):
        return {k: normalize(v, root, k) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, root) for v in value]
    if isinstance(value, str):
        if key in ("daemon", "head"):
            return "REVISION"
        if key == "host":
            return "writer"
        # A state directory's name carries a hash of the temporary path.
        return re.sub(r"-[0-9a-f]{8}/", "-HASH/", value.replace(root, "/root"))
    if key == "pid" and value is not None:
        return 1
    return value


class HostContractTests(HostServeCase):
    def test_the_root_bodies_match_the_shared_fixtures(self):
        (self.home / "sweep.json").write_text(json.dumps({
            "started": NOW - 30 * SEC, "ended": NOW - 20 * SEC,
            "revision": "abc1234", "pid": 777, "exit": 0,
            "projects": {"alpha": "ok", "beta": "ok"}}))
        host = Host.locate()
        server = types.SimpleNamespace(
            host=host, settings=settings(host), started_ms=NOW,
            actions=False, code_check=None)
        self.maxDiff = None
        for name, answer in (("host-status", host_status),
                             ("host-attention", host_attention)):
            with self.subTest(endpoint=name):
                code, body = answer(server, now=NOW)
                self.assertEqual(code, 200)
                expected = json.loads((FIXTURES / f"{name}.json").read_text())
                self.assertEqual(normalize(body, str(self.root)), expected)


class SocketHandoffTests(HostServeCase):
    """The service manager's socket, handed to a real daemon as fd 3: on a
    HEAD move the daemon exits 0, a request sent while none runs waits in
    the kernel's queue, and the next daemon on the same socket answers it."""

    CHECK = 0.2

    def spawn(self, checkout, listener):
        fd = listener.fileno()

        def handoff():
            os.dup2(fd, 3)
            # `dup2` onto itself leaves the close-on-exec flag as it was.
            os.set_inheritable(3, True)
            os.environ["LISTEN_FDS"] = "1"
            os.environ["LISTEN_PID"] = str(os.getpid())
        # `close_fds=False`: fd 3 is made in the child, after the parent's
        # descriptors are chosen; every other descriptor of this process is
        # close-on-exec, so fd 3 is the one the daemon inherits.
        return subprocess.Popen(
            [sys.executable, "-u", str(checkout / "factory.py"), "--serve"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            close_fds=False, preexec_fn=handoff)

    def test_only_a_matching_listen_pid_hands_the_socket_over(self):
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        self.enterContext(patch.object(holophyte.serve_watch, "LISTEN_FD",
                                       os.dup(held.fileno())))
        environ = {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid() + 1)}
        self.assertIsNone(adopted_socket(environ))
        self.assertEqual(set(environ), {"LISTEN_FDS", "LISTEN_PID"})
        environ["LISTEN_PID"] = str(os.getpid())
        adopted = adopted_socket(environ)
        self.addCleanup(adopted.close)
        self.assertEqual(adopted.getsockname(), held.getsockname())
        self.assertEqual(environ, {})

    def test_a_request_queued_between_daemons_is_answered_by_the_next(self):
        # The typed bind is not the socket's: named once, then ignored.
        self.host_config(bind="127.0.0.1:1")
        checkout = factory_checkout(self, self.root / "factory", self.CHECK)
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        first = self.spawn(checkout, listener)
        self.addCleanup(first.kill)
        lines = []
        deadline = monotonic() + 20
        for line in first.stdout:
            lines.append(line)
            if line.startswith("[holo2] serving ") or monotonic() > deadline:
                break
        self.assertIn(f"[holo2] serving 127.0.0.1:{port} ", "".join(lines))
        self.assertIn("[serve] bind 127.0.0.1:1 is ignored", "".join(lines))
        git(checkout, "commit", "-q", "--allow-empty", "-m", "B")
        rest, _ = first.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, "".join(lines) + rest)
        self.assertIn("serve exiting for the service manager", rest)

        # No daemon runs: the kernel queues this on the held socket.
        client = socket.create_connection(("127.0.0.1", port), timeout=20)
        self.addCleanup(client.close)
        client.sendall(b"GET /status HTTP/1.0\r\nHost: test\r\n\r\n")
        second = self.spawn(checkout, listener)
        self.addCleanup(second.wait, 10)
        self.addCleanup(second.kill)
        self.addCleanup(second.stdout.close)
        answer = b""
        while chunk := client.recv(65536):
            answer += chunk
        head, _, payload = answer.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 200"), answer[:200])
        self.assertEqual([p["name"] for p in json.loads(payload)["projects"]],
                         ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()

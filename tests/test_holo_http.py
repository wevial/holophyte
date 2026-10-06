"""`holo` with a client config naming `transport = "http"` calls the host
daemon's routes. The daemon is a real `HostServer` on a loopback port behind
a machine token, over a real registered project and store; `holo` runs as
its own process with the seat's home.

Run: python3 -m unittest discover -s tests -p 'test_holo_http.py' -v
"""
import contextlib
import http.client
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
import store
import store.tickets
from holophyte.config.project import Project
from holophyte.holo.transport import HTTP_ROUTES
from holophyte.host.registry import Host, settings
from holophyte.serve.serve_host import HostHandler, HostServer, host_tokens
from tests.test_holo_grammar import T0

ROOT = Path(__file__).resolve().parent.parent
MACHINE = "machine-token-value"
WRONG = "not-the-machine-token"

RECORDING_SSH = '''#!{python}
import json, sys
with open({record!r}, "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(255)
'''

COVERED = {
    ("runs",): ["runs"],
    ("run",): ["run", "1", "--ledger"],
    ("attention",): ["attention"],
    ("board",): ["board"],
    ("ticket",): ["ticket", "HOLO-1"],
    ("requeue",): ["requeue", "HOLO-1", "rerun"],
    ("send-back",): ["send-back", "1", "fix the test"],
    ("hold",): ["hold", "maintenance"],
    ("release",): ["release", "maintenance over"],
    ("pause",): ["pause", "HOLO-1", "stop"],
    ("resume",): ["resume", "HOLO-1", "go on"],
    ("abort",): ["abort", "HOLO-1", "end it"],
    ("start",): ["start"],
}


class HttpCase(unittest.TestCase):
    actions = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.seat, self.host = self.root / "seat", self.root / "host"
        self.desk, self.bin = self.root / "desk", self.root / "bin"
        for path in (self.seat, self.desk, self.bin):
            path.mkdir()
        self.record = self.root / "ssh.jsonl"
        patcher = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.host)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.alpha = self.register("alpha")
        self.conn = store.open(str(Project.locate(self.alpha).store_path))
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "native:HOLO", self.alpha)
        ssh = self.bin / "ssh"
        ssh.write_text(RECORDING_SSH.format(python=sys.executable,
                                            record=str(self.record)))
        ssh.chmod(0o755)
        self.serve()
        self.token_file = self.secret(self.seat / "daemon.token", MACHINE)
        self.client()

    def register(self, name):
        path = self.root / name
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text(
            f'[board]\nkind = "native"\nprefix = "HOLO"\n'
            f'[serve]\nname = "{name}"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(holophyte.cli.entry.cli(["project", "add", str(path)]))
        return path

    def secret(self, path, value):
        path.write_text(value + "\n")
        path.chmod(0o600)
        return path

    def serve(self):
        machine = self.secret(self.host / "machine.token", MACHINE)
        registry = self.host / "host.toml"
        registry.write_text(
            f"[serve]\nactions = {json.dumps(self.actions)}\n"
            f"machine_token_file = {json.dumps(str(machine))}\n"
            + registry.read_text())
        host = Host.locate()
        knobs = settings(host)
        _, write = host_tokens(host, knobs, "127.0.0.1", host.projects())
        self.server = HostServer(host, knobs, ("127.0.0.1", 0),
                                 console_dir=self.root / "no-console",
                                 read_token=write, write_token=write)
        self.requests = []
        parse = HostHandler.parse_request

        def recorded(handler):
            parsed = parse(handler)
            if parsed:
                self.requests.append((handler.command, handler.path))
            return parsed
        self.enterContext(patch.object(HostHandler, "parse_request", recorded))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def client(self, url=None, extra=""):
        (self.seat / "client.toml").write_text(
            f'transport = "http"\nurl = "{url or self.url}"\n'
            f'token_file = "{self.token_file}"\n' + extra)

    def holo(self, *args, home=None):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("HOLO_PROJECT", "HOLO_TRANSPORT")}
        environment.update(PATH=f"{self.bin}:{os.environ['PATH']}",
                           HOLOPHYTE_HOME=str(home or self.seat),
                           PYTHONPATH=str(ROOT),
                           GIT_CEILING_DIRECTORIES=str(self.root))
        return subprocess.run([sys.executable, "-m", "holophyte.holo", *args],
                              cwd=self.desk, capture_output=True, text=True,
                              env=environment)

    def claim(self, key):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60_000)
        store.tickets.transition(self.conn, ticket, "in_flight")
        return store.claim(self.conn, self.project_id, ticket, now=T0)

    def failed_run(self, key):
        run = self.claim(key)
        store.release(self.conn, run, "failed", "verify failed", now=T0 + 1)
        return run


class ReadTests(HttpCase):
    def test_runs_json_over_http_is_the_local_runs_json_with_its_transport(self):
        self.failed_run("HOLO-1")
        local = self.holo("runs", "--json", "-p", "alpha", home=self.host)
        self.assertEqual(local.returncode, 0, local.stderr)
        remote = self.holo("runs", "--json", "-p", "alpha")
        self.assertEqual(remote.returncode, 0, remote.stderr)
        self.assertEqual(json.loads(remote.stdout),
                         {**json.loads(local.stdout), "transport": "http"})
        self.assertEqual(len(json.loads(remote.stdout)["rows"]), 1)
        self.assertIn(f"via http to {self.url}", remote.stderr)
        self.assertEqual(self.requests, [("GET", "/projects/alpha/runs")])

    def test_every_route_table_row_is_one_the_running_daemon_serves(self):
        run = self.failed_run("HOLO-1")
        for words, route in HTTP_ROUTES.items():
            with self.subTest(words=words):
                if route.method == "POST":
                    self.assertIn(route.path.removeprefix("/actions/"),
                                  self.server.action_names)
                    continue
                path = route.path.format(N=run, KEY="HOLO-1")
                for tail in ("", "/files", "/ledger", "/turns")[
                        :4 if words == ("run",) else 1]:
                    connection = http.client.HTTPConnection(
                        "127.0.0.1", self.server.server_address[1], timeout=10)
                    try:
                        connection.request(
                            "GET", f"/projects/alpha{path}{tail}",
                            headers={"Authorization": f"Bearer {MACHINE}"})
                        status = connection.getresponse().status
                    finally:
                        connection.close()
                    self.assertNotEqual(status, 404, path + tail)


class WriteTests(HttpCase):
    def test_requeue_over_http_writes_the_requeue_row_with_its_note(self):
        run = self.failed_run("HOLO-1")
        completed = self.holo("requeue", "HOLO-1", "rerun", "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.conn.execute(
            "SELECT runId, note FROM interventions WHERE action = 'requeue'"
        ).fetchall(), [(run, "rerun")])
        (line,) = completed.stdout.splitlines()
        self.assertTrue(line.startswith("✓ HOLO-1"), line)

    def test_pause_over_http_reads_the_tickets_run_then_pauses_that_run(self):
        self.failed_run("HOLO-1")
        live = self.claim("HOLO-2")
        completed = self.holo("pause", "HOLO-2", "stop at a safe point",
                              "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (row_id, run_id, note), = self.conn.execute(
            "SELECT id, runId, note FROM interventions WHERE action = 'pause'")
        self.assertEqual(run_id, live)
        self.assertTrue(note.endswith("stop at a safe point"), note)
        (requested,), = self.conn.execute(
            "SELECT stopRequested FROM runs WHERE id = ?", (live,))
        self.assertEqual(requested, row_id)
        self.assertEqual(self.requests, [
            ("GET", "/projects/alpha/tickets/HOLO-2"),
            ("POST", "/projects/alpha/actions/pause")])


class RefusalTests(HttpCase):
    def test_approve_has_no_route_and_goes_nowhere_not_even_ssh(self):
        self.failed_run("HOLO-1")
        self.client(extra='host = "writer"\n')
        completed = self.holo("approve", "HOLO-1", "-p", "alpha")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("holo approve has no HTTP route", completed.stderr)
        self.assertIn('transport = "ssh"', completed.stderr)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.record.exists())

    def test_a_wrong_token_exits_one_naming_the_file_and_never_its_text(self):
        self.secret(self.token_file, WRONG)
        self.assertEqual(set(COVERED), set(HTTP_ROUTES))
        for words, argv in COVERED.items():
            with self.subTest(words=words):
                completed = self.holo(*argv, "-p", "alpha")
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertIn(f"{self.token_file} is not one it accepts",
                              completed.stderr)
                for token in (WRONG, MACHINE):
                    self.assertNotIn(token, completed.stdout + completed.stderr)

    def test_a_port_nothing_listens_on_exits_one_naming_the_url(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            url = f"http://127.0.0.1:{probe.getsockname()[1]}"
        self.client(url=url)
        for words, argv in COVERED.items():
            with self.subTest(words=words):
                completed = self.holo(*argv, "-p", "alpha")
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertIn(f"http to {url} failed", completed.stderr)

    def test_a_url_with_no_token_file_exits_two_naming_both_keys(self):
        (self.seat / "client.toml").write_text(f'url = "{self.url}"\n')
        completed = self.holo("runs", "-p", "alpha")
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("url needs token_file", completed.stderr)
        self.assertEqual(self.requests, [])


class ActionsClosedTests(HttpCase):
    actions = False

    def test_requeue_with_actions_closed_exits_one_naming_serve_actions(self):
        self.failed_run("HOLO-1")
        completed = self.holo("requeue", "HOLO-1", "rerun", "-p", "alpha")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("[serve] actions is not true", completed.stderr)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE action = 'requeue'"
        ).fetchone(), (0,))


if __name__ == "__main__":
    unittest.main()

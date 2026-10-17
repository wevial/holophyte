import http.client
import json
import os
import socket
import subprocess
import sys
from time import monotonic, sleep, time

from holophyte.config.project import Project
from tests.host_fixture import HostFixture, factory_checkout

TOKEN = "restart-test-machine-token"
ROUTE = "/actions/restart-daemon"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class RestartDaemonTests(HostFixture):
    def setUp(self):
        super().setUp()
        self.project = self.repo("alpha")
        self.cli("project", "add", str(self.project))
        self.registry = (self.home / "host.toml").read_text()
        self.token = self.home / "machine.token"
        self.token.write_text(TOKEN + "\n")
        self.token.chmod(0o600)
        self.checkout = factory_checkout(self, self.root / "factory", 0.1)
        self.ledger = self.home / "host-actions.jsonl"
        self.processes = []

    def configure(self, port, actions=True):
        (self.home / "host.toml").write_text(
            f'[serve]\nbind = "127.0.0.1:{port}"\n'
            f'actions = {str(actions).lower()}\n'
            f'machine_token_file = "{self.token}"\n' + self.registry)

    def listener(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        self.port = listener.getsockname()[1]
        return listener

    def spawn(self, listener=None, project=False):
        output = self.root / f"daemon-{len(self.processes)}.log"
        log = self.enterContext(output.open("w"))
        args = [sys.executable, "-u", str(self.checkout / "factory.py")]
        options = {}
        if listener is not None:
            fd = listener.fileno()

            def handoff():
                os.dup2(fd, 3)
                os.set_inheritable(3, True)
                os.environ["LISTEN_FDS"] = "1"
                os.environ["LISTEN_PID"] = str(os.getpid())

            options.update(close_fds=False, preexec_fn=handoff)
        if project:
            args += [str(self.project), "--serve", str(self.port)]
        else:
            args += ["--serve"]
        process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                                   **options)
        self.processes.append(process)
        self.addCleanup(self.stop, process)
        return process, output

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def start(self, actions=True):
        listener = self.listener()
        self.configure(self.port, actions)
        listener.close()
        process, output = self.spawn()
        return process, output, self.wait_status(process, output)

    def request(self, method, path, headers=None, body=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1)
        try:
            client.request(method, path, headers=headers or {},
                           body=None if body is None else json.dumps(body))
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def wait_status(self, process, output, after=None):
        deadline = monotonic() + 15
        last = None
        while monotonic() < deadline:
            self.assertIsNone(process.poll(), output.read_text())
            try:
                code, last = self.request("GET", "/status")
                if code == 200 and (after is None
                                    or last["daemon"]["started_ms"] > after):
                    return last
            except (OSError, http.client.HTTPException) as error:
                last = str(error)
            sleep(0.05)
        self.fail(f"daemon did not become ready: {last}\n{output.read_text()}")

    def assert_same_daemon(self, process, started):
        sleep(0.3)
        self.assertIsNone(process.poll())
        code, status = self.request("GET", "/status")
        self.assertEqual(code, 200, status)
        self.assertEqual(status["daemon"]["started_ms"], started)

    def test_owned_socket_reexecutes_after_answer_and_records_default_author(self):
        process, output, before = self.start()
        started = before["daemon"]["started_ms"]
        requested = int(time() * 1000)
        code, reply = self.request("POST", ROUTE, AUTH)
        self.assertEqual(code, 200, reply)
        self.assertEqual(reply, {
            "action": "restart-daemon", "ok": True,
            "detail": "the daemon drains its requests and restarts",
            "recorded": str(self.ledger),
        })
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({key: row[key] for key in
                          ("action", "author", "note", "via", "pid")}, {
            "action": "restart_daemon", "author": "maintainer", "note": None,
            "via": "POST /actions/restart-daemon", "pid": process.pid,
        })
        self.assertGreaterEqual(row["at"], requested)
        self.assertLessEqual(row["at"], int(time() * 1000))
        after = self.wait_status(process, output, after=started)
        self.assertGreater(after["daemon"]["started_ms"], started)
        self.assertIn("restart requested over HTTP", output.read_text())
        self.assertEqual(len(self.ledger.read_text().splitlines()), 1)

    def test_adopted_socket_exits_zero_and_queues_for_the_next_daemon(self):
        listener = self.listener()
        self.configure(self.port)
        first, output = self.spawn(listener)
        before = self.wait_status(first, output)
        code, reply = self.request("POST", ROUTE, AUTH,
                                   {"author": "operator", "note": "reload"})
        self.assertEqual((code, reply.get("ok")), (200, True), reply)
        first.wait(timeout=15)
        self.assertEqual(first.returncode, 0, output.read_text())
        self.assertIn("restart requested over HTTP", output.read_text())
        self.assertIn("serve exiting for the service manager", output.read_text())
        rows = [json.loads(line) for line in self.ledger.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["author"], rows[0]["note"]),
                         ("operator", "reload"))
        client = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        self.addCleanup(client.close)
        client.sendall(b"GET /status HTTP/1.0\r\nHost: test\r\n\r\n")
        second, _ = self.spawn(listener)
        response = http.client.HTTPResponse(client)
        response.begin()
        self.assertEqual(response.status, 200)
        after = json.loads(response.read())
        self.assertGreater(after["daemon"]["started_ms"],
                           before["daemon"]["started_ms"])
        self.assertIsNone(second.poll())

    def test_unauthorized_and_project_scoped_requests_do_not_restart_or_record(self):
        process, _, before = self.start()
        for path, headers, expected in ((ROUTE, {}, 401),
                                        ("/projects/alpha" + ROUTE, AUTH, 404)):
            with self.subTest(path=path):
                code, reply = self.request("POST", path, headers)
                self.assertEqual(code, expected, reply)
                self.assert_same_daemon(process, before["daemon"]["started_ms"])
                self.assertFalse(self.ledger.exists())

    def test_disabled_actions_do_not_restart_or_record(self):
        process, _, before = self.start(actions=False)
        code, reply = self.request("POST", ROUTE, AUTH)
        self.assertEqual(code, 404, reply)
        self.assert_same_daemon(process, before["daemon"]["started_ms"])
        self.assertFalse(self.ledger.exists())

    def test_unwritable_ledger_reports_failure_and_keeps_serving(self):
        process, _, before = self.start()
        self.ledger.mkdir()
        code, reply = self.request("POST", ROUTE, AUTH)
        self.assertEqual(code, 200, reply)
        self.assertEqual(reply["action"], "restart-daemon")
        self.assertFalse(reply["ok"])
        self.assertIsNone(reply["recorded"])
        self.assertIn(str(self.ledger), reply["detail"])
        self.assert_same_daemon(process, before["daemon"]["started_ms"])

    def test_project_daemon_has_no_restart_daemon_route(self):
        listener = self.listener()
        listener.close()
        config = Project.locate(self.project).config_path
        config.write_text(config.read_text() +
                          f'\n[serve]\nactions = true\ntoken_file = "{self.token}"\n')
        process, output = self.spawn(project=True)
        before = self.wait_status(process, output)
        code, reply = self.request("POST", ROUTE, AUTH)
        self.assertEqual(code, 404, reply)
        self.assert_same_daemon(process, before["daemon"]["started_ms"])
        self.assertFalse(self.ledger.exists())

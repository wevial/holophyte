"""`holo` with a client config naming `transport = "http"` calls the host
daemon's routes. The daemon is a real `HostServer` on a loopback port over a
real registered project and store, its machine token guarding reads as well
as actions, as on a bind beyond loopback; `holo` runs as its own process with
the seat's home.

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
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
import store
import store.board
import store.scouts
import store.tickets
from holophyte.config.project import Project
from holophyte.holo.transport import HTTP_ROUTES
from holophyte.host.registry import Host, settings
from holophyte.serve.serve_host import HostHandler, HostServer, host_tokens
from tests.phase_fixture import finish_run
from tests.test_holo_grammar import T0
from tests.test_scout_file import without_verify
from tests.test_serve_ticket_check import body as ticket_body

ROOT = Path(__file__).resolve().parent.parent
MACHINE = "machine-token-value"
WRONG = "not-the-machine-token"

RECORDING_SYSTEMCTL = '''#!{python}
import json, sys
with open({record!r}, "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
'''

RECORDING_SSH = '''#!{python}
import json, sys
with open({record!r}, "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
sys.exit(255)
'''

COVERED = {
    ("report",): ["report"],
    ("runs",): ["runs"],
    ("run",): ["run", "1", "--ledger"],
    ("attention",): ["attention"],
    ("board",): ["board"],
    ("ticket",): ["ticket", "HOLO-1"],
    ("scouts",): ["scouts"],
    ("scout", "show"): ["scout", "show", "1"],
    ("requeue",): ["requeue", "HOLO-1", "rerun"],
    ("send-back",): ["send-back", "1", "fix the test"],
    ("steer",): ["steer", "HOLO-1", "-n", "log the port"],
    ("hold",): ["hold", "maintenance"],
    ("release",): ["release", "maintenance over"],
    ("pause",): ["pause", "HOLO-1", "stop"],
    ("resume",): ["resume", "HOLO-1", "go on"],
    ("abort",): ["abort", "HOLO-1", "end it"],
    ("start",): ["start"],
    ("scout", "launch"): ["scout", "Map the merge gate", "--kind", "recon"],
    ("bug",): ["bug", "Save drops the title"],
    ("idea",): ["idea", "Show owner initials"],
    ("scout", "file"): ["scout", "file", "1", "1"],
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
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
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

    def test_board_json_over_http_is_the_local_board_json_with_its_transport(self):
        self.claim("HOLO-1")
        boards = []
        for home in (self.host, self.seat):
            completed = self.holo("board", "--json", "-p", "alpha", home=home)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            body = json.loads(completed.stdout)
            body.pop("now")
            boards.append(body)
        local, remote = boards
        self.assertEqual(remote, {**local, "transport": "http"})
        self.assertEqual([entry["ticket"] for column in local["columns"]
                          for entry in column["tickets"]], ["HOLO-1"])

    def test_report_over_http_is_the_local_report_and_draws_its_counts(self):
        now = int(time.time() * 1000)
        for key, outcome, ago in (("HOLO-1", "merged", 60 * 60_000),
                                  ("HOLO-2", "failed", 2 * 24 * 60 * 60_000)):
            run = self.claim(key)
            finish_run(self.conn, run, outcome, "ended", now=now - ago)
        reports = []
        for home in (self.host, self.seat):
            completed = self.holo("report", "--json", "-p", "alpha", home=home)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            body = json.loads(completed.stdout)
            for moment in ("now_ms", "from_ms"):
                body["window"].pop(moment)
            reports.append(body)
        local, remote = reports
        self.assertEqual(remote, {**local, "transport": "http"})
        self.assertEqual((local["shipped"]["merged"], local["shipped"]["failed"]),
                         (1, 1))
        page = self.holo("report", "-p", "alpha")
        self.assertEqual(page.returncode, 0, page.stderr)
        self.assertIn("Shipped   1 merged · 0 abandoned · 1 failed",
                      page.stdout)
        self.assertEqual(self.requests, [("GET", "/projects/alpha/report")] * 2)
        self.assertFalse(self.record.exists())

    def test_scouts_and_scout_show_over_http_read_the_scout_routes(self):
        scout = store.scouts.create_scout(self.conn, self.project_id,
                                          "Map the merge gate", "recon", 10)
        listed = self.holo("scouts", "-p", "alpha")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("Map the merge gate", listed.stdout)
        shown = self.holo("scout", "show", str(scout), "--json", "-p", "alpha")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout)["scout"]["id"], scout)
        self.assertEqual(self.requests, [
            ("GET", "/projects/alpha/scouts"),
            ("GET", f"/projects/alpha/scouts/{scout}")])
        refused = self.holo("scout", "show", "routes", "-p", "alpha")
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("scout id must be a positive integer", refused.stderr)
        self.assertEqual(len(self.requests), 2)

    def test_every_route_table_row_is_one_the_running_daemon_serves(self):
        run = self.failed_run("HOLO-1")
        scout = store.scouts.create_scout(self.conn, self.project_id,
                                          "Map the merge gate", "recon", 10)
        for words, route in HTTP_ROUTES.items():
            with self.subTest(words=words):
                if route.method == "POST":
                    self.assertIn(route.path.removeprefix("/actions/"),
                                  self.server.action_names)
                    continue
                path = route.path.format(
                    N=scout if words == ("scout", "show") else run,
                    KEY="HOLO-1")
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

    def test_requeue_and_start_print_their_words_and_the_rows_written(self):
        run = self.failed_run("HOLO-1")
        for note in ("an earlier hand", "another earlier hand"):
            store.record_intervention(self.conn, run, "approve", note, now=T0)
        systemctl = self.bin / "systemctl"
        systemctl.write_text(RECORDING_SYSTEMCTL.format(
            python=sys.executable, record=str(self.root / "systemctl.jsonl")))
        systemctl.chmod(0o755)
        self.enterContext(patch.dict(
            os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}"}))
        replies = []
        for argv in (["requeue", "HOLO-1", "rerun"], ["start"]):
            completed = self.holo(*argv, "--json", "-p", "alpha")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            replies.append(json.loads(completed.stdout))
        page = self.holo("start", "-p", "alpha")
        self.assertEqual(page.returncode, 0, page.stderr)
        (requeued,), = self.conn.execute(
            "SELECT id FROM interventions WHERE action = 'requeue'")
        launched = [row for (row,) in self.conn.execute(
            "SELECT id FROM interventions WHERE action = 'launch_loop'"
            " ORDER BY id")]
        self.assertEqual(len(launched), 2)
        self.assertNotIn(run, [requeued, *launched])
        self.assertEqual(
            [(reply["action"], reply["recorded"], reply["ok"])
             for reply in replies],
            [("requeue", requeued, True), ("start", launched[0], True)])
        self.assertIn(f"intervention {launched[1]} recorded", page.stdout)
        calls = (self.root / "systemctl.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(call)[:2] for call in calls],
                         [["--user", "start"]] * 2)

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


    def test_steer_hint_over_http_posts_the_hint_and_prints_the_detail(self):
        live = self.claim("HOLO-1")
        completed = self.holo("steer", "HOLO-1", "--hint", "-n", "log the port",
                              "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.conn.execute(
            "SELECT kind, note, runId FROM steerNotes").fetchall(),
            [("hint", "log the port", live)])
        (line,) = completed.stdout.splitlines()
        self.assertIn(f"HOLO-1 steered: hint (steer note 1) on live run {live}",
                      line)
        self.assertEqual(self.requests, [("POST", "/projects/alpha/actions/steer")])

    def test_scout_over_http_posts_the_ask_and_kind_and_prints_the_detail(self):
        live = store.scouts.create_scout(self.conn, self.project_id,
                                         "a live scout", "recon", 30)
        store.scouts.move_scout(self.conn, live, "planning", base_sha="a" * 40)
        with open(Host.locate().path, "a") as registry:
            registry.write("\n[scout]\nmax_parallel = 1\n")
        completed = self.holo("scout", "Map the merge gate", "--kind", "recon",
                              "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (scout_id, ask, kind), = self.conn.execute(
            "SELECT id, ask, kind FROM scouts WHERE id != ?", (live,))
        self.assertEqual((ask, kind), ("Map the merge gate", "recon"))
        self.assertEqual(completed.stdout,
                         f"✓ scout {scout_id} queued: 1 of 1 scout slots in"
                         " use\n")
        self.assertEqual(self.requests,
                         [("POST", "/projects/alpha/actions/scout")])

    def done_scout(self, *bodies):
        scout_id = store.scouts.create_scout(self.conn, self.project_id,
                                             "Map the merge gate", "recon", 30)
        store.scouts.move_scout(self.conn, scout_id, "planning",
                                base_sha="a" * 40)
        store.scouts.record_scout_actions(
            self.conn, scout_id, 1,
            [{"title": f"item {position}", "why": "it is slow", "kind": "fix",
              "body": text} for position, text in enumerate(bodies, 1)])
        store.scouts.end_scout(self.conn, scout_id, "done", "answered")
        return scout_id

    def test_scout_file_over_http_files_the_named_item_and_prints_the_detail(self):
        scout_id = self.done_scout(ticket_body("First"), ticket_body("Second"))
        completed = self.holo("scout", "file", str(scout_id), "1",
                              "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.conn.execute(
            "SELECT position, ticketKey FROM scoutActions WHERE scoutId = ?"
            " ORDER BY position", (scout_id,)).fetchall(),
            [(1, "HOLO-1"), (2, None)])
        self.assertEqual(completed.stdout,
                         f"✓ scout {scout_id} item 1 filed as HOLO-1 in"
                         " Backlog\n")
        self.assertEqual(self.requests,
                         [("POST", "/projects/alpha/actions/scout-file")])

    def test_a_draft_scout_file_over_http_prints_every_checker_problem(self):
        text = without_verify()
        scout_id = self.done_scout(text)
        problems = store.board.check_ticket(self.conn, self.project_id, text,
                                            column="backlog")["problems"]
        self.assertGreater(len(problems), 1)
        completed = self.holo("scout", "file", str(scout_id), "1",
                              "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"scout {scout_id} item 1 filed as HOLO-1 in Backlog",
                      completed.stdout)
        for problem in problems:
            self.assertIn(problem, completed.stdout)
        self.assertEqual(self.requests,
                         [("POST", "/projects/alpha/actions/scout-file")])


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

    def test_steer_withdraw_has_no_route_and_writes_nothing(self):
        self.claim("HOLO-1")
        completed = self.holo("steer", "HOLO-1", "--withdraw", "-n", "drop it",
                              "-p", "alpha")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("holo steer --withdraw has no HTTP route", completed.stderr)
        self.assertEqual(self.requests, [])

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

    def test_a_token_file_of_two_lines_exits_one_naming_the_file_not_its_text(self):
        self.secret(self.token_file, "secret-one\nsecret-two")
        for argv in (["runs"], ["requeue", "HOLO-1", "rerun"]):
            with self.subTest(argv=argv):
                completed = self.holo(*argv, "-p", "alpha")
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertIn(str(self.token_file), completed.stderr)
                for text in ("secret-one", "secret-two"):
                    self.assertNotIn(text, completed.stdout + completed.stderr)
        self.assertEqual(self.requests, [])

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

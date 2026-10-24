"""A scout's launch: `POST /actions/scout` on a project and a host daemon,
`holo scout`, `holo bug` and `holo idea` in-process, and the `holo mcp`
`scout` tool over stdio, each over a real git repository registered in a
temporary host registry, the scout sweep's spawn a recorder.

Run: python3 -m unittest discover -s tests -p 'test_scout_launch.py' -v
"""
import contextlib
import http.client
import io
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host_fixture import git  # noqa: E402

import holophyte.scout.scout_sweep as scout_sweep  # noqa: E402
import holophyte.serve.server  # noqa: E402
import store  # noqa: E402
from holophyte.config.project import Project  # noqa: E402
from holophyte.holo.cli import main as holo_main  # noqa: E402
from holophyte.host.registry import Host, settings  # noqa: E402
from holophyte.scout.scout_config import scout_config  # noqa: E402
from holophyte.serve.serve_host import HostServer, host_tokens  # noqa: E402
from store.scouts import create_scout, move_scout, scout  # noqa: E402
from tests.test_holo_mcp import McpCase  # noqa: E402
from tests.test_scout_process import ScoutFixture  # noqa: E402

TOKEN = "project-token-value"
MACHINE = "machine-token-value"
ASK = "Private tasks notify stakeholders on save"
ROUTES = """[scout]
lead = "first"
helper = "small"
[scout.leads.first]
harness = "claude"
model = "opus"
effort = "high"
[scout.leads.second]
harness = "codex"
model = "gpt-6.1-sol"
effort = "high"
[scout.helpers.small]
harness = "codex"
model = "gpt-6-luna"
effort = "low"
[scout.helpers.other]
harness = "claude"
model = "haiku"
effort = "low"
"""


def scout_rows(path):
    conn = store.open(str(Project.locate(path, adopt=False).store_path))
    try:
        return [scout(conn, scout_id) for (scout_id,) in
                conn.execute("SELECT id FROM scouts ORDER BY id")]
    finally:
        conn.close()


def first_event(path, scout_id):
    conn = store.open(str(Project.locate(path, adopt=False).store_path))
    try:
        kind, payload = conn.execute(
            "SELECT kind, payload FROM runEvents WHERE scoutId = ?"
            " ORDER BY seq LIMIT 1", (scout_id,)).fetchone()
    finally:
        conn.close()
    return kind, json.loads(payload)


def slots(max_parallel):
    with open(Host.locate().path, "a") as registry:
        registry.write(f"\n[scout]\nmax_parallel = {max_parallel}\n")


class LaunchCase(ScoutFixture):
    def setUp(self):
        super().setUp()
        self.spawned = []

        def recorder(argv, **options):
            scout_id = int(argv[-2])
            row = scout(self.conn(Path(argv[-1])), scout_id)
            self.spawned.append((argv, row.phase, row.base_sha))
            return SimpleNamespace(pid=4242)

        self.enterContext(patch.object(scout_sweep, "SPAWN", recorder))

    def secret(self, name, value):
        path = self.root / name
        path.write_text(value + "\n")
        path.chmod(0o600)
        return path

    def serve(self, path, actions=True):
        target = Project.locate(path, adopt=False)
        token = self.secret("serve.token", TOKEN)
        target.config_path.write_text(
            target.config_path.read_text()
            + f'[serve]\ntoken_file = "{token}"\n'
            f"actions = {json.dumps(actions)}\n")
        project = Project.locate(path)
        server = holophyte.serve.server.make_server(
            project, "127.0.0.1", 0, console_dir=self.root / "no-console",
            token=holophyte.serve.server.resolve_token(project, "127.0.0.1"))
        self.listen(server)
        return server

    def listen(self, server):
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)

    def post(self, server, path, body, token=TOKEN):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=30)
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            connection.request("POST", path, json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()


class DaemonLaunchTests(LaunchCase):
    def test_a_launch_records_the_config_defaults_then_starts_the_scout(self):
        path = self.project("alpha")
        server = self.serve(path)
        code, reply = self.post(server, "/actions/scout",
                                {"ask": ASK, "kind": "bug"})
        self.assertEqual(code, 200, reply)
        self.assertEqual((reply["ok"], reply["queued"]), (True, False))
        (row,) = scout_rows(path)
        self.assertEqual(reply["scout"], row.id)
        config = scout_config(Project.locate(path, adopt=False))
        self.assertEqual(
            (row.ask, row.kind, row.lead_route, row.helper_route, row.swarm,
             row.helper_turns, row.waves, row.budget_min),
            (ASK, "bug", config.lead, config.helper, config.swarm,
             config.helper_turns, config.waves, config.budget_min))
        (argv, phase, base), = self.spawned
        self.assertEqual(argv[-3:], ["--scout", str(row.id), str(path)])
        self.assertEqual((phase, base),
                         ("planning", git(path, "rev-parse", "main")))

    def test_the_first_event_is_scout_queued_with_its_author_and_route(self):
        path = self.project("alpha")
        server = self.serve(path)
        for body, author in (({"author": "test seat"}, "test seat"),
                             ({}, "maintainer")):
            with self.subTest(author=author):
                code, reply = self.post(server, "/actions/scout",
                                        {"ask": ASK, "kind": "bug", **body})
                self.assertEqual(code, 200, reply)
                self.assertEqual(
                    first_event(path, reply["scout"]),
                    ("scout_queued",
                     {"author": author, "via": "POST /actions/scout"}))

    def test_a_launch_past_the_hosts_scout_slots_stays_queued(self):
        path = self.project("alpha")
        slots(1)
        self.a_scout(path, "planning")
        server = self.serve(path)
        code, reply = self.post(server, "/actions/scout",
                                {"ask": ASK, "kind": "research"})
        self.assertEqual(code, 200, reply)
        self.assertEqual((reply["ok"], reply["queued"]), (True, True))
        self.assertIn("queued", reply["detail"])
        self.assertEqual(scout(self.conn(path), reply["scout"]).phase,
                         "queued")
        self.assertEqual(self.spawned, [])

    def test_a_spawn_that_fails_answers_not_ok_with_the_scout_failed(self):
        path = self.project("alpha")
        server = self.serve(path)

        def refused(argv, **options):
            raise OSError("no such interpreter")

        with patch.object(scout_sweep, "SPAWN", refused):
            code, reply = self.post(server, "/actions/scout",
                                    {"ask": ASK, "kind": "bug"})
        self.assertEqual(code, 200, reply)
        self.assertEqual((reply["ok"], reply["queued"]), (False, False))
        self.assertIn("could not start: no such interpreter", reply["detail"])
        self.assertEqual(scout(self.conn(path), reply["scout"]).phase,
                         "failed")

    def test_a_refused_launch_writes_no_scout_and_starts_nothing(self):
        path = self.project("alpha")
        server = self.serve(path)
        turns = scout_config(Project.locate(path, adopt=False)).helper_turns
        bad = (({"kind": "bug"}, "ask"),
               ({"ask": "  ", "kind": "bug"}, "ask"),
               ({"ask": ASK, "kind": "triage"}, "kind"),
               ({"ask": ASK, "kind": "bug", "swarm": 0}, "swarm"),
               ({"ask": ASK, "kind": "bug", "minutes": "30"}, "minutes"))
        for body, field in bad:
            with self.subTest(body=body):
                code, reply = self.post(server, "/actions/scout", body)
                self.assertEqual(code, 400, reply)
                self.assertIn(field, reply["error"])
        declined = (({"lead": "nosuch"}, "[scout.leads]"),
                    ({"swarm": turns + 1}, "helper_turns"))
        for extra, named in declined:
            with self.subTest(extra=extra):
                code, reply = self.post(server, "/actions/scout",
                                        {"ask": ASK, "kind": "bug", **extra})
                self.assertEqual(code, 200, reply)
                self.assertIs(reply["ok"], False)
                self.assertIn(named, reply["detail"])
        self.assertEqual(scout_rows(path), [])
        self.assertEqual(self.spawned, [])

    def test_the_host_daemon_guards_the_launch_and_a_closed_daemon_is_404(self):
        path = self.project("alpha")
        machine = self.secret("machine.token", MACHINE)
        registry = Host.locate().path
        registry.write_text("[serve]\nactions = true\nmachine_token_file ="
                            f" {json.dumps(str(machine))}\n"
                            + registry.read_text())
        host = Host.locate()
        knobs = settings(host)
        _, write = host_tokens(host, knobs, "127.0.0.1", host.projects())
        server = HostServer(host, knobs, ("127.0.0.1", 0),
                            console_dir=self.root / "no-console",
                            read_token=write, write_token=write)
        self.listen(server)
        body = {"ask": ASK, "kind": "recon"}
        code, _ = self.post(server, "/projects/alpha/actions/scout", body,
                            token=None)
        self.assertEqual(code, 401)
        self.assertEqual(scout_rows(path), [])
        code, reply = self.post(server, "/projects/alpha/actions/scout", body,
                                token=MACHINE)
        self.assertEqual(code, 200, reply)
        self.assertEqual([row.ask for row in scout_rows(path)], [ASK])

        other = self.project("beta")
        closed = self.serve(other, actions=False)
        code, _ = self.post(closed, "/actions/scout", body)
        self.assertEqual(code, 404)
        self.assertEqual(scout_rows(other), [])


class HoloLaunchTests(LaunchCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"HOLO_TRANSPORT": "local"}))

    def holo(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = holo_main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_holo_scout_launches_on_the_routes_and_counts_it_names(self):
        path = self.project("alpha")
        config = Project.locate(path, adopt=False).config_path
        config.write_text(config.read_text() + ROUTES)
        code, out, err = self.holo(
            "scout", "Map the merge gate", "--kind", "recon", "--lead",
            "second", "--helper", "other", "--swarm", "2", "--minutes", "10",
            "--json", "-p", str(path))
        self.assertEqual(code, 0, err)
        (row,) = scout_rows(path)
        result = json.loads(out)
        self.assertEqual((result["ok"], result["action"]),
                         (True, "scout launch"))
        self.assertIn(f"scout {row.id}", result["detail"])
        self.assertEqual(
            (row.ask, row.kind, row.lead_route, row.helper_route, row.swarm,
             row.budget_min),
            ("Map the merge gate", "recon", "second", "other", 2, 10))
        self.assertEqual(first_event(path, row.id)[1]["via"], "holo scout")

    def test_bug_and_idea_fix_the_kind_and_scout_needs_one(self):
        path = self.project("alpha")
        for argv, kind in ((("bug", "Save drops the title"), "bug"),
                           (("idea", "Show owner initials"), "research")):
            with self.subTest(argv=argv):
                code, _, err = self.holo(*argv, "-p", str(path))
                self.assertEqual(code, 0, err)
                self.assertEqual(scout_rows(path)[-1].kind, kind)
        before = scout_rows(path)
        code, _, err = self.holo("scout", "Save drops the title",
                                 "-p", str(path))
        self.assertEqual(code, 2)
        self.assertIn("--kind", err)
        self.assertEqual(scout_rows(path), before)


class McpLaunchTests(McpCase):
    def test_the_scout_tool_records_the_ask_signed_via_mcp(self):
        conn = store.open(str(self.store_path))
        try:
            (project,) = conn.execute("SELECT id FROM projects").fetchone()
            live = create_scout(conn, project, "a live scout",
                                             "recon", 30)
            move_scout(conn, live, "planning", base_sha="a" * 40)
        finally:
            conn.close()
        slots(1)

        async def use(client, _):
            return (await client.list_tools()).tools
        (tool,) = [tool for tool in self.session(use) if tool.name == "scout"]
        self.assertLessEqual({"note", "author", "kind"},
                             set(tool.input_schema["required"]))

        result = self.call("scout", {"project": "alpha",
                                     "note": "Map the merge gate",
                                     "kind": "recon", "author": "test seat"})
        self.assertIs(result.is_error, False, result.content)
        self.assertIs(result.structured_content["ok"], True)
        row = scout_rows(self.alpha)[-1]
        self.assertEqual((row.ask, row.phase), ("Map the merge gate", "queued"))
        kind, payload = first_event(self.alpha, row.id)
        self.assertEqual((kind, payload["author"]),
                         ("scout_queued", "test seat via MCP"))

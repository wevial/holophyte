"""A done scout's thread: `POST /actions/scout-message` on a project and a
host daemon, the `thread` of `GET /scouts/ID`, `holo scout ask` and
`holo scout thread` in-process, and the `holo mcp` `scout_ask` tool over
stdio, each over a real git repository, the scout sweep's spawn a recorder.

Run: python3 -m unittest discover -s tests -p 'test_scout_thread_action.py' -v
"""
import contextlib
import io
import json
import os
from unittest.mock import patch

import store
from holophyte.host.registry import Host, settings
from holophyte.scout.views import scout_detail
from holophyte.serve.contract import ROUTES
from holophyte.serve.serve_host import HostServer, host_tokens
from store.scout_thread import messages
from store.scouts import create_scout, end_scout, move_scout
from tests.serve_contract_fixture import NOW, normalize_contract
from tests.test_holo_mcp import McpCase
from tests.test_scout_launch import MACHINE, LaunchCase, slots
from tests.test_serve_contract import walk
from tests.test_serve_scouts import FIXTURES, ScoutCase

ASKED = "Does this hit recurring tasks too?"
KEYS = ["id", "author", "mode", "text", "blocks", "answers", "at",
        "consumed_by", "consumed_ms"]


def done_scout(conn):
    (project,) = conn.execute("SELECT id FROM projects").fetchone()
    scout_id = create_scout(conn, project, "where is the bug?", "research", 30)
    move_scout(conn, scout_id, "planning", base_sha="a" * 40)
    end_scout(conn, scout_id, "done", "the lead wrote its report")
    return scout_id


def live_scout(conn, phase="planning"):
    (project,) = conn.execute("SELECT id FROM projects").fetchone()
    scout_id = create_scout(conn, project, "a live scout", "recon", 30)
    move_scout(conn, scout_id, "planning", base_sha="b" * 40)
    if phase != "planning":
        move_scout(conn, scout_id, phase)
    return scout_id


class ActionCase(LaunchCase):
    def thread(self, path, scout_id):
        return messages(self.conn(path), scout_id)

    def ask(self, server, body, path="/actions/scout-message", **options):
        return self.post(server, path, {"mode": "ask", "text": ASKED, **body},
                         **options)


class DaemonAskTests(ActionCase):
    def test_an_ask_is_recorded_then_its_answer_starts_in_a_free_slot(self):
        path = self.project("alpha")
        scout_id = done_scout(self.conn(path))
        server = self.serve(path)
        code, reply = self.ask(server, {"scout": scout_id})
        self.assertEqual(code, 200, reply)
        self.assertEqual((reply["ok"], reply["queued"]), (True, False))
        (message,) = self.thread(path, scout_id)
        self.assertEqual((reply["message"], message.mode, message.text,
                          message.author),
                         (message.id, "ask", ASKED, "maintainer"))
        (argv, phase, _), = self.spawned
        self.assertEqual(argv[-3:], ["--scout", str(scout_id), str(path)])
        self.assertEqual(phase, "answering")

    def test_an_ask_past_the_hosts_scout_slots_waits_untaken(self):
        path = self.project("alpha")
        slots(1)
        live_scout(self.conn(path))
        scout_id = done_scout(self.conn(path))
        server = self.serve(path)
        code, reply = self.ask(server, {"scout": scout_id,
                                        "author": "test seat"})
        self.assertEqual(code, 200, reply)
        self.assertEqual((reply["ok"], reply["queued"]), (True, True))
        self.assertIn("waits for a scout slot", reply["detail"])
        (message,) = self.thread(path, scout_id)
        self.assertEqual((message.id, message.author, message.consumed_by),
                         (reply["message"], "test seat", None))
        self.assertEqual(self.spawned, [])

    def test_a_refused_ask_writes_nothing_and_starts_nothing(self):
        path = self.project("alpha")
        slots(1)
        swarming = live_scout(self.conn(path), "swarming")
        scout_id = done_scout(self.conn(path))
        server = self.serve(path)
        code, first = self.ask(server, {"scout": scout_id})
        self.assertEqual((code, first["queued"]), (200, True), first)
        before = self.thread(path, scout_id)
        for body, field in (({"scout": "14"}, "scout"),
                            ({"scout": 0}, "scout"),
                            ({"scout": scout_id, "mode": None}, "mode"),
                            ({"scout": scout_id, "mode": "chat"}, "mode"),
                            ({"scout": scout_id, "text": "  "}, "text")):
            with self.subTest(body=body):
                code, reply = self.post(
                    server, "/actions/scout-message",
                    {key: value for key, value in
                     {"mode": "ask", "text": ASKED, **body}.items()
                     if value is not None})
                self.assertEqual(code, 400, reply)
                self.assertIn(field, reply["error"])
        code, reply = self.ask(server, {"scout": 999})
        self.assertEqual((code, reply["error"]), (404, "no such scout"))
        for target, named in ((swarming, "swarming"),
                              (scout_id, f"message {first['message']}")):
            with self.subTest(scout=target):
                code, reply = self.ask(server, {"scout": target})
                self.assertEqual(code, 200, reply)
                self.assertIs(reply["ok"], False)
                self.assertIn(named, reply["detail"])
        self.assertEqual(self.thread(path, scout_id), before)
        self.assertEqual(self.thread(path, swarming), [])
        self.assertEqual(self.spawned, [])

    def test_the_host_daemon_guards_the_ask_and_a_closed_daemon_is_404(self):
        path = self.project("alpha")
        scout_id = done_scout(self.conn(path))
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
        route = "/projects/alpha/actions/scout-message"
        code, _ = self.ask(server, {"scout": scout_id}, route, token=None)
        self.assertEqual(code, 401)
        self.assertEqual(self.thread(path, scout_id), [])
        code, reply = self.ask(server, {"scout": scout_id}, route,
                               token=MACHINE)
        self.assertEqual(code, 200, reply)
        self.assertEqual([message.text for message in
                          self.thread(path, scout_id)], [ASKED])

        other = self.project("beta")
        closed_scout = done_scout(self.conn(other))
        closed = self.serve(other, actions=False)
        code, _ = self.ask(closed, {"scout": closed_scout})
        self.assertEqual(code, 404)
        self.assertEqual(self.thread(other, closed_scout), [])


class HoloAskTests(ActionCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"HOLO_TRANSPORT": "local"}))

    def holo(self, *argv):
        from holophyte.holo.cli import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_holo_scout_ask_records_the_ask_and_thread_prints_it(self):
        path = self.project("alpha")
        scout_id = done_scout(self.conn(path))
        code, _, err = self.holo("scout", "ask", str(scout_id), "-p",
                                 str(path))
        self.assertEqual(code, 2, err)
        self.assertEqual(self.thread(path, scout_id), [])
        code, out, err = self.holo("scout", "ask", str(scout_id), ASKED,
                                   "--json", "-p", str(path))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        (message,) = self.thread(path, scout_id)
        self.assertEqual((result["ok"], result["action"]), (True, "scout ask"))
        self.assertIn(f"message {message.id}", result["detail"])
        self.assertEqual(len(self.spawned), 1)

        code, out, err = self.holo("scout", "thread", str(scout_id), "--json",
                                   "-p", str(path))
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertEqual(shown["scout"], scout_id)
        self.assertEqual([(item["id"], item["mode"], item["text"])
                          for item in shown["thread"]],
                         [(message.id, "ask", ASKED)])
        code, out, err = self.holo("scout", "thread", str(scout_id),
                                   "-p", str(path))
        self.assertEqual(code, 0, err)
        (line,) = out.splitlines()
        for word in ("maintainer", "ask", ASKED):
            self.assertIn(word, line)


class ThreadReadTests(ScoutCase):
    def test_the_thread_lists_the_messages_and_the_events_that_took_them(self):
        code, body = scout_detail(self.project, str(self.done), now=NOW)
        self.assertEqual(code, 200, body)
        asked, answered, waiting = body["thread"]
        self.assertEqual([list(item) for item in body["thread"]], [KEYS] * 3)
        self.assertEqual([item["mode"] for item in body["thread"]],
                         ["ask", "answer", "ask"])
        self.assertEqual(answered["answers"], asked["id"])
        self.assertEqual(len(answered["blocks"]), 1)
        self.assertIn(asked["consumed_by"],
                      [event["id"] for event in body["events"]
                       if event["kind"] == "scout_answer_started"])
        self.assertIsNone(waiting["consumed_by"])
        code, body = scout_detail(self.project, str(self.swarming), now=NOW)
        self.assertEqual((code, body["thread"]), (200, []))

    def test_the_seeded_answer_is_the_pinned_scout_answer_with_its_thread(self):
        self.maxDiff = None
        code, body = scout_detail(self.project, str(self.done), now=NOW)
        self.assertEqual(code, 200, body)
        pinned = json.loads((FIXTURES / "scout.json").read_text())
        self.assertEqual(normalize_contract(body), pinned)
        walk(pinned, ROUTES["scout"][0], "scout")


class McpAskTests(McpCase):
    def test_the_scout_ask_tool_records_the_ask_signed_via_mcp(self):
        conn = store.open(str(self.store_path))
        try:
            live_scout(conn)
            scout_id = done_scout(conn)
        finally:
            conn.close()
        slots(1)

        async def use(client, _):
            return (await client.list_tools()).tools
        (tool,) = [tool for tool in self.session(use)
                   if tool.name == "scout_ask"]
        self.assertLessEqual({"scout", "note", "author"},
                             set(tool.input_schema["required"]))
        result = self.call("scout_ask", {"project": "alpha", "scout": scout_id,
                                         "note": "Go on",
                                         "author": "test seat"})
        self.assertIs(result.is_error, False, result.content)
        self.assertIs(result.structured_content["ok"], True)
        conn = store.open(str(self.store_path))
        try:
            (message,) = messages(conn, scout_id)
        finally:
            conn.close()
        self.assertEqual((message.text, message.author, message.consumed_by),
                         ("Go on", "test seat via MCP", None))
        shown = self.call("scout_show", {"project": "alpha",
                                         "scout": scout_id})
        self.assertIs(shown.is_error, False, shown.content)
        self.assertEqual([item["id"] for item in
                          shown.structured_content["thread"]], [message.id])

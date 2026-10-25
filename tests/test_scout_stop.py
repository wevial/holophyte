"""Stopping a scout: `POST /projects/NAME/actions/scout-stop` on a host
daemon against real `factory.py --scout ID PROJECT` processes running stub
lead and helper CLIs, `holo scout stop` over the local transport, and the
`holo mcp` `scout_stop` tool over stdio.

Run: python3 -m unittest discover -s tests -p 'test_scout_stop.py' -v
"""
import errno
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.scout.scout_sweep as scout_sweep
import holophyte.scout.stop as stop_module
import store
from holophyte.cli.host_modes import FACTORY_PATH
from holophyte.config.project import Project
from holophyte.holo.resolve import client_path
from holophyte.host.registry import Host, settings
from holophyte.scout.lead import REPORT_OPENING
from holophyte.serve.serve_host import HostServer, host_tokens
from store.scouts import claim_scout, create_scout, end_scout, move_scout, scout
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_holo_mcp import McpCase
from tests.test_scout_lead import running
from tests.test_scout_process import ScoutFixture

ROOT = Path(__file__).resolve().parent.parent
MACHINE = "machine-token-value"
STOP = "/projects/alpha/actions/scout-stop"
REPORT = {"summary": "nothing found", "findings": [], "actions": []}
QUESTIONS = ["Where is the retry loop?", "Who calls the retry loop?"]
WAIT_S = 30


def wait_for(check, timeout=WAIT_S):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


class StopCase(ScoutFixture):
    """`alpha`, a registered repository with stub lead CLIs on `PATH`, and a
    host daemon over it with actions on behind a machine token."""

    def setUp(self):
        super().setUp()
        self.path = self.project("alpha")
        self.store_conn = self.conn(self.path)
        self.children = []

    def start_host(self, actions=True):
        token = self.home / "machine.token"
        token.write_text(MACHINE + "\n")
        token.chmod(0o600)
        registry = Host.locate().path
        registry.write_text(f'[serve]\nmachine_token_file = "{token}"\n'
                            f'actions = {str(actions).lower()}\n'
                            + registry.read_text())
        host = Host.locate()
        knobs = settings(host)
        read, write = host_tokens(host, knobs, "127.0.0.1", host.projects())
        server = HostServer(host, knobs, ("127.0.0.1", 0),
                            console_dir=self.root / "no-console",
                            read_token=read, write_token=write)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def post(self, payload, token=MACHINE):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", STOP, body=json.dumps(payload),
                         headers=headers)
            response = conn.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            conn.close()

    def stop(self, scout_id, note="wrong project"):
        return self.post({"scout": scout_id, "note": note})

    def planning(self, host=None, kind="research", **counts):
        (project,) = self.store_conn.execute(
            "SELECT id FROM projects").fetchone()
        scout_id = create_scout(self.store_conn, project, "where is the bug?",
                                kind, 30, **counts)
        move_scout(self.store_conn, scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main"), host=host)
        return scout_id

    def events(self, scout_id):
        return [(kind, None if payload is None else json.loads(payload))
                for kind, payload in self.store_conn.execute(
                    "SELECT kind, payload FROM runEvents WHERE scoutId = ?"
                    " ORDER BY seq", (scout_id,))]

    def written(self):
        """Every scout row and every scout event in the store."""
        return (self.store_conn.execute("SELECT * FROM scouts ORDER BY id")
                .fetchall(),
                self.store_conn.execute("SELECT * FROM runEvents"
                                        " WHERE scoutId IS NOT NULL"
                                        " ORDER BY id").fetchall())

    def spawn_scout(self, scout_id):
        log = open(self.root / f"scout-{scout_id}.log", "ab")
        self.addCleanup(log.close)
        proc = subprocess.Popen(
            [sys.executable, "-u", str(FACTORY_PATH), "--scout", str(scout_id),
             str(self.path)], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.addCleanup(self.reap, proc)
        return proc

    def reap(self, proc):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def child(self, name):
        """A pid file a stub turn's child writes; cleanup fails the test
        when that child is still running."""
        pid_file = self.root / f"{name}.pid"
        self.children.append(pid_file)
        if len(self.children) == 1:
            self.addCleanup(self.no_child_left)
        return pid_file

    def no_child_left(self):
        left = []
        for pid_file in self.children:
            text = pid_file.read_text().strip() if pid_file.exists() else ""
            if text and running(int(text)):
                os.kill(int(text), signal.SIGKILL)
                left.append(int(text))
        self.assertEqual(left, [])

    def pid_of(self, pid_file):
        self.assertTrue(wait_for(lambda: pid_file.exists()
                                 and pid_file.read_text().strip()),
                        f"{pid_file.name} was never written")
        return int(pid_file.read_text())


class RunningScoutStopTests(StopCase):
    def test_a_stop_during_the_plan_turn_kills_it_and_ends_the_scout_stopped(self):
        pid_file = self.child("plan-child")
        self.stubs.answer(plan=fenced({"tasks": []}), plan_sleep=600,
                          plan_pid_file=str(pid_file), report=fenced(REPORT))
        self.start_host()
        scout_id = self.planning()
        proc = self.spawn_scout(scout_id)
        child = self.pid_of(pid_file)

        status, answer = self.stop(scout_id)
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["ok"], answer["scout"], answer["pid"]),
                         (True, scout_id, proc.pid))
        self.assertEqual(answer["detail"],
                         f"stop sent to scout {scout_id}'s process {proc.pid}")
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason),
                         ("stopped", "stopped by maintainer: wrong project"))
        self.assertTrue(wait_for(lambda: not running(child)))
        goals = [call["argv"][-1] for call in self.stubs.calls()]
        self.assertFalse([goal for goal in goals
                          if goal.startswith(REPORT_OPENING)])
        kinds = [kind for kind, _ in self.events(scout_id)]
        requested = kinds.index("scout_stop_requested")
        self.assertGreater(kinds.index("scout_stopped"), requested)
        payload = self.events(scout_id)[requested][1]
        self.assertEqual((payload["note"], payload["author"], payload["pid"]),
                         ("wrong project", "maintainer", proc.pid))

    def test_a_stop_during_the_lead_route_probe_kills_it_and_ends_the_scout(self):
        pid_file = self.child("probe-child")
        self.stubs.answer(probe_sleep=600, probe_pid_file=str(pid_file),
                          plan=fenced({"tasks": []}), report=fenced(REPORT))
        self.start_host()
        scout_id = self.planning()
        proc = self.spawn_scout(scout_id)
        child = self.pid_of(pid_file)

        status, answer = self.stop(scout_id)
        self.assertEqual((status, answer["ok"], answer["pid"]),
                         (200, True, proc.pid), answer)
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason),
                         ("stopped", "stopped by maintainer: wrong project"))
        self.assertTrue(wait_for(lambda: not running(child)))
        self.assertEqual([call["turn"] for call in self.stubs.calls()],
                         ["probe"])
        self.assertIn("scout_stopped",
                      [kind for kind, _ in self.events(scout_id)])

    def test_a_stop_while_swarming_stops_both_helpers_and_their_children(self):
        self.stops_both_helpers("research", "read")

    def test_a_stop_while_swarming_stops_both_run_helpers_and_their_children(self):
        self.stops_both_helpers("bug", "run")

    def stops_both_helpers(self, kind, mode):
        pid_files = [self.child(f"helper-{n}") for n in (1, 2)]
        self.stubs.answer(
            plan=fenced({"tasks": [{"question": question, "paths": [],
                                    "mode": mode}
                                   for question in QUESTIONS]}),
            report=fenced(REPORT),
            helpers={question: {"helper_sleep": 600,
                                "helper_pid_file": str(pid_file)}
                     for question, pid_file in zip(QUESTIONS, pid_files)})
        self.start_host()
        scout_id = self.planning(kind=kind, swarm=2, helper_turns=2, waves=1)
        proc = self.spawn_scout(scout_id)
        children = [self.pid_of(pid_file) for pid_file in pid_files]

        def tasks():
            return self.store_conn.execute(
                "SELECT phase FROM scoutTasks WHERE scoutId = ? ORDER BY id",
                (scout_id,)).fetchall()

        self.assertTrue(wait_for(lambda: tasks() == [("running",)] * 2),
                        tasks())
        self.assertEqual(scout(self.store_conn, scout_id).phase, "swarming")
        status, answer = self.stop(scout_id)
        self.assertEqual((status, answer["ok"], answer["pid"]),
                         (200, True, proc.pid), answer)
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)
        self.assertEqual(tasks(), [("stopped",)] * 2)
        self.assertTrue(wait_for(lambda: not any(map(running, children))))
        self.assertEqual(scout(self.store_conn, scout_id).phase, "stopped")
        self.assertEqual(self.stubs.turns("report"), [])

    def test_a_stop_during_a_run_helpers_setup_kills_it_and_ends_it_stopped(self):
        pid_file = self.child("setup")
        config = Project.locate(self.path, adopt=False).config_path
        config.write_text(config.read_text() + "[worktree]\nsetup = "
                          f'["echo $$ > {pid_file}; exec sleep 600"]\n')
        self.stops_one_run_helper(pid_file)

    def test_a_stop_during_a_run_helpers_route_probe_ends_it_stopped(self):
        pid_file = self.child("helper-probe")
        self.stubs.answer(binaries={"codex": {
            "probe_sleep": 600, "probe_pid_file": str(pid_file)}})
        self.stops_one_run_helper(pid_file)
        self.assertEqual([payload for kind, payload in self.events(
            self.scout_id) if kind == "scout_helpers"], [])

    def stops_one_run_helper(self, pid_file):
        answers = json.loads((self.stubs.directory / "answers.json")
                             .read_text())
        self.stubs.answer(**dict(answers, plan=fenced({"tasks": [
            {"question": QUESTIONS[0], "paths": [], "mode": "run"}]}),
            report=fenced(REPORT)))
        self.start_host()
        self.scout_id = self.planning(kind="bug", swarm=1, helper_turns=1,
                                      waves=1)
        proc = self.spawn_scout(self.scout_id)
        child = self.pid_of(pid_file)
        status, answer = self.stop(self.scout_id)
        self.assertEqual((status, answer["ok"], answer["pid"]),
                         (200, True, proc.pid), answer)
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)
        self.assertEqual(self.store_conn.execute(
            "SELECT phase, excerpt FROM scoutTasks WHERE scoutId = ?",
            (self.scout_id,)).fetchall(), [("stopped", "")])
        self.assertTrue(wait_for(lambda: not running(child)))
        self.assertEqual(scout(self.store_conn, self.scout_id).phase,
                         "stopped")
        self.assertEqual(self.stubs.turns("helper"), [])


class DirectStopTests(StopCase):
    def test_a_queued_scout_ends_stopped_and_the_sweep_never_starts_it(self):
        (project,) = self.store_conn.execute(
            "SELECT id FROM projects").fetchone()
        scout_id = create_scout(self.store_conn, project, "where is the bug?",
                                "research", 30)
        self.start_host()
        status, answer = self.stop(scout_id, "not needed")
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["ok"], answer["pid"], answer["detail"]),
                         (True, None, f"scout {scout_id} stopped"))
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason),
                         ("stopped", "stopped by maintainer: not needed"))

        spawned = []
        with patch.object(scout_sweep, "SPAWN",
                          lambda argv, **_: spawned.append(argv)
                          or SimpleNamespace(pid=4242)):
            self.sweep_pass()
        self.assertEqual(spawned, [])
        self.assertEqual(scout(self.store_conn, scout_id).phase, "stopped")

    def test_a_recorded_pid_that_is_no_scout_is_not_signalled(self):
        sleeper = subprocess.Popen(["sleep", "600"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        scout_id = self.planning()
        claim_scout(self.store_conn, scout_id, sleeper.pid)
        move_scout(self.store_conn, scout_id, "swarming")
        self.start_host()
        status, answer = self.stop(scout_id)
        self.assertEqual((status, answer["ok"], answer["pid"]),
                         (200, True, None), answer)
        time.sleep(0.5)
        self.assertIsNone(sleeper.poll())
        row = scout(self.store_conn, scout_id)
        self.assertEqual(row.phase, "stopped")
        self.assertTrue(row.reason.endswith("no scout process was running"),
                        row.reason)

    def test_another_projects_scout_with_the_same_id_is_not_signalled(self):
        scout_id = self.planning()
        other = self.root / "other project"
        other.mkdir()
        named = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)",
             "--scout", str(scout_id), str(other)])
        self.addCleanup(named.wait)
        self.addCleanup(named.kill)
        claim_scout(self.store_conn, scout_id, named.pid)
        move_scout(self.store_conn, scout_id, "swarming")
        self.start_host()
        status, answer = self.stop(scout_id)
        self.assertEqual((status, answer["ok"], answer["pid"]),
                         (200, True, None), answer)
        time.sleep(0.5)
        self.assertIsNone(named.poll())
        self.assertTrue(scout(self.store_conn, scout_id).reason.endswith(
            "no scout process was running"))

    def test_a_scout_process_gone_before_the_signal_ends_the_scout_stopped(self):
        scout_id = self.planning()
        named = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)",
             "--scout", str(scout_id),
             str(self.path)])
        self.addCleanup(named.wait)
        self.addCleanup(named.kill)
        claim_scout(self.store_conn, scout_id, named.pid)
        move_scout(self.store_conn, scout_id, "swarming")
        real = stop_module.runs_scout

        def exits_after_the_check(pid, checked_id, project_path):
            running_scout = real(pid, checked_id, project_path)
            named.kill()
            named.wait()
            return running_scout

        self.start_host()
        with patch.object(stop_module, "runs_scout", exits_after_the_check):
            status, answer = self.stop(scout_id)
        self.assertEqual((status, answer["ok"], answer["pid"]),
                         (200, True, None), answer)
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason),
                         ("stopped", "stopped by maintainer: wrong project;"
                                     " no scout process was running"))
        self.assertEqual(self.events(scout_id)[-1][1]["pid"], named.pid)

    def test_a_signal_the_process_owner_refuses_leaves_the_scout_live(self):
        scout_id = self.planning()
        named = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)",
             "--scout", str(scout_id),
             str(self.path)])
        self.addCleanup(named.wait)
        self.addCleanup(named.kill)
        claim_scout(self.store_conn, scout_id, named.pid)
        move_scout(self.store_conn, scout_id, "swarming")

        def denied(_pid, _signal):
            raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))

        self.start_host()
        with patch.object(stop_module, "os", SimpleNamespace(kill=denied)):
            status, answer = self.stop(scout_id)
        self.assertEqual((status, answer["ok"]), (200, False), answer)
        self.assertIn(str(named.pid), answer["detail"])
        self.assertIsNone(named.poll())
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason), ("swarming", None))

    def test_a_scout_live_on_another_host_is_left_unchanged(self):
        scout_id = self.planning(host="other-writer")
        claim_scout(self.store_conn, scout_id, os.getpid(), host="other-writer")
        before = self.written()
        self.start_host()
        status, answer = self.stop(scout_id)
        self.assertEqual((status, answer["ok"]), (200, False), answer)
        self.assertIn("other-writer", answer["detail"])
        self.assertEqual(self.written(), before)


class RefusedStopTests(StopCase):
    def test_a_done_scout_and_bad_requests_write_nothing(self):
        done = self.planning()
        end_scout(self.store_conn, done, "done", "answered")
        before = self.written()
        self.start_host()
        status, answer = self.stop(done)
        self.assertEqual((status, answer["ok"]), (200, False), answer)
        self.assertIn("done", answer["detail"])
        self.assertEqual(self.events(done), [])
        for payload, code in (
                ({"scout": 999, "note": "why"}, 404),
                ({"scout": str(done), "note": "why"}, 400),
                ({"scout": done}, 400),
                ({"scout": done, "note": "  "}, 400)):
            with self.subTest(payload=payload):
                status, answer = self.post(payload)
                self.assertEqual(status, code, answer)
                if code == 404:
                    self.assertEqual(answer["error"], "no such scout")
        self.assertEqual(self.post({"scout": done, "note": "why"},
                                   token=None)[0], 401)
        self.assertEqual(self.written(), before)

    def test_actions_off_refuse_the_stop_without_writes(self):
        scout_id = self.planning()
        before = self.written()
        self.start_host(actions=False)
        self.assertEqual(self.stop(scout_id)[0], 404)
        self.assertEqual(self.written(), before)


class HoloScoutStopTests(StopCase):
    def holo(self, *argv):
        env = dict(os.environ, HOLO_TRANSPORT="local")
        return subprocess.run([sys.executable, "-m", "holophyte.holo", *argv],
                              cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=120)

    def queued(self):
        (project,) = self.store_conn.execute(
            "SELECT id FROM projects").fetchone()
        return create_scout(self.store_conn, project, "where is the bug?",
                            "research", 30)

    def test_holo_stops_a_queued_scout_and_prints_one_json_object(self):
        scout_id = self.queued()
        ran = self.holo("scout", "stop", str(scout_id), "not needed", "--json",
                        "-p", str(self.path))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        answer = json.loads(ran.stdout)
        self.assertIs(answer["ok"], True)
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason),
                         ("stopped", "stopped by maintainer: not needed"))

    def test_holo_without_a_note_exits_2_and_writes_nothing(self):
        scout_id = self.queued()
        before = self.written()
        ran = self.holo("scout", "stop", str(scout_id), "-p", str(self.path))
        self.assertEqual(ran.returncode, 2, ran.stderr)
        self.assertEqual(self.written(), before)


class McpScoutStopTests(McpCase):
    def seed(self):
        super().seed()
        conn = store.open(str(self.store_path))
        try:
            (project,) = conn.execute("SELECT id FROM projects").fetchone()
            self.scout = create_scout(conn, project, "where is the bug?",
                                      "research", 30)
        finally:
            conn.close()

    def phase(self):
        conn = store.open(str(self.store_path))
        try:
            return scout(conn, self.scout).phase
        finally:
            conn.close()

    def test_the_scout_stop_tool_stops_the_scout_signed_via_mcp(self):
        async def use(client, _):
            return (await client.list_tools()).tools
        (tool,) = [tool for tool in self.session(use)
                   if tool.name == "scout_stop"]
        self.assertLessEqual({"project", "scout", "author"},
                             set(tool.input_schema["required"]))
        self.assertEqual(tool.input_schema["then"], {"required": ["note"]})

        client_path().write_text('default_project = "alpha"\n')
        unnamed = self.call("scout_stop", {
            "scout": self.scout, "note": "not needed", "author": "test seat"})
        self.assertIs(unnamed.is_error, True, unnamed.content)
        self.assertEqual(self.phase(), "queued")

        result = self.call("scout_stop", {
            "project": "alpha", "scout": self.scout, "note": "not needed",
            "author": "test seat"})
        self.assertIs(result.is_error, False, result.content)
        self.assertIs(result.structured_content["ok"], True)
        conn = store.open(str(self.store_path))
        try:
            row = scout(conn, self.scout)
        finally:
            conn.close()
        self.assertEqual((row.phase, row.reason),
                         ("stopped", "stopped by test seat via MCP: not needed"))

"""The scout reads: `GET /scouts`, `GET /scouts/ID` and `GET /scouts/routes`
on project and host daemons, `holo scouts`, `holo scout show` and the
`holo mcp` tools `scouts` and `scout_show`, on a store the writers seed.

Run: python3 -m unittest discover -s tests -p 'test_serve_scouts.py' -v
"""
import http.client
import json
import subprocess
import threading
from pathlib import Path
from unittest import mock

import holophyte.scout.views
import holophyte.serve.server
import store
import store.tickets
from holophyte.agents.probe_log import record_probe
from holophyte.agents.probes import PROBE_WORD, ProbeResult
from holophyte.config.project import Project
from holophyte.host.registry import Host, settings
from holophyte.scout.views import scout_detail, scout_routes, scouts
from holophyte.serve.serve_host import HostServer
from store.scout_thread import post_message, take_message
from store.scouts import (
    create_scout,
    end_scout,
    end_scout_task,
    move_scout,
    record_scout_actions,
    record_scout_event,
    record_scout_report,
    record_scout_tasks,
    start_scout_task,
)
from tests.serve_contract_fixture import NOW, normalize_contract
from tests.test_holo_mcp import McpCase

FIXTURES = Path(__file__).parent / "fixtures" / "serve"
TOKEN = "scout-read-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
ROUTES = """[scout]
lead = "A"
helper = "C"
swarm = 2
helper_turns = 6
waves = 1
budget_min = 20
[scout.leads.A]
harness = "claude"
model = "opus"
effort = "high"
[scout.leads.B]
harness = "codex"
model = "gpt-6.1-sol"
effort = "high"
[scout.helpers.C]
harness = "codex"
model = "gpt-6-luna"
effort = "low"
[scout.helpers.D]
harness = "claude"
model = "haiku"
effort = "low"
"""
UNVERIFIED = "not in the base commit aaaaaaaaaaaa"
ACTIONS = [
    {"title": "Back off between retries", "why": "the loop spins",
     "kind": "fix", "body": "Sleep between attempts in app/retry.py."},
    {"title": "Pin the retry cap", "why": "the cap is untested",
     "kind": "test", "body": "Add a test that stops at the cap."},
]
REPORT = {
    "summary": "The retry loop never backs off.",
    "findings": [
        {"title": "Retry spins without a delay", "status": "shown",
         "confidence": "high",
         "evidence": [{"file": "app/retry.py", "line": 12, "verified": True},
                      {"command": "python3 -m unittest tests.test_retry",
                       "output": "FAILED (failures=1)"}]},
        {"title": "The cap may be ignored", "status": "likely",
         "evidence": [{"file": "app/cap.py", "line": 40, "verified": False,
                       "why": UNVERIFIED},
                      {"url": "https://example.test/issues/7"}]},
    ],
    "actions": ACTIONS,
    "reproduction": {"level": "test", "hypotheses": [
        {"hypothesis": "no delay between attempts",
         "separates": "a delayed loop passes the test",
         "verdict": "supported"}]},
    "cut": ["Does the cap apply to webhooks?"],
    "scratch": "the lead's own notes",
}


def seed_scouts(conn, project):
    """Scout 1 done with a report, scout 2 swarming with two of its three
    tasks ended, scout 3 queued; scout 1's events interleave with scout 2's
    and a run's."""
    done = create_scout(conn, project, "Why does the retry spin?", "bug", 20,
                        lead_route="A", helper_route="C", swarm=2,
                        helper_turns=6, waves=2, subject=("ticket", "ALPHA-1"),
                        now=NOW - 600_000)
    move_scout(conn, done, "planning", base_sha="a" * 40, host="writer",
               now=NOW - 590_000)
    record_scout_event(conn, done, "scout_planned", "the lead planned 3 tasks",
                       now=NOW - 580_000)
    move_scout(conn, done, "swarming")
    later = record_scout_tasks(conn, done, 1, 2, [
        {"question": "Is the cap read?", "paths": ["app/cap.py"],
         "mode": "read", "route": "D"}])
    first = record_scout_tasks(conn, done, 1, 1, [
        {"question": "Where does the loop sleep?", "paths": ["app/retry.py"],
         "mode": "read", "route": "C"},
        {"question": "Does the test fail?", "paths": [], "mode": "run",
         "route": "C"}])
    for offset, task in enumerate(first + later):
        start_scout_task(conn, task, now=NOW - 500_000 + offset * 1_000)
        end_scout_task(conn, task, "done", tokens=1_000 * (offset + 1),
                       cost_usd=0.01, excerpt=f"answer {offset}",
                       now=NOW - 400_000 + offset * 2_000)
    swarming = create_scout(conn, project, "Map the merge gate", "recon", 30,
                            lead_route="B", helper_route="D", swarm=3,
                            helper_turns=3, waves=1, now=NOW - 300_000)
    move_scout(conn, swarming, "planning", base_sha="b" * 40, host="writer",
               now=NOW - 290_000)
    record_scout_event(conn, swarming, "scout_planned", "the lead planned 3",
                       now=NOW - 285_000)
    record_scout_event(conn, done, "scout_swarmed", "3 of 3 helpers answered",
                       now=NOW - 280_000)
    move_scout(conn, swarming, "swarming")
    tasks = record_scout_tasks(conn, swarming, 1, 1, [
        {"question": f"Question {n}", "paths": [], "mode": "read",
         "route": "D"} for n in range(3)])
    for task in tasks:
        start_scout_task(conn, task, now=NOW - 200_000)
    end_scout_task(conn, tasks[0], "done", tokens=500, now=NOW - 150_000)
    end_scout_task(conn, tasks[1], "failed", now=NOW - 140_000)
    move_scout(conn, done, "synthesizing")
    record_scout_report(conn, done, 1, REPORT, now=NOW - 70_000)
    record_scout_actions(conn, done, 1, ACTIONS)
    record_scout_event(conn, swarming, "scout_helper", "helper 2 failed",
                       now=NOW - 65_000)
    end_scout(conn, done, "done", "the lead wrote its report",
              now=NOW - 60_000)
    seed_thread(conn, done)
    queued = create_scout(conn, project, "Show owner initials", "research",
                          10, lead_route="A", helper_route="C", swarm=1,
                          helper_turns=6, waves=1, now=NOW - 5_000)
    return done, swarming, queued


def seed_thread(conn, scout_id):
    asked = post_message(conn, scout_id, "maintainer", "ask",
                         "Does the cap apply to webhooks?", now=NOW - 50_000)
    take_message(conn, asked, host="writer", now=NOW - 45_000)
    post_message(conn, scout_id, "scout", "answer", "Yes, through F1.",
                 blocks=[{"type": "finding", "pass": 1, "index": 1}],
                 answers_id=asked, now=NOW - 40_000)
    move_scout(conn, scout_id, "done")
    post_message(conn, scout_id, "test seat", "ask", "And the retry cap?",
                 now=NOW - 30_000)


def record_probes(project):
    for seat, code in (("scout-lead:A", 0), ("scout-helper:C", 0),
                       ("scout-lead:B", 1)):
        record_probe(project, ProbeResult(["harness"], code, PROBE_WORD, 60,
                                          seat=seat),
                     False, NOW / 1000 - 30, NOW / 1000 - 20)


def steady(body):
    """The body without the clocks a live scout's row reads from now."""
    for row in body.get("scouts") or [body["scout"]]:
        row.pop("elapsed_ms")
        row.pop("heartbeat_age_ms")
    return body


class ScoutCase(McpCase):
    def seed(self):
        super().seed()
        self.project = Project.locate(self.alpha, adopt=False)
        self.project.config_path.write_text(
            self.project.config_path.read_text() + ROUTES)
        conn = store.open(str(self.store_path))
        try:
            project = store.tickets.ensure_project(conn, "native:ALPHA",
                                                   self.alpha)
            self.done, self.swarming, self.queued = seed_scouts(conn, project)
            (run,) = conn.execute("SELECT id FROM runs").fetchone()
            store.record_event(conn, run, "run_note", "a run's event", now=NOW)
        finally:
            conn.close()
        record_probes(self.project)

    def serve(self, project, host="127.0.0.1"):
        token = holophyte.serve.server.resolve_token(project, host)
        server = holophyte.serve.server.make_server(
            project, host, 0, console_dir=self.root / "no-console",
            token=token)
        return self.listen(server)

    def listen(self, server):
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def get(self, port, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def bare(self, name):
        path = self.root / name
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        project = Project.locate(path, adopt=False)
        project.holo_dir.mkdir(parents=True)
        project.config_path.write_text(ROUTES)
        return project


class ListTests(ScoutCase):
    def test_a_limit_truncates_and_a_non_positive_limit_is_400(self):
        port = self.serve(self.project)
        code, body = self.get(port, "/scouts?limit=2")
        self.assertEqual(code, 200, body)
        self.assertEqual([row["id"] for row in body["scouts"]],
                         [self.queued, self.swarming])
        self.assertIs(body["truncated"], True)
        for bad in ("0", "-1", "two"):
            with self.subTest(limit=bad):
                code, body = self.get(port, f"/scouts?limit={bad}")
                self.assertEqual(code, 400, body)
                self.assertIn("limit", body["error"])

    def test_a_store_without_scouts_answers_an_empty_list(self):
        conn = store.open(str(self.store_path))
        try:
            for table in ("threadMessages", "scoutActions", "scoutReports",
                          "scoutTasks"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM runEvents WHERE scoutId IS NOT NULL")
            conn.execute("DELETE FROM scouts")
            conn.commit()
        finally:
            conn.close()
        code, body = scouts(self.project, "")
        self.assertEqual((code, body["scouts"]), (200, []))


class DetailTests(ScoutCase):
    def test_an_unknown_id_is_404_a_malformed_one_400_and_no_store_503(self):
        port = self.serve(self.project)
        code, body = self.get(port, "/scouts/999")
        self.assertEqual((code, body["error"]), (404, "no such scout"))
        for bad in ("abc", "0", "-1"):
            with self.subTest(id=bad):
                self.assertEqual(self.get(port, f"/scouts/{bad}")[0], 400)
        empty = self.serve(self.bare("beta"))
        code, body = self.get(empty, "/scouts")
        self.assertEqual((code, body["error"]), (503, "no store"))

    def test_a_scout_that_starts_mid_read_is_served_from_one_moment(self):
        read_row = holophyte.scout.views.scout

        def row_then_start(conn, scout_id):
            row = read_row(conn, scout_id)
            writer = store.open(str(self.store_path))
            try:
                move_scout(writer, scout_id, "planning", base_sha="c" * 40,
                           host="writer", now=NOW)
            finally:
                writer.close()
            return row

        with mock.patch.object(holophyte.scout.views, "scout",
                               row_then_start):
            code, body = scout_detail(self.project, str(self.queued), now=NOW)
        self.assertEqual(code, 200, body)
        self.assertEqual((body["scout"]["phase"], body["scout"]["started_ms"],
                          body["scout"]["base_sha"]), ("queued", None, None))


class RoutesTests(ScoutCase):
    def test_a_project_with_no_store_still_answers_its_routes(self):
        project = self.bare("beta")
        port = self.serve(project)
        code, body = self.get(port, "/scouts/routes")
        self.assertEqual(code, 200, body)
        self.assertEqual([route["name"] for route in body["leads"]], ["A", "B"])
        self.assertFalse(project.store_path.exists())


class GuardTests(ScoutCase):
    def test_each_read_needs_the_token_beyond_loopback(self):
        token = self.root / "serve.token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        self.project.config_path.write_text(
            self.project.config_path.read_text().replace(
                "[serve]\n", f'[serve]\ntoken_file = "{token}"\n'))
        port = self.serve(Project.locate(self.alpha, adopt=False),
                          host="0.0.0.0")
        for path in ("/scouts", f"/scouts/{self.done}", "/scouts/routes"):
            with self.subTest(path=path):
                self.assertEqual(self.get(port, path)[0], 401)
                self.assertEqual(self.get(port, path, BEARER)[0], 200)

    def test_a_host_daemon_answers_a_projects_scouts_under_its_prefix(self):
        host = Host.locate()
        port = self.listen(HostServer(
            host, settings(host), ("127.0.0.1", 0),
            console_dir=self.root / "no-console", read_token=None,
            write_token=None))
        code, body = self.get(port, "/projects/alpha/scouts")
        self.assertEqual(code, 200, body)
        self.assertEqual([row["id"] for row in body["scouts"]],
                         [self.queued, self.swarming, self.done])


class ContractTests(ScoutCase):
    def test_the_seeded_answers_equal_their_pinned_fixtures(self):
        self.maxDiff = None
        answers = {"scouts": scouts(self.project, "", now=NOW),
                   "scout": scout_detail(self.project, str(self.done), now=NOW),
                   "scout-routes": scout_routes(self.project, now=NOW)}
        for name, (code, body) in answers.items():
            with self.subTest(answer=name):
                self.assertEqual(code, 200, body)
                pinned = json.loads((FIXTURES / f"{name}.json").read_text())
                self.assertEqual(normalize_contract(body), pinned)


class HoloTests(ScoutCase):
    def test_holo_scouts_and_scout_show_print_the_routes_bodies(self):
        port = self.serve(self.project)
        for argv, path in ((["scouts"], "/scouts"),
                           (["scout", "show", str(self.done)],
                            f"/scouts/{self.done}")):
            with self.subTest(argv=argv):
                printed = self.holo(*argv, "--json", "-p", str(self.alpha),
                                    HOLO_TRANSPORT="local")
                self.assertEqual(printed.returncode, 0, printed.stderr)
                code, body = self.get(port, path)
                self.assertEqual(code, 200, body)
                self.assertEqual(steady(json.loads(printed.stdout)),
                                 steady(body))

    def test_holo_scouts_prints_a_line_per_scout_and_an_unknown_id_exits_1(self):
        path = str(self.alpha)
        listed = self.holo("scouts", "-p", path, HOLO_TRANSPORT="local")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.splitlines(), [
            f"scout {self.queued}  research  queued  Show owner initials",
            f"scout {self.swarming}  recon  swarming  Map the merge gate",
            f"scout {self.done}  bug  done  Why does the retry spin?"])
        missing = self.holo("scout", "show", "999", "-p", path,
                            HOLO_TRANSPORT="local")
        self.assertEqual(missing.returncode, 1, missing.stderr)
        self.assertIn("no such scout", missing.stderr)


class McpTests(ScoutCase):
    def test_the_scouts_tool_answers_get_scouts_within_its_limit(self):
        result = self.call("scouts", {"project": "alpha", "limit": 2})
        self.assertIs(result.is_error, False, result.content)
        self.assertEqual([row["id"] for row in
                          result.structured_content["scouts"]],
                         [self.queued, self.swarming])
        self.assertIs(result.structured_content["truncated"], True)

    def test_the_scout_show_tool_is_a_listed_read_answering_get_scouts_id(self):
        async def use(client, _):
            return (await client.list_tools()).tools
        tools = {tool.name: tool for tool in self.session(use)}
        self.assertEqual(
            {name: tools[name].annotations.read_only_hint
             for name in ("scouts", "scout_show", "scout")},
            {"scouts": True, "scout_show": True, "scout": False})
        self.assertEqual(tools["scout_show"].input_schema["required"],
                         ["scout"])
        result = self.call("scout_show", {"project": "alpha",
                                          "scout": self.done})
        self.assertIs(result.is_error, False, result.content)
        code, body = self.get(self.serve(self.project), f"/scouts/{self.done}")
        self.assertEqual(code, 200, body)
        self.assertEqual(steady(result.structured_content), steady(body))

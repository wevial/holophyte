"""A scout's read-only GitHub tools: `github_get` and `github_tests` read the
scout's own repository through a fake `gh` first on `PATH`, refuse anything
but an allow-listed GET, record a `scout_github` event per call, serve over
stdio to the MCP SDK's own client, and reach the lead and read helper turns
of `factory.py --scout ID PROJECT` when `[scout] github = true`.

Run: python3 -m unittest discover -s tests -p 'test_scout_github.py' -v
"""
import http.client
import io
import json
import os
import stat
import sys
import unittest
import zipfile
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from holophyte.config.project import Project
from holophyte.scout.github_mcp import NO_ORIGIN, called, scout_server
from holophyte.scout.github_reads import (
    GITHUB_ARTIFACT_BYTES,
    github_get,
    github_tests,
    origin_repo,
)
from holophyte.scout.scout_config import scout_config
from store.scouts import create_scout, end_scout, move_scout, scout
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_config import ScoutFixture as ConfigFixture
from tests.test_scout_launch import TOKEN, LaunchCase
from tests.test_scout_lead import CLAUDE_LEAD, REPORT
from tests.test_scout_run_helpers import plan

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "https://github.com/example/repo.git"
RUNS = "repos/example/repo/actions/runs?branch=main&per_page=2"
ARTIFACTS = "repos/example/repo/actions/runs/7/artifacts?per_page=100"
GH_TOKEN = "ghp-scout-github-0123456789abcdef"
READ_Q, RUN_Q = "Where is the retry loop?", "How long does the suite take?"
GITHUB_LEAD = CLAUDE_LEAD.replace("[scout]\n", "[scout]\ngithub = true\n", 1)
FAKE_GH = """#!{python}
import json, sys
from pathlib import Path
here = Path({here!r})
with open(here / "gh.jsonl", "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
answers = json.loads((here / "gh-answers.json").read_text())
answer = answers.get(sys.argv[-1])
if answer is None or answer.get("fail"):
    sys.stderr.write((answer or {{}}).get("fail", "gh: Not Found (HTTP 404)"))
    sys.exit(1)
sys.stdout.buffer.write((here / answer["file"]).read_bytes())
"""
RUNS_ANSWER = {
    "total_count": 1, "node_id": "WR_top", "url": "https://api/top",
    "jobs_url": "https://api/top/jobs", "html_url": "https://github.com/top",
    "workflow_runs": [{
        "id": 7, "name": "unit", "node_id": "WR_7", "url": "https://api/7",
        "jobs_url": "https://api/7/jobs", "html_url": "https://github.com/7",
        "conclusion": "failure"}]}
RUNS_TRIMMED = {
    "total_count": 1, "html_url": "https://github.com/top",
    "workflow_runs": [{"id": 7, "name": "unit",
                       "html_url": "https://github.com/7",
                       "conclusion": "failure"}]}
SPEC = "e2e/checkout.spec.ts"
PASSED_MS, FIRST_TRY_MS, RETRY_MS = 1200, 900, 1100
JUNIT_SECONDS, JUNIT_FAILED_SECONDS = "0.25", "1.5"


def spec(title, status, results):
    return {"title": title, "file": SPEC, "tests": [{
        "status": status, "projectName": "chromium",
        "results": [{"status": result, "duration": duration, "retry": number}
                    for number, (result, duration) in enumerate(results)]}]}


PLAYWRIGHT = {"config": {}, "errors": [], "suites": [{
    "title": SPEC, "file": SPEC, "specs": [], "suites": [{
        "title": "checkout", "file": SPEC, "specs": [
            spec("loads", "expected", [("passed", PASSED_MS)]),
            spec("pays", "flaky", [("failed", FIRST_TRY_MS),
                                   ("passed", RETRY_MS)]),
            spec("refunds", "skipped", [("skipped", 0)])]}]}]}
JUNIT = f"""<?xml version="1.0"?>
<testsuites><testsuite name="api">
<testcase classname="tests.test_api" name="test_lists" time="{JUNIT_SECONDS}"/>
<testcase classname="tests.test_api" file="tests/test_api.py"
  name="test_posts" time="{JUNIT_FAILED_SECONDS}"><failure message="no"/></testcase>
</testsuite></testsuites>
"""


def zipped(members):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return data.getvalue()


class FakeGh:
    """A `gh` first on `PATH` that logs each argv and answers its last
    argument, a REST path, from a fixture file, or fails as told."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.answers = {}
        script = self.directory / "gh"
        script.write_text(FAKE_GH.format(python=sys.executable,
                                         here=str(self.directory)))
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.save()
        os.environ["PATH"] = f"{self.directory}{os.pathsep}{os.environ['PATH']}"

    def save(self):
        (self.directory / "gh-answers.json").write_text(
            json.dumps(self.answers))

    def answer(self, path, data):
        name = f"answer-{len(self.answers)}"
        (self.directory / name).write_bytes(
            data if isinstance(data, bytes) else data.encode())
        self.answers[path] = {"file": name}
        self.save()

    def fail(self, path, message):
        self.answers[path] = {"fail": message}
        self.save()

    def calls(self):
        log = self.directory / "gh.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


class GithubCase(LaunchCase):
    """A registered project whose `origin` is a GitHub URL, with a fake
    `gh` first on `PATH`."""

    def setUp(self):
        super().setUp()
        self.gh = FakeGh(self.root / "fake-gh")

    def github_project(self, name="alpha", config="", origin=ORIGIN):
        path = self.project(name, config)
        if origin is not None:
            git(path, "remote", "add", "origin", origin)
        return path

    def target(self, path):
        return Project.locate(path, adopt=False)


class GetTests(GithubCase):
    def setUp(self):
        super().setUp()
        self.path = self.github_project()
        self.repo = origin_repo(self.target(self.path))

    def test_an_allowed_path_answers_its_json_less_api_links_at_every_depth(self):
        self.gh.answer(RUNS, json.dumps(RUNS_ANSWER))
        answer = github_get(self.target(self.path), self.repo, RUNS)
        self.assertFalse(answer.error, answer.text)
        self.assertEqual(json.loads(answer.text), RUNS_TRIMMED)
        self.assertEqual(self.gh.calls(), [
            ["api", "--hostname", "github.com", "--method", "GET", RUNS]])

    def test_each_refused_request_names_its_rule_and_reaches_no_gh(self):
        refused = (
            ("POST", "repos/example/repo/actions/runs", "only GET"),
            ("GET", "repos/other/repo/actions/runs",
             "must be under repos/example/repo/"),
            ("GET", "repos/example/repo/actions/runs/1/rerun",
             "not on the allow-list"),
            ("GET", "graphql", "graphql is not allowed"),
            ("GET", "repos/example/repo/../other/pulls", "'..'"),
            ("GET", "https://api.github.com/repos/example/repo/pulls",
             "scheme or a host"),
            ("GET", "//api.github.com/repos/example/repo/pulls",
             "scheme or a host"),
            ("GET", "repos/example/repo/pulls?state=open\nX: y",
             "control character"),
            ("GET", "repos/example/repo/commits/main#/status", "fragment"))
        for method, path, rule in refused:
            with self.subTest(method=method, path=path):
                answer = github_get(self.target(self.path), self.repo, path,
                                    method)
                self.assertTrue(answer.error)
                self.assertEqual(answer.status, "refused")
                self.assertIn(rule, answer.text)
        self.assertEqual(self.gh.calls(), [])

    def test_a_commit_ref_holding_a_slash_is_read_plain_or_encoded(self):
        reads = ("repos/example/repo/commits/feature/foo/status",
                 "repos/example/repo/commits/heads%2Fmain/check-runs")
        for path in reads:
            self.gh.answer(path, json.dumps({"state": "success"}))
        for path in reads:
            with self.subTest(path=path):
                answer = github_get(self.target(self.path), self.repo, path)
                self.assertFalse(answer.error, answer.text)
                self.assertEqual(json.loads(answer.text), {"state": "success"})
        self.assertEqual([call[-1] for call in self.gh.calls()], list(reads))
        for path in ("repos/example%2Frepo/commits/main/status",
                     "repos/example/repo/commits/a%5Cb/status"):
            with self.subTest(path=path):
                self.assertIn("encoded slash", github_get(
                    self.target(self.path), self.repo, path).text)

    def test_an_answer_past_the_cap_is_cut_with_a_truncation_line(self):
        long = {"workflow_runs": [{"id": number, "name": "x" * 80,
                                   "jobs_url": "https://api/jobs"}
                                  for number in range(900)]}
        raw = json.dumps(long)
        self.assertGreater(len(raw), 90000)
        self.gh.answer(RUNS, raw)
        answer = github_get(self.target(self.path), self.repo, RUNS)
        text = json.dumps({"workflow_runs": [
            {"id": number, "name": "x" * 80} for number in range(900)]})
        self.assertGreater(len(text), 40000)
        self.assertEqual(answer.text, text[:40000] + "\n[holo2] truncated: "
                         f"40000 of {len(text)} characters shown")


class TestsTests(GithubCase):
    def test_a_run_s_report_artifacts_answer_rows_slowest_first(self):
        path = self.github_project()
        self.gh.answer(ARTIFACTS, json.dumps({"artifacts": [
            {"id": 11, "name": "playwright-report", "size_in_bytes": 900,
             "expired": False},
            {"id": 12, "name": "junit", "size_in_bytes": 600,
             "expired": False},
            {"id": 13, "name": "old", "size_in_bytes": 600,
             "expired": True}]}))
        self.gh.answer("repos/example/repo/actions/artifacts/11/zip",
                       zipped({"report.json": json.dumps(PLAYWRIGHT),
                               "index.html": "<html></html>"}))
        self.gh.answer("repos/example/repo/actions/artifacts/12/zip",
                       zipped({"junit/results.xml": JUNIT}))
        target = self.target(path)
        answer = github_tests(target, origin_repo(target), 7)
        self.assertFalse(answer.error, answer.text)
        result = json.loads(answer.text)
        rows = [
            {"title": "test_posts", "file": "tests/test_api.py",
             "duration_ms": int(float(JUNIT_FAILED_SECONDS) * 1000),
             "status": "failed", "retries": 0},
            {"title": "checkout › loads", "file": SPEC,
             "duration_ms": PASSED_MS, "status": "passed", "retries": 0},
            {"title": "checkout › pays", "file": SPEC,
             "duration_ms": RETRY_MS, "status": "flaky", "retries": 1},
            {"title": "test_lists", "file": "tests.test_api",
             "duration_ms": int(float(JUNIT_SECONDS) * 1000),
             "status": "passed", "retries": 0},
            {"title": "checkout › refunds", "file": SPEC, "duration_ms": 0,
             "status": "skipped", "retries": 0}]
        self.assertEqual(result, {"artifacts": ["playwright-report", "junit"],
                                  "total": 5, "failed": 1, "flaky": 1,
                                  "rows": rows})
        self.assertNotIn("repos/example/repo/actions/artifacts/13/zip",
                         [call[-1] for call in self.gh.calls()])

    def test_an_artifact_on_a_later_page_of_the_listing_is_read(self):
        path = self.github_project()
        self.gh.answer(ARTIFACTS, json.dumps({"total_count": 101, "artifacts": [
            {"id": 100 + number, "name": f"trace-{number}",
             "size_in_bytes": 600, "expired": True}
            for number in range(100)]}))
        self.gh.answer(ARTIFACTS + "&page=2", json.dumps({
            "total_count": 101, "artifacts": [
                {"id": 12, "name": "junit", "size_in_bytes": 600,
                 "expired": False}]}))
        self.gh.answer("repos/example/repo/actions/artifacts/12/zip",
                       zipped({"junit/results.xml": JUNIT}))
        target = self.target(path)
        answer = github_tests(target, origin_repo(target), 7, "junit")
        self.assertFalse(answer.error, answer.text)
        result = json.loads(answer.text)
        self.assertEqual((result["artifacts"], result["total"],
                          result["failed"]), (["junit"], 2, 1))


    def test_a_report_member_larger_than_the_artifact_cap_is_still_read(self):
        path = self.github_project()
        padded = JUNIT.replace("</testsuites>", " " * GITHUB_ARTIFACT_BYTES
                               + "</testsuites>")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as writer:
            writer.writestr("junit/results.xml", padded)
        self.assertLess(len(archive.getvalue()), GITHUB_ARTIFACT_BYTES)
        self.gh.answer(ARTIFACTS, json.dumps({"artifacts": [
            {"id": 12, "name": "junit",
             "size_in_bytes": len(archive.getvalue()), "expired": False}]}))
        self.gh.answer("repos/example/repo/actions/artifacts/12/zip",
                       archive.getvalue())
        target = self.target(path)
        answer = github_tests(target, origin_repo(target), 7)
        self.assertFalse(answer.error, answer.text)
        result = json.loads(answer.text)
        self.assertEqual((result["total"], result["failed"]), (2, 1))


class EventTests(GithubCase):
    def get(self, server, route):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=30)
        try:
            connection.request("GET", route,
                               headers={"Authorization": f"Bearer {TOKEN}"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def live_scout(self, path):
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "why is CI slow?", "recon", 30)
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        return conn, scout_id

    def test_each_call_records_its_status_and_the_daemon_serves_them(self):
        path = self.github_project()
        conn, scout_id = self.live_scout(path)
        self.gh.answer(RUNS, json.dumps(RUNS_ANSWER))
        failing = "repos/example/repo/pulls/5"
        self.gh.fail(failing, "gh: Server Error (HTTP 502)")
        target = self.target(path)
        answers = [called(target, scout_id, "github_get", {"path": each})
                   for each in (RUNS, "graphql", failing)]
        events = conn.execute(
            "SELECT summary, payload FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_github' AND level = 'detail' ORDER BY seq",
            (scout_id,)).fetchall()
        self.assertEqual([json.loads(payload) for _, payload in events], [
            {"tool": "github_get", "path": each, "status": status,
             "bytes": len(answer.text.encode())}
            for each, status, answer in zip((RUNS, "graphql", failing),
                                            ("ok", "refused", "failed"),
                                            answers)])
        server = self.serve(path)
        code, detail = self.get(server, f"/scouts/{scout_id}")
        self.assertEqual(code, 200, detail)
        served = [event["summary"] for event in detail["events"]
                  if event["kind"] == "scout_github"]
        self.assertEqual(served, [summary for summary, _ in events])

    def test_the_missing_origin_notice_is_recorded_once_per_scout(self):
        path = self.github_project(config="[scout]\ngithub = true\n",
                                   origin=None)
        conn, scout_id = self.live_scout(path)
        target = self.target(path)
        for _ in range(2):
            self.assertIsNone(scout_server(conn, target,
                                           scout(conn, scout_id)))
        self.assertEqual(conn.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_github'", (scout_id,)).fetchall(),
            [(NO_ORIGIN,)])

    def test_a_call_after_the_scout_ended_answers_and_records_nothing(self):
        path = self.github_project()
        conn, scout_id = self.live_scout(path)
        end_scout(conn, scout_id, "done", "the lead wrote its report")
        self.gh.answer(RUNS, json.dumps(RUNS_ANSWER))
        answer = called(self.target(path), scout_id, "github_get",
                        {"path": RUNS})
        self.assertEqual(json.loads(answer.text), RUNS_TRIMMED)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_github'", (scout_id,)).fetchone(), (0,))


class ServerTests(GithubCase):
    def test_the_stdio_server_lists_two_read_only_tools_and_answers_a_get(self):
        path = self.github_project()
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "why is CI slow?", "recon", 30)
        self.gh.answer(RUNS, json.dumps(RUNS_ANSWER))
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "holophyte.scout.github_mcp", "--project", str(path),
                  "--scout", str(scout_id)], env=env, cwd=str(self.root))

        async def run():
            with anyio.fail_after(60):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        tools = (await client.list_tools()).tools
                        result = await client.call_tool("github_get",
                                                        {"path": RUNS})
                        return tools, result
        tools, result = anyio.run(run)
        self.assertEqual(sorted(tool.name for tool in tools),
                         ["github_get", "github_tests"])
        for tool in tools:
            self.assertIs(tool.annotations.read_only_hint, True)
        self.assertIs(result.is_error, False, result.content)
        self.assertEqual(json.loads(result.content[0].text), RUNS_TRIMMED)
        self.assertEqual([call[-1] for call in self.gh.calls()], [RUNS])


def mcp_entries(argv):
    return [arg for arg in argv if arg.startswith("mcp_servers.holo_github.")]


class TurnTests(GithubCase):
    def run_scout(self, config, origin=ORIGIN):
        os.environ["GH_TOKEN"] = GH_TOKEN
        path = self.github_project("alpha", config, origin)
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "why is CI slow?", "recon", 30,
                                helper_route="luna")
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        self.stubs.answer(plan=plan((READ_Q, "read"), (RUN_Q, "run")),
                          synthesis=fenced({"tasks": []}),
                          report=fenced(REPORT))
        code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 0, printed)
        return conn, scout_id

    def turns(self):
        calls = [call for call in self.stubs.calls() if call["turn"] != "probe"]
        helpers = {call["question"]: call["argv"]
                   for call in calls if call["turn"] == "helper"}
        return calls, helpers

    def test_lead_and_read_helper_turns_carry_the_server_and_no_token(self):
        _, scout_id = self.run_scout(GITHUB_LEAD)
        path = str(self.root / "alpha")
        calls, helpers = self.turns()
        leads = {call["turn"]: call["argv"] for call in calls
                 if call["turn"] in ("plan", "synthesis", "report")}
        self.assertEqual(set(leads), {"plan", "synthesis", "report"})
        for turn, argv in leads.items():
            with self.subTest(turn=turn):
                config = json.loads(argv[argv.index("--mcp-config") + 1])
                entry = config["mcpServers"]["holo_github"]
                self.assertEqual(entry["command"], sys.executable)
                self.assertEqual(entry["args"], [
                    "-m", "holophyte.scout.github_mcp", "--project", path,
                    "--scout", str(scout_id)])
                self.assertEqual(entry["env"]["PYTHONPATH"], str(ROOT))
                at = argv.index("--allowedTools")
                self.assertEqual(argv[at + 1], "mcp__holo_github")
                self.assertTrue(argv[at + 2].startswith("--"))
        entries = mcp_entries(helpers[READ_Q])
        self.assertTrue(entries, helpers[READ_Q])
        (args,) = [entry for entry in entries if ".args=" in entry]
        self.assertEqual(json.loads(args.split("=", 1)[1]), [
            "-m", "holophyte.scout.github_mcp", "--project", path,
            "--scout", str(scout_id)])
        self.assertIn(f"mcp_servers.holo_github.command="
                      f"{json.dumps(sys.executable)}", entries)
        self.assertIn("github_tests", helpers[READ_Q][-1])
        self.assertNotIn("github_tests", helpers[RUN_Q][-1])
        self.assertNotIn("--mcp-config", helpers[RUN_Q])
        self.assertEqual(mcp_entries(helpers[RUN_Q]), [])
        for call in calls:
            for arg in call["argv"]:
                self.assertNotIn(GH_TOKEN, arg)
        goal = leads["plan"][-1]
        self.assertIn("github_get", goal)
        self.assertIn("github_tests", goal)

    def assert_no_server(self):
        calls, _ = self.turns()
        self.assertTrue(calls)
        for call in calls:
            self.assertNotIn("--mcp-config", call["argv"])
            self.assertFalse([arg for arg in call["argv"]
                              if "mcp_servers" in arg], call["argv"])

    def test_without_the_key_no_turn_carries_the_server(self):
        self.run_scout(CLAUDE_LEAD)
        self.assert_no_server()

    def test_with_no_github_origin_no_turn_carries_it_and_one_event_says_so(self):
        conn, scout_id = self.run_scout(GITHUB_LEAD, origin=None)
        self.assert_no_server()
        self.assertEqual(conn.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_github'", (scout_id,)).fetchall(),
            [(NO_ORIGIN,)])


class ConfigTests(ConfigFixture):
    def test_github_is_a_boolean_and_anything_else_names_the_key(self):
        with self.assertRaises(SystemExit) as refused:
            scout_config(self.project('[scout]\ngithub = "yes"\n'))
        self.assertIn("[scout] github", str(refused.exception))
        self.assertIs(scout_config(self.project("[scout]\ngithub = true\n"))
                      .github, True)


if __name__ == "__main__":
    unittest.main()

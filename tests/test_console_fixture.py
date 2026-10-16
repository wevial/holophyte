"""`python3 -m tests.console_fixture` seeds a scratch home with a
native `demo` project, serves it with the real host daemon on an ephemeral
loopback port, runs a command against it and tears it all down.

Each test runs the wrapper as a subprocess from the repository root, its
command a Python script that talks to the daemon and records what it saw.

Run: python3 -m unittest discover -s tests -p 'test_console_fixture.py' -v
"""
import contextlib
import io
import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from holophyte.host.registry import Host, settings
from tests import console_fixture

REPO = Path(__file__).resolve().parent.parent

READ = """\
import json, os, sys, urllib.request
url = os.environ["CONSOLE_URL"]
seen = {}
for path in ("/projects/demo/board", "/projects/demo/shipped?outcome=all",
             "/projects/demo/attention", "/projects/demo/status"):
    with urllib.request.urlopen(url + path, timeout=10) as answer:
        seen[path] = json.load(answer)
for run in seen["/projects/demo/status"]["runs"]:
    path = "/projects/demo/runs/%d" % run["id"]
    with urllib.request.urlopen(url + path, timeout=10) as answer:
        seen[run["ticket"]] = json.load(answer)
open(sys.argv[1], "w").write(json.dumps(seen))
"""

FILE = """\
import json, os, sys, urllib.error, urllib.request
url = os.environ["CONSOLE_URL"] + "/projects/demo/tickets"
data = json.dumps({"body": "# Draft", "column": "backlog"}).encode()
seen = []
for headers in ({}, {"Authorization": "Bearer " + os.environ["CONSOLE_TOKEN"]}):
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            seen.append([answer.status, json.load(answer)])
    except urllib.error.HTTPError as refused:
        seen.append([refused.code, None])
open(sys.argv[1], "w").write(json.dumps(seen))
"""

POLLED = """\
import json, os, sys, urllib.error, urllib.request
url = os.environ["CONSOLE_URL"]
seen = {}
for path in ("/status", "/attention", "/projects", "/projects/demo/status",
             "/projects/demo/board", "/projects/demo/attention",
             "/projects/demo/shipped?outcome=all"):
    try:
        with urllib.request.urlopen(url + path, timeout=10) as answer:
            seen[path] = answer.read().decode()
    except urllib.error.HTTPError as refused:
        seen[path] = refused.read().decode()
open(sys.argv[1], "w").write(json.dumps(seen))
"""

STORY = """\
import json, os, sys, urllib.request
url = os.environ["CONSOLE_URL"] + "/projects/demo"
seen = {}
for path in ("/stories/DEMO-12", "/attention", "/status"):
    with urllib.request.urlopen(url + path, timeout=10) as answer:
        seen[path] = json.load(answer)
open(sys.argv[1], "w").write(json.dumps(seen))
"""

FINDINGS_READ = """\
import json, os, sys, urllib.request
url = os.environ["CONSOLE_URL"] + "/projects/demo/findings?since=0"
with urllib.request.urlopen(url, timeout=10) as answer:
    open(sys.argv[1], "w").write(answer.read().decode())
"""

RECORD = """\
import json, os, sys
open(sys.argv[1], "w").write(json.dumps(
    [os.environ["CONSOLE_URL"], os.environ["HOLOPHYTE_HOME"]]))
sys.exit(3)
"""


class ConsoleFixtureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.out = self.root / "seen.json"

    def wrap(self, *args):
        env = {**os.environ, "HOLOPHYTE_HOME": str(self.home)}
        return subprocess.run(
            [sys.executable, "-m", "tests.console_fixture", *args], cwd=REPO,
            env=env, capture_output=True, text=True, timeout=120)

    def script(self, text):
        result = self.wrap(sys.executable, "-c", text, str(self.out))
        return result, (json.loads(self.out.read_text())
                        if self.out.exists() else None)

    def test_the_command_reads_a_native_project_in_every_state(self):
        result, seen = self.script(READ)

        self.assertEqual(result.returncode, 0, result.stderr)
        board = seen["/projects/demo/board"]
        self.assertIs(board["editable"], True)
        columns = {column["state"]: {ticket["ticket"]: ticket
                                     for ticket in column["tickets"]}
                   for column in board["columns"]}
        self.assertEqual(list(columns["backlog"]), ["DEMO-1", "DEMO-12"])
        self.assertEqual(list(columns["ready"]), ["DEMO-2"])
        self.assertEqual(sorted(columns["in_flight"]),
                         ["DEMO-11", "DEMO-3", "DEMO-7"])
        self.assertIsNotNone(columns["in_flight"]["DEMO-3"]["run"])
        self.assertEqual(sorted(columns["blocked_on_operator"]),
                         ["DEMO-4", "DEMO-8", "DEMO-9"])
        shipped = {row["ticket"]: row["outcome"] for row
                   in seen["/projects/demo/shipped?outcome=all"]["rows"]}
        self.assertEqual(shipped, {"DEMO-5": "merged", "DEMO-6": "failed",
                                   "DEMO-10": "merged", "DEMO-11": "failed"})
        blocked = [item["ticket"] for item
                   in seen["/projects/demo/attention"]["items"]
                   if item["kind"] == "blocked"]
        self.assertEqual(blocked, ["DEMO-4"])

    def test_the_overview_states_are_served_as_the_daemon_reads_them(self):
        midnight = datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000

        result, seen = self.script(READ)

        self.assertEqual(result.returncode, 0, result.stderr)
        status = seen["/projects/demo/status"]
        stale_ms = status["thresholds"]["heartbeat_stale_ms"]
        runs = {run["ticket"]: run for run in status["runs"]}
        self.assertEqual({ticket: run["phase"] for ticket, run in runs.items()},
                         {"DEMO-3": "working", "DEMO-7": "reviewing"})
        for run in runs.values():
            self.assertLess(run["heartbeat_age_ms"], stale_ms, run["ticket"])
        (review,) = seen["DEMO-7"]["rounds"]
        self.assertEqual(review["verdict"], "changes_requested")
        self.assertEqual([f["severity"] for f in review["findings"]], ["p1"])

        items = seen["/projects/demo/attention"]["items"]
        self.assertNotIn("stale_run", [item["kind"] for item in items])
        parks = {item["ticket"]: item for item in items
                 if item["kind"] == "pr_open"}
        self.assertEqual(sorted(parks), ["DEMO-8", "DEMO-9"])
        self.assertEqual((parks["DEMO-8"]["park_kind"],
                          parks["DEMO-8"]["pr"]["checks"]), ("ci", "pending"))
        self.assertEqual(
            (parks["DEMO-9"]["park_kind"], parks["DEMO-9"]["pr"]["checks"],
             parks["DEMO-9"]["pr"]["review"],
             parks["DEMO-9"]["pr"]["open_threads"]),
            ("pull_request", "success", "approved", 0))
        failed = [(item["ticket"], item["reason"]) for item in items
                  if item["kind"] == "failed"]
        self.assertEqual(failed, [("DEMO-11", "verify failed: 2 tests red")])

        rows = {row["ticket"]: row for row
                in seen["/projects/demo/shipped?outcome=all"]["rows"]}
        today = rows["DEMO-10"]
        self.assertEqual(today["outcome"], "merged")
        self.assertEqual(today["pr_url"],
                         "https://github.com/example/demo/pull/43")
        self.assertGreaterEqual(today["ended_ms"], midnight)
        self.assertLessEqual(today["ended_ms"], time.time() * 1000)

    def test_the_story_spans_the_seeded_states_and_leaves_the_overview(self):
        result, seen = self.script(STORY)

        self.assertEqual(result.returncode, 0, result.stderr)
        children = {child["ticket"]: child
                    for child in seen["/stories/DEMO-12"]["children"]}
        self.assertEqual(sorted(children), ["DEMO-1", "DEMO-10", "DEMO-11",
                                            "DEMO-2", "DEMO-5", "DEMO-7"])
        self.assertEqual(children["DEMO-1"]["depends_on"],
                         ["DEMO-7", "DEMO-11"])
        live = {run["ticket"]: run["id"]
                for run in seen["/status"]["runs"]}["DEMO-7"]
        self.assertEqual(children["DEMO-7"]["run"], live)
        self.assertEqual(
            [(item["kind"], item["ticket"])
             for item in seen["/attention"]["items"]],
            [("blocked", "DEMO-4"), ("pr_open", "DEMO-8"),
             ("pr_open", "DEMO-9"), ("failed", "DEMO-11")])
        self.assertEqual(seen["/status"]["queued"], [])

    def test_the_findings_route_serves_a_pending_feature_and_a_kept_guardrail(self):
        result, seen = self.script(FINDINGS_READ)

        self.assertEqual(result.returncode, 0, result.stderr)
        rows = sorted((row["ticket"], row["kind"], row["state"], row["path"],
                       row["line"]) for row in seen["follow_ups"])
        self.assertEqual(rows, [
            ("DEMO-5", "guardrail", "ledger", "holophyte/serve/server.py", 12),
            ("DEMO-7", "feature", "pending",
             "holophyte/cli/shadow_report.py", 88)])

    def test_no_route_the_console_polls_names_the_machine(self):
        result, seen = self.script(POLLED)

        self.assertEqual(result.returncode, 0, result.stderr)
        for path, text in seen.items():
            self.assertNotIn(socket.gethostname(), text, path)
        (demo,) = json.loads(seen["/status"])["projects"]
        self.assertEqual(demo["host"], "writer-host")

    def test_a_filing_needs_the_machine_token_the_command_is_given(self):
        result, seen = self.script(FILE)

        self.assertEqual(result.returncode, 0, result.stderr)
        (bare, _), (status, answer) = seen
        self.assertEqual(bare, 401)
        self.assertEqual(status, 201)
        self.assertEqual(answer["ticket"], "DEMO-13")

    def test_the_command_s_code_is_kept_and_everything_torn_down(self):
        result, (url, home) = self.script(RECORD)

        self.assertEqual(result.returncode, 3, result.stderr)
        address = urlsplit(url)
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection((address.hostname, address.port), 5)
        self.assertNotEqual(Path(home), self.home)
        self.assertFalse(Path(home).exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_a_failed_build_stops_before_the_command(self):
        build = shlex.join([sys.executable, "-c", "raise SystemExit(1)"])
        marker = self.root / "ran"

        result = self.wrap("--build", build, sys.executable, "-c",
                           f"open({str(marker)!r}, 'w')")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(build, result.stderr)
        self.assertFalse(marker.exists())

    def test_a_relative_home_seeds_a_token_the_daemon_can_read(self):
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.root)

        token = console_fixture.seed(Path("scratch"))

        knobs = settings(Host.locate(Path("scratch")))
        self.assertEqual(knobs.machine_token_file.read_text().strip(), token)

    def test_a_daemon_that_cannot_spawn_is_named_and_the_command_not_run(self):
        marker = self.root / "ran"
        stderr = io.StringIO()
        spawn = OSError(11, "Resource temporarily unavailable")

        with patch.dict(os.environ), \
                patch.object(console_fixture, "seed", return_value="t"), \
                patch.object(console_fixture, "start_daemon",
                             side_effect=spawn), \
                contextlib.redirect_stderr(stderr):
            code = console_fixture.main(
                [sys.executable, "-c", f"open({str(marker)!r}, 'w')"])

        self.assertEqual(code, 1)
        self.assertIn("host daemon did not start", stderr.getvalue())
        self.assertIn("Resource temporarily unavailable", stderr.getvalue())
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

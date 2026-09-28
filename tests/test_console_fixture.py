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
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from holophyte.host import Host, settings
from tests import console_fixture

REPO = Path(__file__).resolve().parent.parent

READ = """\
import json, os, sys, urllib.request
url = os.environ["CONSOLE_URL"]
seen = {}
for path in ("/projects/demo/board", "/projects/demo/shipped?outcome=all",
             "/projects/demo/attention"):
    with urllib.request.urlopen(url + path, timeout=10) as answer:
        seen[path] = json.load(answer)
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
        self.assertEqual(list(columns["backlog"]), ["DEMO-1"])
        self.assertEqual(list(columns["ready"]), ["DEMO-2"])
        self.assertEqual(list(columns["in_flight"]), ["DEMO-3"])
        self.assertIsNotNone(columns["in_flight"]["DEMO-3"]["run"])
        self.assertEqual(list(columns["blocked_on_operator"]), ["DEMO-4"])
        shipped = {row["ticket"]: row["outcome"] for row
                   in seen["/projects/demo/shipped?outcome=all"]["rows"]}
        self.assertEqual(shipped, {"DEMO-5": "merged", "DEMO-6": "failed"})
        blocked = [item["ticket"] for item
                   in seen["/projects/demo/attention"]["items"]
                   if item["kind"] == "blocked"]
        self.assertEqual(blocked, ["DEMO-4"])

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
        self.assertEqual(answer["ticket"], "DEMO-7")

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

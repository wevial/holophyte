"""`holo mcp --http`: the stdio server's tools at `POST /mcp` over the SDK's
Streamable HTTP transport, behind the host's machine token, driven by the
SDK's own client and by a plain HTTP client against a real subprocess.

Run: python3 -m unittest discover -s tests -p 'test_holo_mcp_http.py' -v
"""
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
import httpx2
from mcp import ClientSession, MCPError
from mcp.client.streamable_http import streamable_http_client

import holophyte.cli.entry
from holophyte.config.project import Project
from tests.host_fixture import git
from tests.test_holo_mcp import ROOT, TOOLS
from tests.test_holo_mcp_writes import AUTHOR, WRITES, WriteCase

TOKEN = "machine-secret"
FIXTURE = ROOT / "tests" / "fixtures" / "serve" / "mcp-tools-list.json"
NAME = "alpha"
CHECK_SEC = 2
# Uvicorn's graceful stop and the interpreter's exit, after the check fires.
SHUTDOWN_SEC = 1
DRAIN_SEC = 1
CUTS = {"\nCODE_CHECK_SEC = 15\n": f"\nCODE_CHECK_SEC = {CHECK_SEC}\n",
        "\nDRAIN_SEC = 20\n": f"\nDRAIN_SEC = {DRAIN_SEC}\n"}
START_WAIT_SEC = 30
REQUEUE = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
           "params": {"name": "requeue",
                      "arguments": {"project": NAME, "ticket": "HOLO-1",
                                    "note": "rerun", "author": AUTHOR}}}


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def listening(port):
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


class HttpCase(WriteCase):
    """A temporary home whose `host.toml` registers `alpha`, a native board
    holding HOLO-1 with a failed run, and names a machine token file; the
    server runs from `checkout` with `[serve] actions` set by `ACTIONS`."""

    ACTIONS = True

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.enterContext(patch.dict(os.environ,
                                     {"HOLOPHYTE_HOME": str(self.home)}))
        self.repo = self.root / NAME
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        target = Project.locate(self.repo, adopt=False)
        target.holo_dir.mkdir(parents=True)
        target.config_path.write_text('[board]\nkind = "native"\n'
                                      'prefix = "HOLO"\n'
                                      f'[serve]\nname = "{NAME}"\n')
        self.store_path = target.store_path
        with open(os.devnull, "w") as quiet, patch("sys.stdout", quiet):
            self.assertFalse(holophyte.cli.entry.cli(
                ["project", "add", str(self.repo)]))
        self.seed()
        token = self.home / "machine.token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        self.serve_table(f'machine_token_file = "machine.token"\n'
                         f'actions = {str(self.ACTIONS).lower()}\n')
        self.checkout = ROOT
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    def serve_table(self, text):
        registry = self.home / "host.toml"
        registry.write_text(registry.read_text() + "\n[serve]\n" + text)

    def environment(self, **extra):
        env = super().environment(**extra)
        env.pop("HOLO_PROJECT")
        return {**env, "PYTHONPATH": str(self.checkout)}

    def start(self):
        server = subprocess.Popen(
            [sys.executable, "-m", "holophyte.holo", "mcp", "--http",
             f"127.0.0.1:{self.port}"], cwd=self.outside,
            env=self.environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, server)
        deadline = time.monotonic() + START_WAIT_SEC
        while not listening(self.port):
            if server.poll() is not None or time.monotonic() > deadline:
                self.fail(f"the server never listened: {server.communicate()}")
            time.sleep(0.05)
        return server

    def stop(self, server):
        if server.poll() is None:
            server.terminate()
        server.communicate(timeout=30)

    def over_http(self, use):
        """`use(session, initialized)` on an SDK client session over HTTP
        presenting the machine token; its return value."""
        async def run():
            with anyio.fail_after(60):
                async with httpx2.AsyncClient(
                        headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                    async with streamable_http_client(
                            self.url, http_client=http) as streams:
                        async with ClientSession(streams[0],
                                                 streams[1]) as client:
                            initialized = await client.initialize()
                            return await use(client, initialized)
        return anyio.run(run)

    def post(self, method="POST", body=REQUEUE, **headers):
        """A plain HTTP request to `/mcp`: its status and decoded body."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, "/mcp", json.dumps(body).encode(), {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", **headers})
            reply = conn.getresponse()
            return reply.status, reply.read()
        finally:
            conn.close()


async def tools(client, _):
    return (await client.list_tools()).tools


class ActionsTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.start()

    def test_the_sdk_client_initializes_with_the_tools_capability(self):
        async def use(_, initialized):
            return initialized

        self.assertIsNotNone(self.over_http(use).capabilities.tools)

    def test_it_lists_what_the_stdio_server_lists(self):
        over_http = self.over_http(tools)
        on_stdio = self.session(tools)

        self.assertEqual({tool.name for tool in over_http}, TOOLS | WRITES)
        self.assertEqual([tool.model_dump() for tool in over_http],
                         [tool.model_dump() for tool in on_stdio])

    def test_the_tools_list_reply_is_the_pinned_fixture(self):
        status, body = self.post(
            body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            Authorization=f"Bearer {TOKEN}")

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), json.loads(FIXTURE.read_text()))

    def test_requeue_runs_on_the_host_and_records_the_author_via_mcp(self):
        (self.home / "client.toml").write_text('host = "seat.invalid"\n')

        async def use(client, _):
            return await client.call_tool("requeue", {
                "project": NAME, "ticket": "HOLO-1",
                "note": "rerun after the fix", "author": AUTHOR})
        result = self.over_http(use)

        self.assertIs(result.is_error, False, result.content)
        self.assertEqual(self.status("HOLO-1"), "ready")
        self.assertEqual(self.interventions()[-1],
                         (result.structured_content["recorded"], "requeue",
                          "test seat via MCP: rerun after the fix"))


class GuardTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.start()

    def test_no_bearer_or_a_wrong_one_is_401_with_an_empty_body(self):
        before = self.dump()
        for headers in ({}, {"Authorization": "Bearer not-the-token"},
                        {"Authorization": TOKEN}):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(**headers), (401, b"{}"))
        self.assertEqual(self.dump(), before)

    def test_an_origin_is_403_and_a_get_is_405(self):
        before = self.dump()
        bearer = {"Authorization": f"Bearer {TOKEN}"}

        status, _ = self.post(Origin="https://example.test", **bearer)
        self.assertEqual(status, 403)
        status, _ = self.post("GET", **bearer)
        self.assertEqual(status, 405)
        self.assertEqual(self.dump(), before)


class ReadsOnlyTests(HttpCase):
    ACTIONS = False

    def test_only_the_reads_are_listed_and_a_write_is_unknown(self):
        self.start()
        before = self.dump()

        async def use(client, _):
            listed = await tools(client, _)
            try:
                await client.call_tool("requeue", REQUEUE["params"]["arguments"])
            except MCPError as refused:
                return listed, refused
            return listed, None
        listed, refused = self.over_http(use)

        self.assertEqual({tool.name for tool in listed}, TOOLS)
        self.assertIsNotNone(refused, "requeue answered")
        self.assertEqual(refused.error.code, -32602)
        self.assertEqual(self.dump(), before)


class StartupTests(HttpCase):
    def test_no_machine_token_file_exits_1_naming_it_and_nothing_listens(self):
        registry = self.home / "host.toml"
        registry.write_text(registry.read_text().replace(
            'machine_token_file = "machine.token"\n', ""))

        done = subprocess.run(
            [sys.executable, "-m", "holophyte.holo", "mcp", "--http",
             f"127.0.0.1:{self.port}"], cwd=self.outside,
            env=self.environment(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=60)

        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("[serve] machine_token_file", done.stderr)
        self.assertNotIn("serving", done.stdout)


class CodeFollowTests(HttpCase):
    def setUp(self):
        super().setUp()
        self.checkout = self.clone()

    def clone(self):
        """A `git clone` of this factory whose check interval and drain are
        cut to `CHECK_SEC` and `DRAIN_SEC` in a commit of its own, made
        before the server starts."""
        checkout = self.root / "factory"
        git(self.root, "clone", "-q", str(ROOT), str(checkout))
        watch = checkout / "holophyte" / "serve" / "serve_watch.py"
        text = watch.read_text()
        for line, cut in CUTS.items():
            self.assertIn(line, text)
            text = text.replace(line, cut)
        watch.write_text(text)
        git(checkout, "commit", "-q", "-am", "check interval and drain")
        return checkout

    def assert_exits_0_after_a_commit(self, server, within):
        (self.checkout / "moved.txt").write_text("B\n")
        git(self.checkout, "add", "moved.txt")
        git(self.checkout, "commit", "-q", "-m", "B")
        committed = time.monotonic()
        try:
            code = server.wait(timeout=within)
        except subprocess.TimeoutExpired:
            self.fail(f"still serving {within}s after the commit")
        elapsed = time.monotonic() - committed

        self.assertEqual(code, 0, server.communicate())
        self.assertLessEqual(elapsed, within)

    def test_a_new_commit_in_the_clone_exits_0_within_the_check_interval(self):
        server = self.start()

        self.assert_exits_0_after_a_commit(server, CHECK_SEC + SHUTDOWN_SEC)

    def test_a_stalled_request_holds_the_exit_no_longer_than_the_drain(self):
        server = self.start()
        stalled = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(stalled.close)
        stalled.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                        b"Authorization: Bearer " + TOKEN.encode() +
                        b"\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 1000\r\n\r\n{")

        self.assert_exits_0_after_a_commit(
            server, CHECK_SEC + DRAIN_SEC + SHUTDOWN_SEC)


if __name__ == "__main__":
    unittest.main()

"""`holo mcp`: an MCP server on stdio whose tools are the `holo` reads, each
answering what its `holo ... --json` command prints, driven here by the SDK's
own stdio client and by raw pipes, both to a real subprocess.

Run: python3 -m unittest discover -s tests -p 'test_holo_mcp.py' -v
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio
from mcp import ClientSession, MCPError, StdioServerParameters, stdio_client

import holophyte.cli.entry
import store
import store.tickets
from holophyte.config.project import Project
from holophyte.holo.cli import package_version

ROOT = Path(__file__).resolve().parent.parent
MINUTE = 60_000
NOW = 1_750_000_000_000
TOOLS = {"status", "attention", "report", "runs", "run", "board", "ticket",
         "board_diff", "sweep_preview"}
INSTALL = "python3 -m pip install --user -r requirements.txt"
PROTOCOL = "2025-11-25"


class McpCase(unittest.TestCase):
    """A temporary home registering `alpha`, a native board whose store holds
    ALPHA-1 with a failed run and ALPHA-2, ready but depending on ALPHA-1."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.enterContext(patch.dict(os.environ,
                                     {"HOLOPHYTE_HOME": str(self.home)}))
        self.alpha = self.root / "alpha"
        subprocess.run(["git", "init", "-q", str(self.alpha)], check=True)
        target = Project.locate(self.alpha, adopt=False)
        target.holo_dir.mkdir(parents=True)
        target.config_path.write_text('[board]\nkind = "native"\n'
                                      'prefix = "ALPHA"\n'
                                      '[serve]\nname = "alpha"\n')
        self.store_path = target.store_path
        with open(os.devnull, "w") as quiet, patch("sys.stdout", quiet):
            self.assertFalse(holophyte.cli.entry.cli(
                ["project", "add", str(self.alpha)]))
        self.seed()

    def seed(self):
        conn = store.open(str(self.store_path))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "native:ALPHA",
                                                   self.alpha)
            failed = self.ticket(conn, project, "ALPHA-1")
            store.tickets.transition(conn, failed, "in_flight")
            run = store.claim(conn, project, failed, now=NOW - 30 * MINUTE)
            store.release(conn, run, "failed", reason="verify went red",
                          now=NOW - MINUTE)
            self.ticket(conn, project, "ALPHA-2", depends_on=["ALPHA-1"])
        finally:
            conn.close()

    def ticket(self, conn, project, key, **extra):
        return store.tickets.mirror_ticket(
            conn, project, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE,
            board_column="ready", **extra)

    def environment(self, **extra):
        env = {key: value for key, value in os.environ.items()
               if key not in ("HOLO_PROJECT", "HOLO_TRANSPORT")}
        env.update(HOLOPHYTE_HOME=str(self.home), PYTHONPATH=str(ROOT),
                   GIT_CEILING_DIRECTORIES=str(self.root))
        return {**env, **extra}

    def holo(self, *args, **extra):
        return subprocess.run([sys.executable, "-m", "holophyte.holo", *args],
                              cwd=self.outside, capture_output=True,
                              text=True, env=self.environment(**extra))

    def session(self, use):
        """`use(session, initialized)` on an SDK client session with
        `python3 -m holophyte.holo mcp`; its return value."""
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "holophyte.holo", "mcp"],
            env=self.environment(), cwd=str(self.outside))

        async def run():
            with anyio.fail_after(60):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as client:
                        initialized = await client.initialize()
                        return await use(client, initialized)
        return anyio.run(run)

    def call(self, name, arguments):
        """The tool's result, or the `MCPError` the call raised."""
        async def use(client, _):
            try:
                return await client.call_tool(name, arguments)
            except MCPError as refused:
                return refused
        return self.session(use)

    def dump(self):
        conn = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()


class SessionTests(McpCase):
    def test_initialize_offers_tools_and_names_the_server_and_its_version(self):
        async def use(_, initialized):
            return initialized
        initialized = self.session(use)

        self.assertIsNotNone(initialized.capabilities.tools)
        self.assertEqual((initialized.server_info.name,
                          initialized.server_info.version),
                         ("holo", package_version()))

    def test_tools_are_the_nine_reads_each_read_only_with_an_input_schema(self):
        async def use(client, _):
            return (await client.list_tools()).tools
        tools = self.session(use)

        self.assertEqual(sorted(tool.name for tool in tools), sorted(TOOLS))
        for tool in tools:
            with self.subTest(tool=tool.name):
                self.assertEqual(tool.input_schema["type"], "object")
                self.assertIs(tool.annotations.read_only_hint, True)

    def test_status_structured_content_is_what_holo_status_json_prints(self):
        result = self.call("status", {"project": "alpha"})
        oracle = self.holo("status", "--json", "-p", "alpha")

        self.assertEqual(oracle.returncode, 0, oracle.stderr)
        self.assertIs(result.is_error, False)
        self.assertEqual(result.structured_content, json.loads(oracle.stdout))
        self.assertEqual(json.loads(result.content[0].text),
                         result.structured_content)

    def test_a_whole_number_sent_as_a_float_runs_as_the_integer(self):
        async def use(client, _):
            return (await client.call_tool("runs", {"project": "alpha",
                                                    "limit": 1.0}),
                    await client.call_tool("run", {"project": "alpha",
                                                   "run": 1.0,
                                                   "view": "ledger"}))
        runs, ledger = self.session(use)
        oracles = (self.holo("runs", "--limit", "1", "--json", "-p", "alpha"),
                   self.holo("run", "1", "--ledger", "--json", "-p", "alpha"))

        for result, oracle in zip((runs, ledger), oracles):
            with self.subTest(args=oracle.args[3:]):
                self.assertEqual(oracle.returncode, 0, oracle.stderr)
                self.assertIs(result.is_error, False, result.content)
                self.assertEqual(result.structured_content,
                                 json.loads(oracle.stdout))

    def test_a_run_the_store_lacks_is_an_error_result_with_its_message(self):
        result = self.call("run", {"project": "alpha", "run": 999})
        oracle = self.holo("run", "999", "--json", "-p", "alpha")

        self.assertEqual(oracle.returncode, 1, oracle.stderr)
        self.assertEqual(json.loads(oracle.stdout)["error"], "no such run")
        self.assertIs(result.is_error, True)
        self.assertEqual(result.structured_content, json.loads(oracle.stdout))
        self.assertEqual(json.loads(result.content[0].text),
                         result.structured_content)

    def test_arguments_the_input_schema_refuses_are_an_error_result(self):
        result = self.call("run", {"project": "alpha", "run": "12"})

        self.assertIs(result.is_error, True)
        self.assertIn("'12' is not of type 'integer'", result.content[0].text)

    def test_an_unknown_tool_is_invalid_params_and_writes_nothing(self):
        before = self.dump()

        refused = self.call("requeue", {"project": "alpha", "key": "ALPHA-1"})

        self.assertIsInstance(refused, MCPError)
        self.assertEqual(refused.code, -32602)
        self.assertEqual(self.dump(), before)

    def test_board_diff_is_the_difference_as_text_though_it_exits_1(self):
        command = self.holo("board", "diff", "-p", "alpha")
        result = self.call("board_diff", {"project": "alpha"})

        self.assertEqual(command.returncode, 1, command.stderr)
        self.assertIs(result.is_error, False)
        self.assertIn("ALPHA-2: ready in the store, not on the board's ready"
                      " listing", result.content[0].text)
        self.assertIsNone(result.structured_content)


class RawPipeTests(McpCase):
    """The server on bare pipes, read without the SDK's client; killed after
    60 seconds, so a missing line ends the read rather than hanging it."""

    def start(self):
        self.errors = self.enterContext(tempfile.TemporaryFile("w+"))
        child = subprocess.Popen(
            [sys.executable, "-m", "holophyte.holo", "mcp"], cwd=self.outside,
            env=self.environment(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.errors, text=True)
        deadline = threading.Timer(60, child.kill)
        deadline.start()
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.addCleanup(deadline.cancel)
        self.addCleanup(child.stdout.close)
        self.send(child, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": PROTOCOL,
                                     "capabilities": {},
                                     "clientInfo": {"name": "raw",
                                                    "version": "0"}}})
        return child

    def send(self, child, message):
        child.stdin.write(json.dumps(message) + "\n")
        child.stdin.flush()

    def until(self, child, ident):
        lines = []
        while True:
            line = child.stdout.readline()
            self.assertTrue(line, f"stdout ended before response {ident}")
            lines.append(line)
            if json.loads(line).get("id") == ident:
                return lines

    def test_every_stdout_line_is_a_json_rpc_message_while_a_command_prints(self):
        child = self.start()
        lines = self.until(child, 1)
        self.send(child, {"jsonrpc": "2.0",
                          "method": "notifications/initialized"})
        self.send(child, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "status",
                                     "arguments": {"project": "nosuch"}}})
        lines += self.until(child, 2)
        child.stdin.close()
        lines += child.stdout.readlines()
        child.wait(timeout=30)

        messages = [json.loads(line) for line in lines]
        self.assertTrue(all(message["jsonrpc"] == "2.0"
                            for message in messages))
        [answer] = [message["result"] for message in messages
                    if message.get("id") == 2]
        self.assertIs(answer["isError"], True)
        self.assertIn("[holo2]", answer["content"][0]["text"])

    def test_closing_stdin_after_initialize_exits_0(self):
        child = self.start()
        self.until(child, 1)
        self.send(child, {"jsonrpc": "2.0",
                          "method": "notifications/initialized"})
        child.stdin.close()

        code = child.wait(timeout=30)
        self.errors.seek(0)
        self.assertEqual(code, 0, self.errors.read())


class WithoutSdkTests(McpCase):
    def test_only_holo_mcp_needs_the_sdk_and_it_names_the_install(self):
        broken = self.root / "broken"
        (broken / "mcp").mkdir(parents=True)
        (broken / "mcp" / "__init__.py").write_text(
            "raise ImportError('no mcp here')\n")
        path = os.pathsep.join((str(broken), str(ROOT)))
        factory = subprocess.run(
            [sys.executable, str(ROOT / "factory.py"), "--help"],
            cwd=self.outside, capture_output=True, text=True,
            env=self.environment(PYTHONPATH=path))
        status = self.holo("status", "--json", PYTHONPATH=path)
        served = self.holo("mcp", PYTHONPATH=path)

        self.assertEqual(factory.returncode, 0, factory.stderr)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(served.returncode, 1, served.stderr)
        self.assertIn("'mcp'", served.stderr)
        self.assertIn(INSTALL, served.stderr)


if __name__ == "__main__":
    unittest.main()

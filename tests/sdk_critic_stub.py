"""A stub `claude` CLI that the real `claude_agent_sdk` drives, and its case.

The stub speaks the pinned SDK's stream-json transport: the `-v` version
check, the `initialize` control request, and one user message answered by
an `assistant` and a `result` message. Given MCP calls, before it answers it
sends them to the session's in-process `holo_run` server as the CLI does,
`mcp_message` control requests carrying JSON-RPC (an `initialize`
handshake, then each call), and records each JSON-RPC response.
"""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from loop_fixture import LoopFixture

import holophyte.agents.fallback
import holophyte.loop.runs
import holophyte.review.freshness
import store.tickets as tickets
from holophyte.agents.agent_routes import reset

STUB = """
import os, signal, subprocess, sys, time
if SCRIPT.get("pids"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open(SCRIPT["pids"], "a") as out:
        out.write(f"{os.getpid()}\\n")
    while True:
        time.sleep(1)
if sys.argv[1:] == ["-v"]:
    print("2.1.300 (Claude Code)")
    sys.exit(0)
git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True)
with open(SCRIPT["calls"], "a") as calls:
    calls.write(json.dumps({
        "argv": sys.argv[1:], "cwd": os.getcwd(),
        "head": git("rev-parse", "HEAD").stdout.strip(),
        "detached": git("symbolic-ref", "-q", "HEAD").returncode != 0,
        "env": {name: os.environ.get(name) for name in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "SDK_CRITIC_MARKER")},
    }) + "\\n")
if SCRIPT.get("pid"):
    with open(SCRIPT["pid"], "w") as out:
        out.write(str(os.getpid()))
    while True:
        time.sleep(1)

def send(message):
    sys.stdout.write(json.dumps(message) + "\\n")
    sys.stdout.flush()

def mcp(method, params, rpc_id=None):
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    if rpc_id is not None:
        message["id"] = rpc_id
    request_id = f"stub-{method}-{rpc_id}"
    send({"type": "control_request", "request_id": request_id, "request": {
        "subtype": "mcp_message", "server_name": "holo_run",
        "message": message}})
    for line in iter(sys.stdin.readline, ""):
        reply = json.loads(line)
        if (reply["type"] == "control_response"
                and reply["response"]["request_id"] == request_id):
            return reply["response"]["response"]["mcp_response"]
    sys.exit(3)

def call_tools():
    mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "stub", "version": "0"}}, 0)
    mcp("notifications/initialized", {})
    answers = [mcp(method, params, n)
               for n, (method, params) in enumerate(SCRIPT["mcp"], 1)]
    with open(SCRIPT["answers"], "w") as out:
        json.dump(answers, out)

for line in iter(sys.stdin.readline, ""):
    message = json.loads(line)
    if message["type"] == "control_request":
        send({"type": "control_response", "response": {
            "subtype": "success", "request_id": message["request_id"],
            "response": {}}})
    elif message["type"] == "user":
        probe = "single word: ready" in message["message"]["content"]
        if SCRIPT["mcp"] and not probe:
            call_tools()
        text = "ready" if probe and not SCRIPT["is_error"] else SCRIPT["answer"]
        send({"type": "assistant", "session_id": "s-1",
              "parent_tool_use_id": None, "message": {
                  "role": "assistant", "model": "stub",
                  "content": [{"type": "text", "text": text}]}})
        send({"type": "result", "subtype": "success", "session_id": "s-1",
              "duration_ms": 5, "duration_api_ms": 4,
              "is_error": SCRIPT["is_error"], "num_turns": SCRIPT["num_turns"],
              "result": text, "usage": SCRIPT["usage"],
              "total_cost_usd": SCRIPT["cost"]})
        if SCRIPT["is_error"]:
            sys.exit(1)
"""

STALE = "Looked at main.\nFRESHNESS: STALE already done by KO-5"
SDK_CRITIC = ('[agents.critic]\nharness = "claude-sdk"\nmodel = "sonnet"\n'
              'effort = "high"\n')


class SdkCriticCase(LoopFixture):
    def setUp(self):
        super().setUp()
        self.calls = self.target.parent / "sdk-calls.jsonl"
        self.answers = self.target.parent / "sdk-answers.json"
        self.stub = self.target.parent / "claude"
        self.addCleanup(reset, self.project)

    def stubbed(self, answer=STALE, *, is_error=False, usage=None, cost=None,
                num_turns=1, pid=None, pids=None, extra="", agents="", mcp=()):
        """Configure a `claude-sdk` critic whose `[harnesses] claude` is a stub
        CLI answering `answer` with the given usage, or, given `pid`, writing
        its pid there and never answering; given `pids`, every invocation,
        `-v` included, appends its pid there, ignores SIGTERM and hangs.
        Given `mcp`, (method, params) pairs, a turn first sends each to the
        `holo_run` server and `mcp_answers()` returns the responses."""
        script = json.dumps(dict(
            calls=str(self.calls), answer=answer, is_error=is_error,
            usage=usage or {}, cost=cost, num_turns=num_turns,
            pid=str(pid) if pid else None, pids=str(pids) if pids else None,
            mcp=[list(call) for call in mcp], answers=str(self.answers)))
        self.stub.write_text(f"#!{sys.executable}\nimport json\n"
                             f"SCRIPT = json.loads({script!r})\n" + STUB)
        self.stub.chmod(0o755)
        self.configure(f'{agents}{SDK_CRITIC}{extra}[harnesses]\n'
                       f'claude = "{self.stub}"\n[loop]\ncritic_after_hours = 12\n')
        self.addCleanup(reset, self.project)

    def stub_calls(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def mcp_answers(self):
        return json.loads(self.answers.read_text())

    def admits(self, task, provider):
        conn = holophyte.loop.runs.open_store(self.project)
        self.addCleanup(conn.close)
        project_id = tickets.ensure_project(conn, provider.team, self.target)
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            admitted = holophyte.review.freshness.critic_admits(
                self.project, conn, project_id, provider, task)
        return admitted, printed.getvalue()

    def start(self, **options):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            started = holophyte.agents.fallback.startup_routes(
                self.project, SimpleNamespace(team="test"), **options)
        return started, printed.getvalue()


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0]
                == "Z")

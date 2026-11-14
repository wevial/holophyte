import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from holophyte.redact import redact_prose

THREAD_LINE = re.compile(r"^Thread (\S+)$")
STILL_RUNNING = "is still running"
SETTLE_WINDOW = 30
SETTLE_PAUSE = 1
CALL_TIMEOUT = 30
PROTOCOL = "2025-06-18"
ISSUE = ("auth", "session", "issue", "--subject", "mcp-client",
         "--scope", "orchestration:read", "--scope", "orchestration:operate",
         "--ttl", "5m", "--label", "holophyte critic cleanup", "--json")


class StepFailed(Exception):
    pass


def thread_id(output):
    for line in output.splitlines():
        match = THREAD_LINE.fullmatch(line.strip())
        if match:
            return match.group(1)
    return None


def first_line(error, secrets=()):
    text = redact_prose(str(error), secrets).strip()
    return (text.splitlines() or [type(error).__name__])[0][:200]


def runtime_origin():
    home = os.environ.get("T3CODE_HOME") or str(Path.home() / ".t3")
    state = Path(home) / "userdata" / "server-runtime.json"
    return json.loads(state.read_text())["origin"]


def t3(binary, *args):
    done = subprocess.run([binary, *args], capture_output=True, text=True,
                          timeout=CALL_TIMEOUT)
    if done.returncode != 0:
        raise StepFailed(done.stderr.strip() or done.stdout.strip()
                         or f"t3 exited {done.returncode}")
    return done.stdout


def git(*args):
    done = subprocess.run(["git", *args], capture_output=True, text=True)
    if done.returncode != 0:
        raise StepFailed(done.stderr.strip() or f"git exited {done.returncode}")
    return done.stdout.strip()


class Mcp:
    def __init__(self, origin, token):
        self.url = origin.rstrip("/") + "/mcp"
        self.token = token
        self.session = None
        self.protocol = None
        self.ids = 0

    def post(self, message):
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        request = urllib.request.Request(
            self.url, json.dumps(message).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=CALL_TIMEOUT) as reply:
                self.session = reply.headers.get("Mcp-Session-Id") or self.session
                return reply.headers.get_content_type(), reply.read().decode()
        except urllib.error.HTTPError as e:
            raise StepFailed(f"HTTP {e.code}: {e.read().decode(errors='replace')}")

    def request(self, method, params):
        self.ids += 1
        kind, body = self.post({"jsonrpc": "2.0", "id": self.ids,
                                "method": method, "params": params})
        replies = ([json.loads(line[5:]) for line in body.splitlines()
                    if line.startswith("data:") and line[5:].strip()]
                   if kind == "text/event-stream" else [json.loads(body)])
        reply = next((r for r in replies if r.get("id") == self.ids), None)
        if reply is None:
            raise StepFailed(f"no reply to {method}")
        if "error" in reply:
            raise StepFailed(reply["error"].get("message") or str(reply["error"]))
        return reply.get("result") or {}

    def initialize(self):
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "holophyte", "version": "1"}})
        self.protocol = result.get("protocolVersion", PROTOCOL)
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool, arguments):
        result = self.request("tools/call", {"name": tool,
                                             "arguments": arguments})
        if result.get("isError"):
            texts = [c.get("text", "") for c in result.get("content") or []]
            raise StepFailed(" ".join(texts).strip() or f"{tool} failed")
        return result.get("structuredContent") or {}


def settle(mcp, thread, interrupted):
    deadline = time.monotonic() + SETTLE_WINDOW
    while True:
        try:
            return mcp.call("t3_thread_organize",
                            {"threadId": thread, "action": "settle"})
        except StepFailed as e:
            if (not interrupted or STILL_RUNNING not in str(e)
                    or time.monotonic() >= deadline):
                raise
        time.sleep(SETTLE_PAUSE)


def outcome(thread, step, error=None, secrets=()):
    detail = "" if error is None else first_line(error, secrets)
    return dict(thread=thread, step=step, ok=error is None, detail=detail)


def interrupt(mcp, thread, secrets):
    try:
        mcp.call("t3_thread_interrupt", {"threadId": thread})
    except Exception as e:
        return outcome(thread, "interrupt", e, secrets)
    return outcome(thread, "interrupt")


def clean_thread(binary, output, timed_out, secrets=()):
    thread = thread_id(output)
    if thread is None:
        return [outcome(None, "thread",
                        StepFailed("the turn's output holds no Thread line"))]
    steps, hidden, session = [], set(secrets), {}
    step = "interrupt" if timed_out else "settle"
    try:
        session.update(json.loads(t3(binary, *ISSUE)))
        hidden.add(session["token"])
        mcp = Mcp(runtime_origin(), session["token"])
        mcp.initialize()
        if timed_out:
            steps.append(interrupt(mcp, thread, hidden))
            step = "settle"
        place = mcp.call("t3_thread_read",
                         {"threadId": thread}).get("thread") or {}
        settle(mcp, thread, timed_out)
        steps.append(outcome(thread, step))
        path, branch = place.get("worktreePath"), place.get("branch")
        if path and Path(path).exists():
            step = "worktree"
            common = git("-C", path, "rev-parse", "--path-format=absolute",
                         "--git-common-dir")
            git("-C", common, "worktree", "remove", "--force", path)
            steps.append(outcome(thread, step))
            if branch:
                step = "branch"
                git("-C", common, "branch", "-D", branch)
                steps.append(outcome(thread, step))
    except Exception as e:
        steps.append(outcome(thread, step, e, hidden))
    finally:
        if session.get("sessionId"):
            try:
                t3(binary, "auth", "session", "revoke", session["sessionId"])
            except Exception as e:
                steps.append(outcome(thread, "revoke", e, hidden))
    return steps

"""After a `harness = "t3"` critic turn the factory settles the T3 thread over
T3's MCP endpoint, then removes the worktree and branch T3 made for it, each
step recorded on the claimed run as a `t3_cleanup` event.

Run: python3 -m unittest tests.test_t3_cleanup -v
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
# Both discovery and named-module unittest commands need the harness on sys.path.
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit, FakeAgent  # noqa: E402
from loop_fixture import LoopFixture, StubProvider  # noqa: E402
from test_critic import RealCritic, filed  # noqa: E402

import holophyte.review.freshness  # noqa: E402 - after the sys.path insert above
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.agents.t3_cleanup import clean_thread  # noqa: E402

STILL_RUNNING = "Thread mcp:t1 is still running. Stop it before settling."
FAKE_T3 = """\
#!{python}
import json, subprocess, sys, time
args = sys.argv[1:]
with open({log!r}, "a") as log:
    log.write(" ".join(args[:4]) + "\\n")
if args[:2] == ["thread", "list"]:
    sys.exit(0)
if args[:3] == ["auth", "session", "issue"]:
    print(json.dumps({{"sessionId": "s1", "token": "tok"}}))
    sys.exit(0)
if args[:3] == ["auth", "session", "revoke"]:
    sys.exit(0)
if args[:2] == ["thread", "start"]:
    if {hang!r}:
        print("Thread mcp:t1", file=sys.stderr, flush=True)
        time.sleep(30)
    subprocess.run(["git", "-C", args[2], "worktree", "add", "-q", "-b",
                    "t3/critic-check", {worktree!r}, "main"], check=True)
    print("Thread mcp:t1", file=sys.stderr, flush=True)
    print("FRESHNESS: FRESH", flush=True)
    sys.exit(0)
sys.exit(2)
"""


class FakeMcp(BaseHTTPRequestHandler):
    """Answers MCP over HTTP as the T3 server would; logs each tool call."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer tok":
            self.send_response(401)
            self.end_headers()
            return
        if "id" not in message:
            self.send_response(202)
            self.end_headers()
            return
        params = message.get("params") or {}
        if message["method"] == "initialize":
            result = {"protocolVersion": params["protocolVersion"]}
        else:
            self.server.calls.append((params["name"], params["arguments"]))
            result = self.server.answer(params["name"])
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"],
                           "result": result})
        stream = self.server.stream
        payload = (f"event: message\ndata: {body}\n\n" if stream else body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if stream
                         else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class T3CleanupTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.root = self.target.parent
        self.t3_project = self.root / "t3-project"
        self.worktree = self.root / "t3-worktrees" / "critic-check"
        self.log = self.root / "t3.log"
        identity = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", "-b", "main",
                        str(self.t3_project)], check=True)
        subprocess.run(["git", *identity, "-C", str(self.t3_project), "commit",
                        "-q", "--allow-empty", "-m", "root"], check=True)

    def serve(self, settle_error=None, worktree=True, stream=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcp)
        server.calls, server.stream = [], stream
        thread = {"id": "mcp:t1"}
        if worktree:
            thread.update(worktreePath=str(self.worktree),
                          branch="t3/critic-check")

        def answer(tool):
            if tool == "t3_thread_read":
                return {"isError": False, "structuredContent": {"thread": thread}}
            if tool == "t3_thread_organize" and settle_error:
                return {"isError": True, "content": [
                    {"type": "text", "text": settle_error}]}
            return {"isError": False, "structuredContent": {"sequence": 7}}
        server.answer = answer
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        home = self.root / "t3-home"
        (home / "userdata").mkdir(parents=True)
        (home / "userdata" / "server-runtime.json").write_text(json.dumps(
            {"origin": f"http://127.0.0.1:{server.server_port}"}))
        env = patch.dict(os.environ, {"T3CODE_HOME": str(home)})
        env.start()
        self.addCleanup(env.stop)
        return server

    def claim(self, hang=False):
        stub = self.root / "t3"
        stub.write_text(FAKE_T3.format(python=sys.executable, log=str(self.log),
                                       hang=hang, worktree=str(self.worktree)))
        stub.chmod(0o755)
        self.configure(f'[agents.critic]\nharness = "t3"\n'
                       f'project = "{self.t3_project}"\nworktree = "main"\n'
                       'provider = "codex"\nmodel = "gpt-6-luna"\n'
                       f'[harnesses]\nt3 = "{stub}"\n'
                       '[loop]\ncritic_after_hours = 12\n')
        self.addCleanup(reset, self.project)
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(fake=RealCritic(FakeAgent(Commit("one"), APPROVE)),
                      provider=StubProvider(filed(13, 1)))
        self.assertTrue(self.read(
            "SELECT 1 FROM runs r JOIN tickets t ON t.id = r.ticketId"
            " WHERE t.linearIdentifier = 'KO-131'"), "KO-131 was not claimed")
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT e.payload FROM runEvents e JOIN runs r ON r.id = e.runId"
            " JOIN tickets t ON t.id = r.ticketId WHERE e.kind = 't3_cleanup'"
            " AND t.linearIdentifier = 'KO-131' ORDER BY e.seq")]

    def listed_worktrees(self):
        return subprocess.run(
            ["git", "-C", str(self.t3_project), "worktree", "list"],
            capture_output=True, text=True, check=True).stdout

    def test_a_settled_thread_loses_its_worktree_and_branch(self):
        server = self.serve(stream=True)

        steps = self.claim()

        self.assertIn(("t3_thread_organize",
                       {"threadId": "mcp:t1", "action": "settle"}), server.calls)
        self.assertNotIn(str(self.worktree), self.listed_worktrees())
        self.assertEqual(subprocess.run(
            ["git", "-C", str(self.t3_project), "branch", "--list",
             "t3/critic-check"], capture_output=True, text=True,
            check=True).stdout, "")
        self.assertIn("auth session revoke s1", self.log.read_text())
        self.assertEqual([(s["thread"], s["step"], s["ok"]) for s in steps],
                         [("mcp:t1", "settle", True), ("mcp:t1", "worktree", True),
                          ("mcp:t1", "branch", True)])

    def test_a_refused_settle_leaves_the_worktree_and_still_claims(self):
        self.serve(settle_error=STILL_RUNNING)

        steps = self.claim()

        self.assertEqual([(s["step"], s["ok"]) for s in steps],
                         [("settle", False)])
        self.assertIn(STILL_RUNNING, steps[0]["detail"])
        self.assertIn(str(self.worktree), self.listed_worktrees())
        self.assertIn("auth session revoke s1", self.log.read_text())

    def test_a_timed_out_turn_interrupts_its_thread_before_settling_it(self):
        server = self.serve(worktree=False)

        with patch.object(holophyte.review.freshness, "CRITIC_TIMEOUT", 2):
            steps = self.claim(hang=True)

        tools = [name for name, _ in server.calls]
        self.assertIn(("t3_thread_interrupt", {"threadId": "mcp:t1"}),
                      server.calls)
        self.assertLess(tools.index("t3_thread_interrupt"),
                        tools.index("t3_thread_organize"))
        self.assertEqual([(s["step"], s["ok"]) for s in steps],
                         [("interrupt", True), ("settle", True)])
        self.assertIn("critic: its turn failed (timed out after 2 seconds)",
                      [summary for (summary,) in self.read(
                          "SELECT summary FROM runEvents WHERE kind = 'warning'"
                          " AND summary LIKE 'critic:%'")][0])



class CleanupFailureTests(unittest.TestCase):
    def t3(self, issue, revoke):
        """A fake `t3` that runs `issue` for `auth session issue` and `revoke`
        for `auth session revoke`; the T3 home holds no runtime state."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"T3CODE_HOME": str(root)}))
        binary = root / "t3"
        binary.write_text(f"#!{sys.executable}\nimport sys\n"
                          "if sys.argv[1:4] == ['auth', 'session', 'issue']:\n"
                          f"{issue}"
                          "if sys.argv[1:4] == ['auth', 'session', 'revoke']:\n"
                          f"{revoke}")
        binary.chmod(0o755)
        return str(binary)

    def test_a_secret_cut_by_the_detail_limit_leaves_no_prefix(self):
        secret = "s3cr3t-credential-value-0123456789"
        binary = self.t3(f"    sys.exit('x' * 190 + {secret!r})\n",
                         "    sys.exit(0)\n")

        [step] = clean_thread(binary, "Thread mcp:t1", False, {secret})

        self.assertEqual((step["step"], step["ok"]), ("settle", False))
        self.assertNotIn(secret[:10], step["detail"])

    def test_a_failed_revoke_is_recorded_as_a_failed_step(self):
        binary = self.t3("    print('{\"sessionId\": \"s1\", \"token\": \"tok\"}')\n"
                         "    sys.exit(0)\n",
                         "    sys.exit('database is locked')\n")

        steps = clean_thread(binary, "Thread mcp:t1", False)

        self.assertEqual([(s["step"], s["ok"]) for s in steps],
                         [("settle", False), ("revoke", False)])
        self.assertIn("database is locked", steps[1]["detail"])

if __name__ == "__main__":
    unittest.main()

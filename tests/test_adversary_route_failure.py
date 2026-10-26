"""An adversary turn whose container reply cannot be read is kept and switches."""
from tests.loop_fixture import Commit, LoopFixture, StubProvider, a_task  # isort: skip

import contextlib
import io
import json
import os
import sys
from unittest.mock import patch

from fake_agent import FakeAgent, SpawnGuard, no_agent_processes  # noqa: E402

import holophyte.cli.operator
import holophyte.loop.implement
from holophyte.agents import probes
from holophyte.agents.agent_routes import reset
from holophyte.review import review_runner
from holophyte.serve import serve_runs

CUT = '{"type":"item.comp'

# `run` answers by the prompt, since the review and adversary turns run at
# once; `inspect` reports the removed container gone. The adversary reply is
# killed by SIGTERM unless ADVERSARY_EXIT is 0.
DOCKER = f"""#!{sys.executable}
import json, os, signal, subprocess, sys
args = sys.argv[1:]
if args[:1] == ["inspect"]:
    sys.exit(1)
if args[:1] != ["run"]:
    sys.exit(0)
prompt = args[args.index("review") + 1]
source = next(v.split(":")[0] for v in args if v.endswith(":/workspace:ro"))
def say(text):
    print(json.dumps({{"type": "thread.started"}}))
    print(json.dumps({{"type": "item.completed", "item": {{
        "type": "command_execution", "exit_code": 0}}}}))
    print(json.dumps({{"type": "item.completed",
                      "item": {{"type": "agent_message", "text": text}}}}))
print("PREFLIGHT_OK candidate=stand-in", file=sys.stderr, flush=True)
if prompt == {probes.REVIEW_PROBE_GOAL!r}:
    say("ready " + subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True).strip())
elif prompt.startswith("You are a READ-ONLY adversarial"):
    print(json.dumps({{"type": "thread.started"}}))
    print({CUT!r}, flush=True)
    if os.environ.get("ADVERSARY_EXIT") != "0":
        os.kill(os.getpid(), signal.SIGTERM)
else:
    say("Reviewed the diff; no blockers.\\n"
        "CRITERION 1: met \\u2014 tests/test_thing.py::test_it_works\\n"
        "SCOPE poetry.lock: needed \\u2014 the scripted work\\n"
        "VERDICT: APPROVE")
"""


class KilledAdversaryContainerTests(LoopFixture):
    def setUp(self):
        super().setUp()
        root = self.target.parent
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for name in ("docker", "codex", "codex-code-mode-host"):
            (bin_dir / name).write_text(
                DOCKER if name == "docker" else "#!/bin/sh\nexit 0\n")
            (bin_dir / name).chmod(0o755)
        (root / "auth.json").write_text("{}")
        for patcher in (
                patch.dict(os.environ, {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}),
                patch.object(review_runner, "SCRATCH_ROOT", root / "reviews"),
                patch.object(review_runner, "CODEX_AUTH", root / "auth.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(reset, self.project)

    def payloads(self, kind):
        return [json.loads(payload) for (payload,) in self.read(
            f"SELECT payload FROM runEvents WHERE kind = '{kind}' ORDER BY seq")]

    def loop(self):
        provider = StubProvider(a_task())
        guard = SpawnGuard(blocked=("claude", "codex", "podman"))
        with no_agent_processes(guard), \
                patch.dict(sys.modules,
                           {"holophyte.board.linear_provider": provider}), \
                patch.object(holophyte.loop.implement, "agent",
                             FakeAgent(Commit(path="poetry.lock"))), \
                contextlib.redirect_stdout(io.StringIO()):
            holophyte.cli.operator.main(self.project, provider)

    def test_a_killed_adversary_container_is_kept_and_switches_to_the_fallback(self):
        calls = self.db.parent / "fallback-calls"
        script = self.db.parent / "adversary-fallback"
        script.write_text(
            f"#!{sys.executable}\nimport subprocess, sys\n"
            f"open({str(calls)!r}, 'a').write(sys.argv[-1][:31] + '\\n')\n"
            f"if sys.argv[-1] == {probes.REVIEW_PROBE_GOAL!r}:\n"
            " print('ready ' + subprocess.check_output("
            "['git', 'rev-parse', 'HEAD'], text=True).strip())\n"
            "else:\n print('Nothing broke.\\nADVERSARY: DONE')\n")
        script.chmod(0o755)
        self.configure("[review]\nadversary = true\n"
                       f'[agents]\nadversary_fallback = "{script}"\n')
        self.loop()
        switches = [json.loads(summary) for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'route_fallback'")]
        self.assertEqual([s["seat"] for s in switches], ["adversary"])
        self.assertIn("command failed (-15)", switches[0]["reason"])
        self.assertEqual(calls.read_text().splitlines(),
                         [probes.REVIEW_PROBE_GOAL[:31],
                          "You are a READ-ONLY adversarial"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        (boundary,) = self.payloads("review_boundary")
        self.assertEqual((boundary["role"], boundary["exit_status"],
                          boundary["line"]), ("adversary", -15, CUT))

    def test_an_unreadable_adversary_reply_without_a_fallback_fails_kept(self):
        self.configure("[review]\nadversary = true\n")
        with patch.dict(os.environ, {"ADVERSARY_EXIT": "0"}):
            self.loop()
        ((run_id, outcome, kind, reason),) = self.read(
            "SELECT id, outcome, failureKind, outcomeReason FROM runs")
        self.assertEqual((outcome, kind), ("failed", "review_route"))
        self.assertIn("reviewer route failed for adversary: "
                      "Codex emitted invalid JSONL", reason)
        self.assertEqual(len([p for p in self.payloads("agent_turn")
                              if p["role"] == "adversary"]), 1)
        (boundary,) = self.payloads("review_boundary")
        self.assertEqual((boundary["role"], boundary["exit_status"],
                          boundary["line"]), ("adversary", 0, CUT))
        _, detail = serve_runs.run_detail(self.project, str(run_id))
        (shown,) = [e["summary"] for e in detail["events"]
                    if e["kind"] == "review_boundary"]
        self.assertIn("exit 0", shown)
        self.assertIn(CUT, shown)


if __name__ == "__main__":
    import unittest
    unittest.main()

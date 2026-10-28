"""The `claude-sdk` critic: the real `claude_agent_sdk` drives a stub CLI.

The stub, in `sdk_critic_stub.py`, speaks the pinned SDK's stream-json
transport, so these tests guard Holophyte's side:
the options it passes, its reading of the `ResultMessage`, the stop at the
timeout, the credential environment and the probe's diagnostics.

Run: python3 -m unittest discover -s tests -p 'test_claude_sdk_critic.py' -v
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402
    AGENT_BINARIES,
    APPROVE,
    Commit,
    FakeAgent,
    SpawnGuard,
)
from loop_fixture import StubProvider  # noqa: E402
from sdk_critic_stub import SdkCriticCase, running  # noqa: E402

import holophyte.agents.fallback  # noqa: E402 - after the sys.path insert above
import holophyte.agents.roles  # noqa: E402 - after the sys.path insert above
import holophyte.loop.runs  # noqa: E402 - after the sys.path insert above
import holophyte.review.freshness  # noqa: E402 - after the sys.path insert above
from holophyte.agents.agent_routes import routes  # noqa: E402
from tests.test_critic import RealCritic, filed  # noqa: E402


class SdkCriticTurnTests(SdkCriticCase):
    def test_an_sdk_critic_runs_the_configured_cli_and_its_verdict_parks(self):
        self.stubbed()
        task = filed(13, 1)
        provider = StubProvider(task)

        admitted, _ = self.admits(task, provider)

        self.assertFalse(admitted)
        comments = dict(provider.comments)
        self.assertIn("critic: stale", comments["iss-131"])
        self.assertIn("already done by KO-5", comments["iss-131"])
        [call] = self.stub_calls()
        self.assertTrue(call["detached"])
        self.assertEqual(call["head"], self.git("rev-parse", "main").strip())
        self.assertFalse(Path(call["cwd"]).exists())
        argv = call["argv"]
        pairs = list(zip(argv, argv[1:]))
        self.assertIn(("--model", "sonnet"), pairs)
        self.assertIn(("--effort", "high"), pairs)
        disallowed = argv[argv.index("--disallowedTools") + 1]
        self.assertEqual(set(disallowed.split(",")),
                         {"Bash", "Edit", "Write", "NotebookEdit"})

    def test_an_sdk_critic_turn_records_its_usage_and_cost_on_the_run(self):
        self.stubbed("FRESHNESS: FRESH", cost=0.0123, num_turns=2, usage={
            "input_tokens": 100, "cache_read_input_tokens": 20,
            "output_tokens": 7})
        guard = SpawnGuard(blocked=tuple(name for name in AGENT_BINARIES
                                         if name != "claude"))
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(fake=RealCritic(FakeAgent(Commit("one"), APPROVE)),
                      provider=StubProvider(filed(13, 1)), guard=guard)

        turns = [json.loads(payload) for (payload,) in self.read(
            "SELECT e.payload FROM runEvents e JOIN runs r ON r.id = e.runId"
            " JOIN tickets t ON t.id = r.ticketId WHERE e.kind = 'agent_turn'"
            " AND t.linearIdentifier = 'KO-131' ORDER BY e.seq")]
        [turn] = [turn for turn in turns if turn["role"] == "critic"]
        self.assertEqual(turn["label"], "claude-sdk sonnet")
        self.assertEqual(turn["route"], "primary")
        self.assertEqual(turn["exit_status"], 0)
        self.assertEqual(turn["usage"], {"input_tokens": 120, "output_tokens": 7,
                                         "cost_usd": 0.0123, "num_turns": 2})

    def test_an_sdk_critic_past_its_timeout_stops_the_cli(self):
        pid_file = self.target.parent / "stub.pid"
        self.stubbed(pid=pid_file)
        task = filed(13, 1)

        started = time.monotonic()
        with patch.object(holophyte.review.freshness, "CRITIC_TIMEOUT", 2):
            admitted, printed = self.admits(task, StubProvider(task))

        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(admitted)
        self.assertIn("critic: its turn failed (timed out after 2 seconds)",
                      printed)
        self.assertFalse(running(int(pid_file.read_text())))

    def test_a_timed_out_sdk_critic_turn_leaves_no_cli_process_running(self):
        pids = self.target.parent / "stub.pids"
        self.stubbed(pids=pids)

        with patch.dict("os.environ"):
            os.environ.pop("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", None)
            with self.assertRaises(subprocess.TimeoutExpired):
                holophyte.agents.roles.critic_turn(
                    self.project, "GOAL", self.target, 1)

        started = [int(pid) for pid in pids.read_text().split()]
        self.assertTrue(started)
        self.assertEqual([pid for pid in started if running(pid)], [])

    def test_the_sdk_critic_inherits_the_login_or_takes_the_configured_key_file(
            self):
        key = self.target.parent / "anthropic-key"
        key.write_text("test-key-123\n")
        seen = []
        for extra in ("", f'api_key_file = "{key}"\n'
                          'base_url = "http://127.0.0.1:9"\n'):
            self.stubbed(extra=extra)
            with patch.dict("os.environ", {"SDK_CRITIC_MARKER": "inherited"}):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                holophyte.agents.roles.critic_turn(
                    self.project, "GOAL", self.target, 30)
            seen.append(self.stub_calls()[-1]["env"])

        self.assertEqual(seen[0]["SDK_CRITIC_MARKER"], "inherited")
        self.assertIsNone(seen[0]["ANTHROPIC_API_KEY"])
        self.assertEqual(seen[1]["ANTHROPIC_API_KEY"], "test-key-123")
        self.assertEqual(seen[1]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9")


class SdkCriticProbeTests(SdkCriticCase):
    def test_an_sdk_critic_probe_names_a_missing_key_file_and_the_switch_says_why(
            self):
        fallback = self.target.parent / "critic-fallback"
        fallback.write_text("#!/bin/sh\necho ready\n")
        fallback.chmod(0o755)
        missing = self.target.parent / "no-such-key"
        command = f"{fallback} --model sonnet"
        self.stubbed(extra=f'api_key_file = "{missing}"\n',
                     agents=f'[agents]\ncritic_fallback = "{command}"\n')

        started, printed = self.start(activate=False)

        self.assertTrue(started)
        failed = re.search(r"critic probe failed \(could not start: (.+)\): ",
                           printed)
        self.assertIsNotNone(failed, printed)
        self.assertIn(str(missing), failed.group(1))
        switched = re.search(r"critic route down \((.+)\); using fallback: ",
                             printed)
        self.assertIsNotNone(switched, printed)
        self.assertIn(failed.group(1), switched.group(1))
        self.assertEqual(routes(self.project).commands["critic"], command)

    def test_an_sdk_critic_probe_names_a_missing_login(self):
        login = "Not logged in · Please run /login"
        self.stubbed(login, is_error=True)

        started, printed = self.start()

        self.assertTrue(started)
        self.assertIn("critic probe failed (exit 1)", printed)
        self.assertIn(f"[holo2]   | {login}", printed)
        self.assertIn("[holo2] critic route down; claims skip the relevance check",
                      printed)
        self.assertTrue(routes(self.project).critic_failed)


if __name__ == "__main__":
    unittest.main()

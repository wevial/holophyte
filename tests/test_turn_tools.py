"""The `holo_run` tools an SDK critic turn hosts: `read_ticket` and
`report_freshness`, served by the real `claude_agent_sdk` and `mcp` server
to a stub CLI that calls them over the SDK's control protocol.

Run: python3 -m unittest discover -s tests -p 'test_turn_tools.py' -v
"""
import io
import json
import sys
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
from sdk_critic_stub import SdkCriticCase  # noqa: E402

from tests.test_critic import RealCritic, filed  # noqa: E402


def call(name, arguments):
    return ("tools/call", {"name": name, "arguments": arguments})


def said(answer):
    """A tools/call response's error flag and its text."""
    result = answer["result"]
    return (result.get("isError", False),
            "".join(part["text"] for part in result["content"]))


class TurnToolsTests(SdkCriticCase):
    def claimed_turn(self, task):
        """Run the loop over `task` with the real critic; the claimed run's
        critic `agent_turn` payload."""
        guard = SpawnGuard(blocked=tuple(name for name in AGENT_BINARIES
                                         if name != "claude"))
        provider = StubProvider(task)
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(fake=RealCritic(FakeAgent(Commit("one"), APPROVE)),
                      provider=provider, guard=guard)
        turns = [json.loads(payload) for (payload,) in self.read(
            "SELECT e.payload FROM runEvents e JOIN runs r ON r.id = e.runId"
            " JOIN tickets t ON t.id = r.ticketId WHERE e.kind = 'agent_turn'"
            " AND t.linearIdentifier = 'KO-131' ORDER BY e.seq")]
        [turn] = [turn for turn in turns if turn["role"] == "critic"]
        self.assertNotIn("iss-131", dict(provider.comments))
        return turn

    def test_an_sdk_critic_turn_hosts_exactly_the_two_run_tools(self):
        self.stubbed("FRESHNESS: FRESH", mcp=[("tools/list", {})])
        task = filed(13, 1)

        admitted, _ = self.admits(task, StubProvider(task))

        self.assertTrue(admitted)
        [listing] = self.mcp_answers()
        self.assertEqual(sorted(tool["name"] for tool in
                                listing["result"]["tools"]),
                         ["read_ticket", "report_freshness"])
        argv = self.stub_calls()[-1]["argv"]
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        self.assertIn("mcp__holo_run__read_ticket", allowed)
        self.assertIn("mcp__holo_run__report_freshness", allowed)

    def test_read_ticket_answers_only_the_judged_ticket(self):
        self.stubbed("FRESHNESS: FRESH", mcp=[
            call("read_ticket", {}), call("read_ticket", {"key": "KO-1"})])
        task = filed(13, 1, body="B", title="T")

        self.admits(task, StubProvider(task))

        first, second = map(said, self.mcp_answers())
        self.assertFalse(first[0])
        self.assertEqual(json.loads(first[1]),
                         {"key": "KO-131", "title": "T", "body": "B"})
        self.assertTrue(second[0])
        self.assertIn("'key'", second[1])

    def test_read_ticket_redacts_a_known_secret_as_the_brief_does(self):
        token = "ghp_turnToolsSecret0123456789"
        self.stubbed("FRESHNESS: FRESH", mcp=[call("read_ticket", {})])
        task = filed(13, 1, body=f"B uses {token}", title=f"T {token}")

        with patch.dict("os.environ", {"GH_TOKEN": token}):
            self.admits(task, StubProvider(task))

        [(error, text)] = map(said, self.mcp_answers())
        self.assertFalse(error)
        self.assertNotIn(token, text)
        self.assertEqual(json.loads(text)["key"], "KO-131")

    def test_a_reported_stale_verdict_parks_without_a_freshness_line(self):
        self.stubbed("Looked at main; it is done.", mcp=[call(
            "report_freshness",
            {"verdict": "stale", "reason": "already done by KO-5"})])
        task = filed(13, 1)
        provider = StubProvider(task)

        admitted, _ = self.admits(task, provider)

        self.assertFalse(admitted)
        comment = dict(provider.comments)["iss-131"]
        self.assertIn("critic: stale", comment)
        self.assertIn("already done by KO-5", comment)

    def test_a_reported_verdict_wins_over_the_reply_line_and_is_marked_on_the_turn(
            self):
        self.stubbed("Checked.\nFRESHNESS: STALE contradicted",
                     mcp=[call("report_freshness", {"verdict": "fresh"})])

        turn = self.claimed_turn(filed(13, 1))

        self.assertIs(turn["tool_verdict"], True)

    def test_report_freshness_refuses_a_missing_reason_and_a_second_report(self):
        self.stubbed("Not sure.", mcp=[
            call("report_freshness", {"verdict": "stale"}),
            call("report_freshness", {"verdict": "unsure", "reason": "r1"}),
            call("report_freshness", {"verdict": "fresh"})])
        task = filed(13, 1)
        provider = StubProvider(task)

        admitted, _ = self.admits(task, provider)

        missing, first, second = map(said, self.mcp_answers())
        self.assertTrue(missing[0])
        self.assertIn("reason", missing[1])
        self.assertFalse(first[0])
        self.assertTrue(second[0])
        self.assertIn("already reported", second[1])
        self.assertFalse(admitted)
        comment = dict(provider.comments)["iss-131"]
        self.assertIn("critic: unsure", comment)
        self.assertIn("r1", comment)

    def test_an_sdk_turn_that_reports_nothing_falls_back_to_the_freshness_line(
            self):
        self.stubbed("Checked.\nFRESHNESS: FRESH")

        turn = self.claimed_turn(filed(13, 1))

        self.assertIs(turn["tool_verdict"], False)


if __name__ == "__main__":
    unittest.main()

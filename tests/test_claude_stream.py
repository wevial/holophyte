"""A harness-built Claude implement turn streams its events, and a turn
that prints nothing for `[agents] turn_quiet_sec` ends as stalled."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import BRANCH, LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.review_round  # noqa: E402

READY = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "num_turns": 1, "result": "ready", "session_id": "S"})
INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "S",
                   "cwd": "/workspace", "tools": ["Edit"]})
EDIT = json.dumps({"type": "assistant", "session_id": "S",
                   "parent_tool_use_id": None,
                   "message": {"role": "assistant", "content": [
                       {"type": "tool_use", "id": "toolu_1", "name": "Edit",
                        "input": {"file_path": "stalled-edit.txt"}}]}})
WIP = "WIP: implementer stalled mid-edit (KO-131); not verified"


class StalledTurnTests(LoopFixture):
    def test_a_quiet_streaming_turn_is_killed_and_kept_as_stalled_wip(self):
        script = self.db.parent / "claude-harness"
        script.write_text(
            "#!/bin/sh\n"
            'for last; do :; done\n'
            'case "$last" in "Reply with the single word: ready") '
            f"echo '{READY}'; exit 0;; esac\n"
            f"echo '{INIT}'\necho '{EDIT}'\n"
            'echo "stalled edit" > stalled-edit.txt\nsleep 60\n')
        script.chmod(0o755)
        self.configure('[agents]\nturn_quiet_sec = 2\n'
                       '[agents.implementer]\nharness = "claude"\n'
                       f'[harnesses]\nclaude = "{script}"\n')
        provider = StubProvider(a_task())
        reviewer = FakeAgent(APPROVE)
        started = time.monotonic()
        with no_agent_processes(), \
                patch.dict(sys.modules, {"linear_provider": provider}), \
                patch.object(holophyte.loop.review_round, "agent", reviewer), \
                patch.object(holophyte.loop.adjudicate, "agent", reviewer):
            holophyte.cli.operator.main(self.project, provider)

        self.assertLess(time.monotonic() - started, 15)
        stalls = [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'turn_stalled'")]
        self.assertEqual(len(stalls), 1, stalls)
        self.assertEqual(stalls[0]["quiet_s"], 2)
        self.assertEqual(stalls[0]["last_tool"], "Edit")
        self.assertEqual(self.read("SELECT COUNT(*) FROM runEvents"
                                   " WHERE kind = 'turn_timeout'"), [(0,)])
        self.assertEqual(self.git("log", "-1", BRANCH, "--format=%s"), WIP)
        self.assertEqual(self.git("show", f"{BRANCH}:stalled-edit.txt"),
                         "stalled edit\n")
        ((kind, reason),) = self.read("SELECT failureKind, outcomeReason FROM runs")
        self.assertEqual(kind, "infra")
        self.assertTrue(reason.startswith(
            "implementer turn stalled: no stream event for 2 s"), reason)

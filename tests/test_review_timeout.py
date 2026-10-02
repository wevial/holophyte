"""A container review that runs past its time limit is a reviewer route failure."""
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
import review_runner
from holophyte.agents import probes, roles
from holophyte.agents.agent_routes import reset

# A probe is answered with the staged commit; a review takes the next numbered
# turn, and a turn reading SLEEP outlasts any review time limit.
DOCKER = f"""#!{sys.executable}
import json, os, subprocess, sys, time
args = sys.argv[1:]
if args[0] == "inspect":
    sys.exit(1)
if args[0] != "run":
    sys.exit(0)
turns, prompt, model = os.environ["REVIEW_TURNS"], args[-3], args[-2]
with open(os.path.join(turns, "log"), "a") as log:
    log.write(json.dumps([model, prompt == {probes.REVIEW_PROBE_GOAL!r}]) + "\\n")
print("PREFLIGHT_OK candidate=stand-in", file=sys.stderr, flush=True)
if prompt == {probes.REVIEW_PROBE_GOAL!r}:
    workspace = next(a.split(":")[0] for a in args if a.endswith(":/workspace:ro"))
    head = subprocess.check_output(["git", "-C", workspace, "rev-parse", "HEAD"],
                                   text=True).strip()
    reply = os.environ["REVIEW_EVENTS"].replace("APPROVAL", "ready " + head)
    print(reply)
    sys.exit(0)
count = os.path.join(turns, "count")
n = int(open(count).read()) + 1 if os.path.exists(count) else 1
open(count, "w").write(str(n))
turn = open(os.path.join(turns, str(n))).read()
if turn == "SLEEP":
    print("partial reasoning", flush=True)
    time.sleep(120)
print(turn)
"""

APPROVAL = ("Reviewed the diff; no blockers.\n"
            "CRITERION 1: met — tests/test_thing.py::test_it_works\n"
            "SCOPE scripted-1.txt: needed — the scripted work\n"
            "VERDICT: APPROVE")
PAIRS = ('[agents]\nreview_model = "gpt-6-astra"\nreview_effort = "medium"\n'
         'review_fallback_model = "gpt-5.6-sol"\n'
         'review_fallback_effort = "high"\n')


def events(text=APPROVAL):
    return "\n".join(json.dumps(event) for event in (
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "command_execution",
                                            "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "agent_message",
                                            "text": text}})) + "\n"


class ReviewTimeoutLoopTests(LoopFixture):
    def setUp(self):
        super().setUp()
        root = self.target.parent
        bin_dir, self.turns = root / "bin", root / "turns"
        bin_dir.mkdir()
        self.turns.mkdir()
        for name in ("docker", "codex", "codex-code-mode-host"):
            (bin_dir / name).write_text(
                DOCKER if name == "docker" else "#!/bin/sh\nexit 0\n")
            (bin_dir / name).chmod(0o755)
        (root / "auth.json").write_text("{}")
        for patcher in (
                patch.dict(os.environ, {
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "REVIEW_TURNS": str(self.turns),
                    "REVIEW_EVENTS": events("APPROVAL")}),
                patch.object(review_runner, "SCRATCH_ROOT", root / "reviews"),
                patch.object(review_runner, "CODEX_AUTH", root / "auth.json"),
                patch.object(roles, "REVIEW_TIMEOUT", 3)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(reset, self.project)

    def review(self, *turns):
        """Run the loop with a scripted implementer and the stand-in container
        reviewer answering `turns` in order."""
        for n, turn in enumerate(turns, 1):
            (self.turns / str(n)).write_text(turn)
        provider = StubProvider(a_task())
        guard = SpawnGuard(blocked=("claude", "codex", "podman"))
        with no_agent_processes(guard), \
                patch.dict(sys.modules, {"linear_provider": provider}), \
                patch.object(holophyte.loop.implement, "agent",
                             FakeAgent(Commit())), \
                contextlib.redirect_stdout(io.StringIO()):
            holophyte.cli.operator.main(self.project, provider)
        self.assertEqual((self.turns / "count").read_text(), str(len(turns)))

    def payloads(self, kind):
        return [json.loads(payload) for (payload,) in self.read(
            f"SELECT payload FROM runEvents WHERE kind = '{kind}' ORDER BY seq")]

    def review_turns(self):
        return [(p["route"], p["timed_out"]) for p in self.payloads("agent_turn")
                if p["role"] == "review"]

    def dispatched(self):
        """The model of every container review run that was not a probe."""
        lines = (self.turns / "log").read_text().splitlines()
        return [model for model, probe in map(json.loads, lines) if not probe]

    def crashes(self):
        return self.read("SELECT summary FROM runEvents WHERE kind = 'crash'")

    def test_a_timed_out_review_without_fallback_retries_once_and_approves(self):
        self.review("SLEEP", events())
        self.assertIn("reviewing -> merge_gate", self.transitions())
        self.assertEqual(self.crashes(), [])
        self.assertEqual(self.review_turns(),
                         [("primary", True), ("primary", False)])
        ((verdict,),) = self.read("SELECT verdict FROM reviewRounds")
        self.assertEqual(verdict, "pass")

    def test_a_timed_out_review_fails_over_to_the_configured_fallback(self):
        self.configure(PAIRS)
        self.review("SLEEP", events())
        self.assertIn("reviewing -> merge_gate", self.transitions())
        ((summary,),) = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'route_fallback'")
        switch = json.loads(summary)
        self.assertIn("timed out", switch["reason"])
        self.assertEqual(self.dispatched(), ["gpt-6-astra", "gpt-5.6-sol"])
        self.assertEqual(self.review_turns(),
                         [("primary", True), ("fallback", False)])

    def test_two_timed_out_reviews_fail_the_run_with_a_short_reason(self):
        self.review("SLEEP", "SLEEP")
        ((outcome, reason),) = self.read("SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertLess(len(reason), 300)
        self.assertIn("review timed out after 3s (twice)", reason)
        self.assertNotIn("docker", reason)
        self.assertNotIn("--volume", reason)
        self.assertEqual(self.crashes(), [])
        self.assertEqual(self.review_turns(),
                         [("primary", True), ("primary", True)])


if __name__ == "__main__":
    import unittest
    unittest.main()

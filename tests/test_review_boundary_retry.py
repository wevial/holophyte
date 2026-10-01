"""A review turn whose Codex output cannot be parsed keeps it and retries once."""
from tests.loop_fixture import Commit, LoopFixture, StubProvider, a_task  # isort: skip

import contextlib
import io
import json
import os
import sys
from unittest.mock import patch

from fake_agent import FakeAgent, SpawnGuard, no_agent_processes  # noqa: E402
from sweep_fixture import SweepTestCase  # noqa: E402

import holophyte.cli.operator
import holophyte.loop.implement
import review_runner
from holophyte.agents import probes, roles
from holophyte.agents.agent_routes import reset

# `run` prints the next numbered turn's output and the preflight marker the
# runner requires; `inspect` reports the removed container gone.
DOCKER = """#!/bin/sh
case "$1" in
  run)
    n=$(( $(cat "$REVIEW_TURNS/count" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$REVIEW_TURNS/count"
    cat "$REVIEW_TURNS/$n"
    echo "PREFLIGHT_OK candidate=stand-in" >&2 ;;
  inspect) exit 1 ;;
esac
exit 0
"""

APPROVAL = ("Reviewed the diff; no blockers.\n"
            "CRITERION 1: met — tests/test_thing.py::test_it_works\n"
            "SCOPE scripted-1.txt: needed — the scripted work\n"
            "VERDICT: APPROVE")
OFFENDING = "WARNING: proceeding, even though we could not update PATH"


def events(text=APPROVAL, noise=None):
    lines = [json.dumps({"type": "thread.started"}),
             json.dumps({"type": "item.completed", "item": {
                 "type": "command_execution", "exit_code": 0}})]
    if noise is not None:
        lines.append(noise)
    lines.append(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": text}}))
    return "\n".join(lines) + "\n"


class StandInReviewerLoopTests(LoopFixture):
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
                    "REVIEW_TURNS": str(self.turns)}),
                patch.object(review_runner, "SCRATCH_ROOT", root / "reviews"),
                patch.object(review_runner, "CODEX_AUTH", root / "auth.json")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def review(self, *outputs):
        """Run the loop with a scripted implementer and the default review
        route, whose container is the shim answering `outputs` in turn."""
        for n, output in enumerate(outputs, 1):
            (self.turns / str(n)).write_text(output)
        provider = StubProvider(a_task())
        guard = SpawnGuard(blocked=("claude", "codex", "podman"))
        with no_agent_processes(guard), \
                patch.dict(sys.modules, {"linear_provider": provider}), \
                patch.object(holophyte.loop.implement, "agent",
                             FakeAgent(Commit())), \
                contextlib.redirect_stdout(io.StringIO()):
            holophyte.cli.operator.main(self.project, provider)
        self.assertEqual((self.turns / "count").read_text().strip(),
                         str(len(outputs)))

    def payloads(self, kind):
        return [json.loads(payload) for (payload,) in self.read(
            f"SELECT payload FROM runEvents WHERE kind = '{kind}' ORDER BY seq")]

    def review_turns(self):
        return [p for p in self.payloads("agent_turn") if p["role"] == "review"]

    def test_an_unparsable_first_turn_is_kept_and_the_retry_approves(self):
        self.review(events(noise=OFFENDING), events())
        self.assertIn("reviewing -> merge_gate", self.transitions())
        self.assertEqual(len(self.review_turns()), 2)
        (boundary,) = self.payloads("review_boundary")
        self.assertEqual(boundary["line"], OFFENDING)
        ((verdict,),) = self.read("SELECT verdict FROM reviewRounds")
        self.assertEqual(verdict, "pass")

    def test_two_unparsable_turns_fail_the_run_with_both_kept(self):
        first, second = events(noise=OFFENDING), events(noise="partial {\"type")
        self.review(first, second)
        ((outcome, reason),) = self.read("SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn("reviewer route failed", reason)
        self.assertEqual(len(self.review_turns()), 2)
        kept = self.payloads("review_boundary")
        self.assertEqual([(p["line"], p["tail"], p["exit_status"]) for p in kept],
                         [(OFFENDING, first, 0), ('partial {"type', second, 0)])

    def test_a_long_line_with_control_characters_is_stored_clipped_and_clean(self):
        noise = "codex: \x1b[31mstream\x07 cut\t" + "x" * 5000
        output = events(noise=noise[:5000])
        self.review(output, output)
        kept = self.payloads("review_boundary")
        self.assertEqual(len(kept), 2)
        for payload in kept:
            self.assertLessEqual(len(payload["line"]), 300)
            self.assertTrue(payload["line"].isprintable(), payload["line"])
            self.assertTrue(payload["line"].startswith("codex: [31mstream cut"))
            self.assertLessEqual(len(payload["tail"]), 2048)
            self.assertTrue(output.endswith(payload["tail"]))


class ContainerFallbackTests(SweepTestCase):
    def setUp(self):
        from holophyte.loop.gates import sh

        super().setUp()
        sh(["git", "init", "-q", str(self.target)])
        sh(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
            "commit", "--allow-empty", "-qm", "base"], cwd=self.target)
        self.sha = sh(["git", "rev-parse", "HEAD"], cwd=self.target)
        self.addCleanup(reset, self.project)

    def test_a_configured_fallback_replaces_the_same_route_retry(self):
        self.configure('[agents]\nreview_model = "gpt-6-astra"\n'
                       'review_effort = "medium"\n'
                       'review_fallback_model = "gpt-5.6-sol"\n'
                       'review_fallback_effort = "high"\n')
        reviews = []

        def run_review(*, candidate_sha, prompt, model, effort, verdicts, **_):
            reviews.append((model, prompt))
            if model == "gpt-6-astra":
                review_runner.parse_codex_output(events(noise=OFFENDING), verdicts)
            return (f"ready {candidate_sha}" if prompt == probes.REVIEW_PROBE_GOAL
                    else "VERDICT: PASS")

        run = self.a_run()
        with patch.object(roles.review_runner, "run_review",
                          side_effect=run_review), \
                contextlib.redirect_stdout(io.StringIO()):
            reply = roles.agent(self.project, "review", "judge", self.target,
                                base_sha=self.sha, candidate_sha=self.sha,
                                conn=self.conn, run_id=run)
        self.assertEqual(reply, "VERDICT: PASS")
        self.assertEqual(reviews, [("gpt-6-astra", "judge"),
                                   ("gpt-5.6-sol", probes.REVIEW_PROBE_GOAL),
                                   ("gpt-5.6-sol", "judge")])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action='route_fallback'"
        ).fetchone()[0], 1)


if __name__ == "__main__":
    import unittest
    unittest.main()

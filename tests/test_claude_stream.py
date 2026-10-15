"""A harness-built Claude implement turn streams its events, and a turn
that prints nothing for `[agents] turn_quiet_sec` ends as stalled."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, FakeAgent, no_agent_processes  # noqa: E402
from isolation_fixture import IsolationCase  # noqa: E402
from loop_fixture import (  # noqa: E402
    BRANCH,
    VALID_BODY,
    LoopFixture,
    StubProvider,
    a_task,
)
from sweep_fixture import SweepTestCase  # noqa: E402

import holophyte.agents.probes  # noqa: E402
import holophyte.agents.roles  # noqa: E402
import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.isolation import launcher  # noqa: E402

READY = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "num_turns": 1, "result": "ready", "session_id": "S"})
INIT = json.dumps({"type": "system", "subtype": "init", "session_id": "S",
                   "cwd": "/workspace", "tools": ["Edit"]})
EDIT = json.dumps({"type": "assistant", "session_id": "S",
                   "parent_tool_use_id": None,
                   "message": {"role": "assistant", "content": [
                       {"type": "tool_use", "id": "toolu_1", "name": "Edit",
                        "input": {"file_path": "stalled-edit.txt"}}]}})
NOT_NOW = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "num_turns": 1, "result": "not now", "session_id": "S"})
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
        self.assertEqual(self.git("log", "-1", BRANCH, "--format=%s").strip(), WIP)
        self.assertEqual(self.git("show", f"{BRANCH}:stalled-edit.txt"),
                         "stalled edit\n")
        ((kind, reason),) = self.read("SELECT failureKind, outcomeReason FROM runs")
        self.assertEqual(kind, "infra")
        self.assertTrue(reason.startswith(
            "implementer turn stalled: no stream event for 2 s"), reason)

    def test_a_profile_probe_reads_the_result_not_a_ready_word_elsewhere(self):
        script = self.db.parent / "claude-harness"
        script.write_text(
            "#!/bin/sh\n"
            'for last; do :; done\n'
            'case "$last $*" in "Reply with the single word: ready"*sonnet*) '
            f"echo '{COMMANDS_CHANGED}'; echo '{NOT_NOW}'; exit 0;; "
            f"\"Reply with the single word: ready\"*) echo '{READY}'; exit 0;; esac\n"
            f"echo '{INIT}'\nsleep 60\n")
        script.chmod(0o755)
        self.configure('[agents]\nturn_quiet_sec = 2\n'
                       '[agents.implementer]\nharness = "claude"\n'
                       '[agents.implementer_profiles.sonnet]\nharness = "claude"\n'
                       'model = "sonnet"\n'
                       f'[harnesses]\nclaude = "{script}"\n')
        body = VALID_BODY.replace("Depends on: none\n",
                                  "Depends on: none\nImplementer: sonnet\n")
        provider = StubProvider(dict(a_task(), body=body))
        with no_agent_processes(), \
                patch.dict(sys.modules, {"linear_provider": provider}):
            holophyte.cli.operator.main(self.project, provider)
        [(payload,)] = self.read("SELECT payload FROM runEvents"
                                 " WHERE kind = 'implementer_profile'")
        event = json.loads(payload)
        self.assertEqual((event["profile"], event["requested"]), (None, "sonnet"))
        self.assertIn("probe failed", event["why"])


FIXTURE = HERE / "fixtures" / "claude_stream" / "turn.jsonl"
STREAM_FLAGS = ["--output-format", "stream-json", "--verbose"]
LIMITED = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "num_turns": 1,
                      "result": "You've hit your limit · resets 3pm"})
DONE = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                   "num_turns": 1, "result": "finished", "session_id": "S"})
STATUS = json.dumps({"type": "system", "subtype": "status", "session_id": "S"})
COMMANDS_CHANGED = next(line for line in FIXTURE.read_text().splitlines()
                        if '"commands_changed"' in line)


class StreamTurnCase(SweepTestCase):
    """A real git checkout and a real `[harnesses] claude` script."""

    def setUp(self):
        super().setUp()
        for args in (["init", "-q", "-b", "main"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                      "commit", "--allow-empty", "-qm", "base"]):
            subprocess.run(["git", *args], cwd=self.target, check=True)
        self.run_id = self.a_run()
        self.argv_log = self.root / "argv"
        self.fallback_log = self.root / "fallback-calls"

    def harness(self, body, agents=""):
        script = self.root / "claude-harness"
        script.write_text("#!/bin/sh\n"
                          f'printf "%s\\0" "$@" > "{self.argv_log}"\n' + body)
        script.chmod(0o755)
        self.configure(f"[agents]\n{agents}[agents.implementer]\n"
                       f'harness = "claude"\n[harnesses]\nclaude = "{script}"\n')
        self.addCleanup(reset, self.project)

    def fallback(self):
        script = self.root / "fallback-implementer"
        script.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{self.fallback_log}"\n'
            'for last; do :; done\n'
            'case "$last" in "Reply with the single word: ready") echo ready;;'
            " *) echo completed;; esac\n")
        script.chmod(0o755)
        return f'implementer_fallback = "{script}"\n'

    def argv(self):
        return self.argv_log.read_text().split("\0")[:-1]

    def events(self, kind):
        return [json.loads(payload) if payload else None
                for (payload,) in self.conn.execute(
                    "SELECT payload FROM runEvents WHERE kind = ?", (kind,))]

    def timed(self, budget_min=25):
        return holophyte.loop.implement._timed(
            self.project, self.conn, self.run_id, 60, self.target, budget_min,
            "implement the thing")


class StreamDecodingTests(StreamTurnCase):
    def test_the_captured_stream_yields_its_answer_and_summed_usage(self):
        self.harness(f'cat "{FIXTURE}"\n')
        output, timed_out = self.timed()
        self.assertFalse(timed_out)
        argv = self.argv()
        at = argv.index("--output-format")
        self.assertEqual(argv[at:at + 3], STREAM_FLAGS)
        self.assertNotIn("json", argv)
        self.assertEqual(output, "The subagent finished and replied done.")
        [turn] = self.events("agent_turn")
        self.assertEqual(turn["usage"], {
            "input_tokens": 4 + 16269 + 33363 + 2 + 1034 + 27361,
            "output_tokens": 456 + 277, "cost_usd": 0.00715, "num_turns": 4})

    def test_an_error_result_on_the_stream_activates_the_fallback(self):
        self.harness(f"cat <<'EOF'\n{STATUS}\n{LIMITED}\nEOF\n",
                     agents=self.fallback())
        output = holophyte.agents.roles.agent(
            self.project, "implement", "work", self.target, conn=self.conn,
            run_id=self.run_id)
        self.assertEqual(output, "completed")
        self.assertEqual(len(self.events("route_fallback")), 1)

    def test_a_transport_error_beside_stream_lines_is_the_output_and_retries(self):
        count = self.root / "count"
        self.harness(
            f'n=$(( $(cat "{count}" 2>/dev/null || echo 0) + 1 ))\n'
            f'echo "$n" > "{count}"\n'
            f"if [ \"$n\" = 1 ]; then echo '{INIT}'; echo '{STATUS}';"
            " echo 'Error: ECONNRESET' >&2; exit 1; fi\n"
            f"echo '{DONE}'\n")
        with patch.object(holophyte.loop.implement, "sleep"):
            output, timed_out = holophyte.loop.implement._transport_timed(
                self.project, self.conn, self.run_id, 60, self.target, 25,
                "implement the thing")
        self.assertEqual((str(output), timed_out), ("finished", False))
        self.assertEqual(len(self.events("transport_retry")), 1)
        (payload,), = self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'implementer_output'")
        self.assertEqual(payload, "Error: ECONNRESET")


class QuietWatchdogTests(StreamTurnCase):
    def test_a_stalled_turn_does_not_activate_the_fallback(self):
        self.harness(f"echo '{INIT}'\nsleep 30\n",
                     agents="turn_quiet_sec = 2\n" + self.fallback())
        started = time.monotonic()
        output, timed_out = self.timed()
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(timed_out)
        self.assertEqual(len(self.events("turn_stalled")), 1)
        self.assertEqual(self.events("route_fallback"), [])
        self.assertFalse(self.fallback_log.exists())

    def test_a_turn_that_closes_its_output_and_hangs_still_stalls(self):
        self.harness(f"echo '{INIT}'\nexec >&- 2>&-\nsleep 30\n",
                     agents="turn_quiet_sec = 2\n")
        started = time.monotonic()
        output, timed_out = self.timed()
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(timed_out)
        self.assertEqual(len(self.events("turn_stalled")), 1)
        self.assertEqual(self.events("turn_timeout"), [])

    def test_a_turn_printing_every_second_outlives_a_short_quiet_period(self):
        self.harness(f"for i in 1 2 3 4 5; do echo '{STATUS}'; sleep 1; done\n"
                     f"echo '{DONE}'\n", agents="turn_quiet_sec = 2\n")
        output, timed_out = self.timed()
        self.assertEqual((str(output), timed_out), ("finished", False))
        self.assertEqual(self.events("turn_stalled"), [])

    def test_a_turn_that_exits_while_a_child_holds_its_pipe_is_not_stalled(self):
        self.harness(f"echo '{DONE}'\n(sleep 8 &)\nexit 0\n",
                     agents="turn_quiet_sec = 2\n")
        output, timed_out = self.timed()
        self.assertEqual((str(output), timed_out), ("finished", False))
        self.assertEqual(self.events("turn_stalled"), [])

    def test_a_zero_quiet_period_leaves_a_silent_turn_to_the_turn_cap(self):
        self.harness(f"echo '{INIT}'\nsleep 90\n",
                     agents="turn_quiet_sec = 0\nturn_cap_min = 1\n")
        output, timed_out = self.timed()
        self.assertTrue(timed_out)
        self.assertEqual(len(self.events("turn_timeout")), 1)
        self.assertEqual(self.events("turn_stalled"), [])


class StreamProbeTests(StreamTurnCase):
    def probe_answering(self, answer):
        reply = json.dumps({"type": "result", "subtype": "success",
                            "is_error": False, "result": answer})
        self.harness(f"echo '{INIT}'\necho '{COMMANDS_CHANGED}'\necho '{reply}'\n")
        return holophyte.agents.probes.probe_implementer(self.project)

    def test_a_ready_word_outside_the_result_fails_the_probe(self):
        self.assertIn("ready", COMMANDS_CHANGED)
        self.assertFalse(self.probe_answering("not now").ok)

    def test_a_ready_result_passes_the_probe(self):
        self.assertTrue(self.probe_answering("ready").ok)


class OpaqueImplementerTests(StreamTurnCase):
    def test_a_session_regex_implementer_reads_as_today_with_no_watchdog(self):
        session = "0b5c1f4e-7d2a-4c33-9e1f-2a6b8c4d5e6f"
        script = self.root / "claude-implement"
        script.write_text("#!/bin/sh\n"
                          f'printf "%s\\0" "$@" > "{self.argv_log}"\n'
                          f'echo "session id: {session}"\necho working\n'
                          "sleep 4\necho done\n")
        script.chmod(0o755)
        self.configure(f'[agents]\nimplementer = "{script}"\n'
                       "implementer_session = 'session id: (\\S+)'\n"
                       "turn_quiet_sec = 2\n")
        self.addCleanup(reset, self.project)
        output, timed_out = self.timed()
        self.assertFalse(timed_out)
        self.assertEqual(str(output),
                         f"session id: {session}\nworking\ndone")
        self.assertFalse(any("stream-json" in arg for arg in self.argv()))
        self.assertEqual(self.conn.execute(
            "SELECT providerSessionId FROM runs WHERE id = ?",
            (self.run_id,)).fetchone(), (session,))
        self.assertEqual(self.events("turn_stalled"), [])


class ContainerDispatchTests(IsolationCase):
    def launched_kwargs(self, implementer):
        main, worktree = self.make_worktree()
        self.target.path = main
        self.table["agents"].update(implementer=implementer, turn_quiet_sec=2,
                                    implementer_isolation="container")
        with patch.object(launcher, "image_ready"), \
                patch.object(launcher.review_runner, "_remove_container"), \
                patch.object(launcher, "run_capped",
                             return_value=(0, DONE)) as run:
            holophyte.agents.roles.agent(self.target, "implement", "task",
                                         worktree, timeout=17)
        return run.call_args.kwargs

    def test_a_claude_harness_turn_passes_its_quiet_period_to_the_container(self):
        self.assertEqual(self.launched_kwargs({"harness": "claude"})["quiet"], 2)

    def test_an_opaque_turn_passes_no_quiet_period(self):
        self.assertNotIn("quiet", self.launched_kwargs("agent-cli"))

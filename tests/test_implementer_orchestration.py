"""The implementer's orchestration mode: the brief paragraph it adds, the
config refusals, the run event and the unchanged resume command line.

Run: python3 -m unittest discover -s tests -p 'test_implementer_orchestration.py'
"""
import json
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import holophyte.agents.probes
import holophyte.agents.roles
import holophyte.board.projection
import holophyte.cli.operator
import holophyte.config.checks
import holophyte.config.project
import holophyte.loop.adjudicate
import holophyte.loop.implement
import holophyte.loop.review_round
import holophyte.question_cli
import store
import store.tickets
from holophyte.agents.fix_session import resume_argv
from holophyte.agents.probes import PROBE_GOAL
from holophyte.loop.runs import open_store
from tests.failure_triage_fixture import FakeClaude, question_guard
from tests.fake_agent import APPROVE, REQUEST_CHANGES, Commit, FakeAgent
from tests.loop_fixture import VALID_BODY, LoopFixture, StubProvider, a_task

ROOT = Path(__file__).resolve().parents[1]

SUBAGENT_PHRASES = (
    "may go to subagents you start with your own tools",
    "do not commit, branch, push or open worktrees",
    "you integrate their work and commit it",
    "have an independent subagent review the whole change against the "
    "acceptance criteria",
)
WORKFLOW_OPT_IN = ("The maintainer has opted this ticket in to a Claude Code "
                   "multi-agent workflow")

TODAYS_BRIEF = (
    "Implement this task in this repo:\n\nadd a thing\n\n# Add a thing\n\n"
    "## Summary\n\nThe thing, added.\n\n## What / Why / How\n\n"
    "**What:** Add the thing.\n\n**Why:** The thing is wanted.\n\n"
    "**How:** Write the thing.\n\n## In scope\n\n* The thing.\n\n"
    "## Out of scope\n\n* Everything else.\n\n## Acceptance criteria\n\n"
    "- [ ] Given the thing, when it runs, then it works (a test witnesses "
    "this)\n\n## Verify command(s)\n\n```\necho ok\n```\n\n"
    "## Implementation notes\n\n* None.\n\n## Estimate & dependencies\n\n"
    "Estimate: 5 min · Depends on: none\n\n## Open questions\n\n* None\n\n"
    "These verify commands must pass before review and again before merge:"
    "\n\necho ok\n\nThe full unit suite runs as a pull request check; do not "
    "run it in the worktree. Run only the commands listed above.\n\n"
    "The ticket above is the contract, acceptance criteria included; the "
    "task is done only when they hold. Commit your work with a clear "
    "message. Stay strictly on-scope; do not expand the task. Commit "
    "messages carry no tool attribution or co-author lines for an AI.\n\n"
    "Include in your reply, before any closing line asked for below, one "
    "line naming the riskiest thing the change touches:\n"
    "BLAST RADIUS: high|medium|low — reason\n\n"
    "If the ticket reports a defect and a test built from the real data "
    "shape passes on the unchanged code, the defect did not reproduce: "
    "commit the test(s) only, change no other code, and end your reply with "
    "exactly this line:\nOUTCOME: NOT_REPRODUCED\n\n"
    "Acceptance criterion ids:\nC1. Given the thing, when it runs, then it "
    "works\n\n"
    "When an acceptance criterion cannot be met as written (the data or "
    "contract it names lacks a field, two criteria contradict each other, or "
    "a repository pin or rule forbids it), do not substitute different "
    "behaviour or wording, and do not write a test that asserts a substitute."
    " Do the rest of the ticket, commit it, and end the reply with one line "
    "per such criterion, using its id above, with a one-line reason naming "
    "what is missing or what contradicts it:\nCONTRADICTION: criterion C1 — reason")

CODEX = '[agents.implementer]\nharness = "codex"\n'
CLAUDE = '[agents.implementer]\nharness = "claude"\n'


def ticket_line(mode):
    return VALID_BODY.replace("Depends on: none\n",
                              f"Depends on: none\nOrchestration: {mode}\n")


class OrchestrationBriefTests(LoopFixture):
    def implement_goal(self, config, body=VALID_BODY):
        self.configure(config)
        with patch.object(holophyte.agents.probes, "run_capped",
                          return_value=(0, "ready")):
            fake, _ = self.loop(Commit("the thing", path="app.txt"), APPROVE,
                                provider=StubProvider(dict(a_task(), body=body)))
        self.assertEqual(fake.roles[0], "implement")
        return fake.turns[0].goal

    def events(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'orchestration'")]

    def assert_subagents_brief(self, goal):
        for phrase in SUBAGENT_PHRASES:
            self.assertIn(phrase, goal)

    def test_codex_subagents_brief_carries_subagents_not_workflow(self):
        goal = self.implement_goal(CODEX + 'orchestration = "subagents"\n')
        self.assert_subagents_brief(goal)
        self.assertNotIn(WORKFLOW_OPT_IN, goal)
        self.assertNotIn("Workflow tool", goal)

    def test_claude_workflow_brief_carries_the_explicit_opt_in(self):
        goal = self.implement_goal(CLAUDE + 'orchestration = "workflow"\n')
        self.assertIn(WORKFLOW_OPT_IN, goal)
        self.assertIn("If the Workflow tool is unavailable, orchestrate "
                      "subagents instead.", goal)

    def test_no_orchestration_key_keeps_todays_brief(self):
        self.assertEqual(self.implement_goal(CLAUDE), TODAYS_BRIEF)

    def test_orchestration_off_keeps_todays_brief(self):
        self.assertEqual(self.implement_goal(CLAUDE + 'orchestration = "off"\n'),
                         TODAYS_BRIEF)

    def test_ticket_subagents_line_overrides_a_project_set_off(self):
        goal = self.implement_goal(CLAUDE + 'orchestration = "off"\n',
                                   ticket_line("subagents"))
        self.assert_subagents_brief(goal)
        self.assertEqual(self.events(), [{"mode": "subagents",
                                          "requested": "subagents",
                                          "source": "ticket"}])

    def test_ticket_off_line_overrides_a_project_set_subagents(self):
        goal = self.implement_goal(CODEX + 'orchestration = "subagents"\n',
                                   ticket_line("off"))
        self.assertNotIn("subagent", goal)

    def test_ticket_workflow_on_codex_runs_as_subagents(self):
        goal = self.implement_goal(CODEX, ticket_line("workflow"))
        self.assert_subagents_brief(goal)
        self.assertNotIn(WORKFLOW_OPT_IN, goal)
        self.assertEqual(self.events(), [{"mode": "subagents",
                                          "requested": "workflow",
                                          "source": "ticket"}])

    def test_ticket_workflow_beside_a_codex_fallback_reaches_neither_route(self):
        self.configure(CLAUDE + '[agents]\nimplementer_fallback = "codex exec"\n')
        implement_calls = []

        def runner(cmd, cwd, timeout, **kwargs):
            if "ready" in cmd[-1]:
                return 0, "ready"
            implement_calls.append(cmd)
            if cmd[0] == "claude":
                return 1, "You've hit your limit"
            Commit("the thing", path="app.txt").play(Path(cwd), 1)
            return 0, "done"

        reviewer = FakeAgent(APPROVE)

        def dispatch(target, role, goal, cwd, **kwargs):
            if role != "implement":
                return reviewer(target, role, goal, cwd, **kwargs)
            return holophyte.agents.roles.agent(target, role, goal, cwd,
                                                **kwargs)

        with patch.object(holophyte.agents.roles, "run_capped", runner), \
                patch.object(holophyte.agents.probes, "run_capped", runner):
            self.loop(fake=dispatch, provider=StubProvider(
                dict(a_task(), body=ticket_line("workflow"))))
        self.assertEqual([cmd[0] for cmd in implement_calls], ["claude", "codex"])
        for cmd in implement_calls:
            self.assert_subagents_brief(cmd[-1])
            self.assertNotIn("Workflow tool", cmd[-1])
        self.assertEqual(self.events(), [{"mode": "subagents",
                                          "requested": "workflow",
                                          "source": "ticket"}])

    def test_project_subagents_run_records_mode_and_source(self):
        self.implement_goal(CODEX + 'orchestration = "subagents"\n')
        self.assertEqual(self.events(), [{"mode": "subagents",
                                          "requested": "subagents",
                                          "source": "project"}])

    def test_unset_run_records_off_from_default(self):
        self.implement_goal("")
        self.assertEqual(self.events(), [{"mode": "off", "requested": "off",
                                          "source": "default"}])


class OrchestrationConfigTests(LoopFixture):
    def check(self, config):
        self.configure(config)
        holophyte.config.checks.check_document(self.project)

    def test_startup_refuses_a_bad_value_mode_or_seat(self):
        for config, message in (
            (CLAUDE + 'orchestration = "swarm"\n',
             r"\[agents\.implementer\] orchestration must be one of"),
            (CODEX + 'orchestration = "workflow"\n',
             r"\[agents\.implementer\] orchestration: harness 'codex'"),
            ('[agents.reviewer]\nharness = "codex"\n'
             'orchestration = "subagents"\n',
             r"\[agents\.reviewer\] orchestration: unknown key"),
        ):
            with self.subTest(config=config):
                with self.assertRaisesRegex(SystemExit, message):
                    self.check(config)
        self.check(CODEX + 'orchestration = "subagents"\n')
        self.check(CLAUDE + 'orchestration = "workflow"\n')

    def test_workflow_beside_a_non_claude_fallback_is_refused(self):
        workflow = CLAUDE + 'orchestration = "workflow"\n'
        with self.assertRaisesRegex(
                SystemExit, r"orchestration.*implementer_fallback"):
            self.check('[agents]\nimplementer_fallback = "codex exec"\n'
                       + workflow)
        self.check('[agents]\nimplementer_fallback = "claude -p"\n' + workflow)

    def test_profile_workflow_beside_a_non_claude_fallback_is_refused(self):
        with self.assertRaisesRegex(
                SystemExit, r"\[agents\.implementer_profiles\.big\] "
                            r"orchestration.*implementer_fallback"):
            self.check('[agents]\nimplementer_fallback = "codex exec"\n'
                       '[agents.implementer_profiles.big]\nharness = "claude"\n'
                       'orchestration = "workflow"\n')

    def test_workflow_accepts_a_quoted_claude_fallback(self):
        workflow = CLAUDE + 'orchestration = "workflow"\n'
        for fallback in ('\'"claude" -p\'',
                         '\'"/opt/claude tools/claude" -p\''):
            with self.subTest(fallback=fallback):
                self.check(f'[agents]\nimplementer_fallback = {fallback}\n'
                           + workflow)


class OrchestrationResumeTests(LoopFixture):
    def resume(self, config):
        self.configure(config + '[loop]\nfix_session = "resume"\n')
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        project_id = store.tickets.ensure_project(conn, StubProvider.TEAM,
                                                  self.target)
        ticket = holophyte.board.projection.mirror_task(conn, project_id,
                                                        a_task())
        run = store.claim(conn, project_id, ticket)
        session = "0b7e2a1c-5d3f-4e8a-9c6b-2f1d0e9a8b7c"
        store.record_agent_session(conn, run, session, "implement", "primary")
        conn.commit()
        argv, reason = resume_argv(self.project, conn, run)
        self.assertIsNone(reason)
        self.assertIn(session, argv)
        store.release(conn, run, "failed", "test over")
        conn.commit()
        return argv

    def test_orchestration_never_reaches_the_resume_argv(self):
        with_key = self.resume(CODEX + 'orchestration = "subagents"\n')
        without = self.resume(CODEX)
        self.assertEqual(with_key, without)


PROFILES = ('[agents.implementer]\nharness = "claude"\nmodel = "opus"\n'
            '[agents.implementer_profiles.sonnet]\nharness = "claude"\n'
            'model = "sonnet"\n')


def implementer_line(name):
    return VALID_BODY.replace("Depends on: none\n",
                              f"Depends on: none\nImplementer: {name}\n")


@contextmanager
def profile_routes(db, reviews, probe=(0, "ready"), pause=False, down=(),
                   on_probe=None):
    """Implement and fix turns run through the real role dispatch, so their
    `agent_turn` labels come from the route the run resolved. A model in
    `down` is out of quota on every turn after its probe."""
    def runner(cmd, cwd, timeout, **kwargs):
        if cmd[-1] == PROBE_GOAL:
            if on_probe is not None and "sonnet" in cmd:
                on_probe()
            return probe if "sonnet" in cmd else (0, "ready")
        if any(model in cmd for model in down):
            return 1, "You've hit your limit"
        Commit("the thing", path=f"work-{uuid.uuid4().hex}.txt").play(
            Path(cwd), 0)
        if pause:
            conn = store.open(db)
            run, = conn.execute("SELECT id FROM runs WHERE endedAt IS NULL"
                                ).fetchone()
            store.pause(conn, run, "reboot writer")
            conn.close()
        return 0, "done"

    reviewer = FakeAgent(*reviews)

    def dispatch(target, role, goal, cwd, **kwargs):
        if role != "implement":
            return reviewer(target, role, goal, cwd, **kwargs)
        return holophyte.agents.roles.agent(target, role, goal, cwd, **kwargs)

    with patch.object(holophyte.agents.roles, "run_capped", runner), \
            patch.object(holophyte.agents.probes, "run_capped", runner):
        yield dispatch


def resume_in_this_process(target, body):
    """The loop's entry in a fresh interpreter, against a fixture's store."""
    project = holophyte.config.project.Project.locate(Path(target))
    provider = StubProvider(dict(a_task(), body=body))
    with patch("holophyte.loop.pool_handoff.factory_checkout",
               return_value=Path(target)), \
            profile_routes(project.store_path,
                           (REQUEST_CHANGES, APPROVE)) as dispatch, \
            patch.dict(sys.modules, {"linear_provider": provider}), \
            patch.object(holophyte.loop.implement, "agent", dispatch), \
            patch.object(holophyte.loop.review_round, "agent", dispatch), \
            patch.object(holophyte.loop.adjudicate, "agent", dispatch):
        return holophyte.cli.operator.main(project, provider)


class ProfileRunFixture(LoopFixture):
    def run_ticket(self, body, *reviews, probe=(0, "ready"), pause=False,
                   down=(), on_probe=None, guard=None):
        with profile_routes(self.db, reviews, probe, pause, down,
                            on_probe) as dispatch:
            self.loop(fake=dispatch, guard=guard, provider=StubProvider(
                dict(a_task(), body=body)))

    def turns(self):
        return [json.loads(payload)["label"] for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'agent_turn' "
            "ORDER BY id")]

    def profiles(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'implementer_profile'")]


class ImplementerProfileTests(ProfileRunFixture):

    def pause_on_sonnet_and_resume(self):
        from holophyte.loop.stop import command
        self.configure(PROFILES)
        self.run_ticket(implementer_line("sonnet"), pause=True)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("paused",)])
        command(self.project, a_task()["id"], None, resume=True)

    def test_ticket_profile_routes_the_implement_and_fix_turns(self):
        self.configure(PROFILES)
        self.run_ticket(implementer_line("sonnet"), REQUEST_CHANGES, APPROVE)
        self.assertEqual(self.turns(), ["claude sonnet", "claude sonnet"])
        self.assertEqual(self.profiles(), [{
            "profile": "sonnet", "source": "ticket", "requested": "sonnet",
            "why": None, "label": "claude sonnet"}])

    def test_ticket_without_a_line_runs_on_the_default(self):
        self.configure(PROFILES)
        self.run_ticket(VALID_BODY, APPROVE)
        self.assertEqual(self.turns(), ["claude opus"])
        self.assertEqual(self.profiles(), [{
            "profile": None, "source": "default", "requested": None,
            "why": None, "label": "claude opus"}])

    def test_unknown_profile_runs_on_the_default(self):
        self.configure(PROFILES)
        self.run_ticket(implementer_line("haiku"), APPROVE)
        self.assertEqual(self.turns(), ["claude opus"])
        self.assertEqual(self.profiles(), [{
            "profile": None, "source": "default", "requested": "haiku",
            "why": "unknown profile", "label": "claude opus"}])

    def test_profile_whose_probe_fails_runs_on_the_default(self):
        self.configure(PROFILES)
        self.run_ticket(implementer_line("sonnet"), APPROVE,
                        probe=(1, "sonnet route refused"))
        self.assertEqual(self.turns(), ["claude opus"])
        event, = self.profiles()
        self.assertEqual((event["profile"], event["requested"]),
                         (None, "sonnet"))
        self.assertIn("probe failed (exit 1)", event["why"])
        self.assertIn("sonnet route refused", event["why"])

    def test_continuation_in_a_new_process_keeps_the_profile(self):
        self.pause_on_sonnet_and_resume()
        child = subprocess.run(
            [sys.executable, "-c",
             "import sys\n"
             "from tests.test_implementer_orchestration import "
             "resume_in_this_process\n"
             f"sys.exit(resume_in_this_process({str(self.target)!r}, "
             f"{VALID_BODY!r}))"],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertEqual(self.turns(), ["claude sonnet", "claude sonnet"])
        self.assertEqual(len(self.profiles()), 1)

    def test_continuation_whose_profile_left_the_config_stops(self):
        self.pause_on_sonnet_and_resume()
        self.configure('[agents.implementer]\nharness = "claude"\n'
                       'model = "opus"\n')
        self.run_ticket(VALID_BODY, REQUEST_CHANGES, APPROVE)
        self.assertEqual(self.turns(), ["claude sonnet"])
        self.assertEqual(len(self.profiles()), 1)
        self.assertEqual(self.read(
            "SELECT outcome, failureKind FROM runs ORDER BY id")[-1],
            ("failed", "infra"))

    def test_default_route_fallback_leaves_a_healthy_profile_in_place(self):
        self.configure(PROFILES + '[agents]\nimplementer_fallback = "codex exec"\n')
        down_at_startup = (1, "You've hit your limit")
        with patch.object(holophyte.cli.operator, "probe_implementer",
                          return_value=holophyte.agents.probes.ProbeResult(
                              ["claude", "--model", "opus"], *down_at_startup,
                              timeout=90)):
            self.run_ticket(implementer_line("sonnet"), APPROVE)
        self.assertEqual(self.turns(), ["claude sonnet"])
        self.assertEqual(self.profiles()[0]["label"], "claude sonnet")

    def test_profile_outage_switch_holds_for_its_turns_not_the_default(self):
        self.configure(PROFILES + '[agents]\nimplementer_fallback = "codex exec"\n')
        tickets = (dict(a_task(1), body=implementer_line("sonnet")),
                   dict(a_task(2), body=VALID_BODY))
        with profile_routes(self.db, (REQUEST_CHANGES, APPROVE, APPROVE),
                            down=("sonnet",)) as dispatch:
            self.loop(fake=dispatch, provider=StubProvider(*tickets))
        self.assertEqual(self.turns(),
                         ["claude sonnet", "codex", "codex", "claude opus"])

    def test_a_slow_profile_probe_keeps_the_run_heartbeating(self):
        self.configure(PROFILES + '[supervisor]\nheartbeat_stale_min = 0.002\n')
        beats = []

        def slow_probe():
            conn = store.open(self.db)
            try:
                for _ in range(2):
                    beats.append(conn.execute(
                        "SELECT lastHeartbeat FROM runs").fetchone()[0])
                    time.sleep(0.4)
            finally:
                conn.close()

        self.run_ticket(implementer_line("sonnet"), APPROVE,
                        on_probe=slow_probe)
        self.assertLess(beats[0], beats[1])
        self.assertEqual(self.turns(), ["claude sonnet"])

    def test_merge_gate_resume_whose_profile_left_the_config_stops(self):
        from pause_fixture import PauseReply

        from holophyte.loop.stop import command
        self.configure(PROFILES + '[merge]\napprove = "human"\n')
        self.run_ticket(implementer_line("sonnet"),
                        PauseReply(self.db, APPROVE))
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "merge_gate")])
        command(self.project, a_task()["id"], None, resume=True)
        self.configure('[agents.implementer]\nharness = "claude"\n'
                       'model = "opus"\n[merge]\napprove = "human"\n')
        self.run_ticket(implementer_line("sonnet"))
        self.assertEqual(len(self.profiles()), 1)
        self.assertEqual(self.read(
            "SELECT outcome, failureKind FROM runs ORDER BY id")[-1],
            ("failed", "infra"))


CLASSIFIER = PROFILES + '[questions.complexity]\nbackend = "claude"\n'
ROUTING = CLASSIFIER + 'route = true\nprofiles = { small = "sonnet" }\n'


class ComplexityClassifierTests(ProfileRunFixture):
    def setUp(self):
        super().setUp()
        self.claude = FakeClaude(self)

    def classify(self, config, body=VALID_BODY):
        self.configure(config)
        self.run_ticket(body, APPROVE, guard=question_guard())

    def verdicts(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'complexity'")]

    def test_a_verdict_is_recorded_and_changes_nothing_with_route_off(self):
        self.claude.answer("small", 0.9, reason="one config key\nand a test")
        self.classify(CLASSIFIER)
        self.assertEqual(self.verdicts(), [{
            "verdict": "small", "confidence": 0.9, "reason": "one config key",
            "backend": "claude", "model": "haiku", "why": None}])
        self.assertEqual(self.turns(), ["claude opus"])
        self.assertEqual(self.profiles()[0]["source"], "default")

    def test_a_confident_routed_verdict_picks_its_profile(self):
        self.claude.answer("small", 0.9, reason="one config key")
        self.classify(ROUTING)
        self.assertEqual(self.turns(), ["claude sonnet"])
        self.assertEqual(self.profiles(), [{
            "profile": "sonnet", "source": "classifier", "requested": None,
            "why": None, "label": "claude sonnet"}])

    def test_a_low_confidence_verdict_keeps_the_classifier_out(self):
        self.claude.answer("small", 0.5, reason="one config key")
        self.classify(ROUTING)
        self.assertEqual(self.turns(), ["claude opus"])
        self.assertEqual(self.profiles()[0]["source"], "default")

    def test_a_ticket_line_outranks_a_confident_verdict(self):
        self.claude.answer("small", 0.9, reason="one config key")
        self.classify(ROUTING + '[agents.implementer_profiles.opus]\n'
                      'harness = "claude"\nmodel = "opus"\n',
                      implementer_line("opus"))
        self.assertEqual(self.profiles()[0]["profile"], "opus")
        self.assertEqual(self.profiles()[0]["source"], "ticket")

    def test_a_timed_out_classifier_leaves_the_run_on_its_usual_route(self):
        self.claude.answer("small", 0.9, reason="one config key")
        self.claude.stall(5)
        with patch.object(holophyte.question_cli, "TIMEOUT", 0.5):
            self.classify(ROUTING)
        self.assertEqual(self.turns(), ["claude opus"])
        verdict, = self.verdicts()
        self.assertEqual((verdict["verdict"], verdict["why"]),
                         (None, "timeout"))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_merge_gate_resume_reuses_the_recorded_verdict(self):
        from pause_fixture import PauseReply

        from holophyte.loop.stop import command
        self.claude.answer("small", 0.9, reason="one config key")
        self.configure(CLASSIFIER + '[merge]\napprove = "human"\n')
        self.run_ticket(VALID_BODY, PauseReply(self.db, APPROVE),
                        guard=question_guard())
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "merge_gate")])
        command(self.project, a_task()["id"], None, resume=True)
        self.run_ticket(VALID_BODY, guard=question_guard())
        self.assertEqual(len(self.verdicts()), 1)
        self.assertEqual(
            [call["mode"] for call in self.claude.calls()].count("question"), 1)

    def test_a_long_escaped_body_keeps_the_record_whole(self):
        self.claude.answer("large", 0.9, reason="a long ticket")
        body = VALID_BODY.replace("The thing, added.", "変" * 20000)
        self.classify(CLASSIFIER, body)
        prompt = next(call["argv"][-1] for call in self.claude.calls()
                      if call["mode"] == "question")
        record = json.loads(prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertEqual((record["estimate_min"], record["criteria"]), (5, 1))
        self.assertTrue(record["body"].startswith("# Add a thing"))

    def test_a_resume_asks_when_the_resumed_run_has_no_verdict(self):
        from pause_fixture import PauseReply

        from holophyte.loop.stop import command
        self.claude.answer("small", 0.9, reason="one config key")
        self.configure(PROFILES + '[merge]\napprove = "human"\n')
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        project_id = store.tickets.ensure_project(conn, StubProvider.TEAM,
                                                  self.target)
        ticket = holophyte.board.projection.mirror_task(conn, project_id,
                                                        a_task())
        run = store.claim(conn, project_id, ticket)
        store.record_event(conn, run, "complexity", "complexity: small",
                           level="detail", payload=json.dumps({
                               "verdict": "small", "confidence": 0.9}))
        store.release(conn, run, "failed", "an earlier attempt")
        conn.commit()
        self.run_ticket(VALID_BODY, PauseReply(self.db, APPROVE))
        self.assertEqual(self.read(
            "SELECT outcome, resumePhase FROM runs ORDER BY id")[-1],
            ("paused", "merge_gate"))
        command(self.project, a_task()["id"], None, resume=True)
        self.configure(CLASSIFIER + '[merge]\napprove = "human"\n')
        self.run_ticket(VALID_BODY, guard=question_guard())
        self.assertEqual(self.read(
            "SELECT runId FROM runEvents WHERE kind = 'complexity'"
            " ORDER BY id"), [(1,), (3,)])

    def test_a_long_escaped_title_keeps_the_record_whole(self):
        self.claude.answer("large", 0.9, reason="a long title")
        self.configure(CLASSIFIER)
        with profile_routes(self.db, (APPROVE,), (0, "ready"), False, (),
                            None) as dispatch:
            self.loop(fake=dispatch, guard=question_guard(),
                      provider=StubProvider(dict(
                          a_task(), title="変" * 20000, body=VALID_BODY)))
        prompt = next(call["argv"][-1] for call in self.claude.calls()
                      if call["mode"] == "question")
        record = json.loads(prompt.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        self.assertTrue(record["title"].startswith("変"))
        self.assertTrue(record["body"].startswith("# Add a thing"))

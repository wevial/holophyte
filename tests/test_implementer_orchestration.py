"""The implementer's orchestration mode: the brief paragraph it adds, the
config refusals, the run event and the unchanged resume command line.

Run: python3 -m unittest discover -s tests -p 'test_implementer_orchestration.py'
"""
import json
from pathlib import Path
from unittest.mock import patch

import holophyte.agents.probes
import holophyte.agents.roles
import holophyte.board.projection
import holophyte.config.checks
import store
import store.tickets
from holophyte.agents.fix_session import resume_argv
from holophyte.loop.runs import open_store
from tests.fake_agent import APPROVE, Commit, FakeAgent
from tests.loop_fixture import VALID_BODY, LoopFixture, StubProvider, a_task

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
    "If the ticket reports a defect and a test built from the real data "
    "shape passes on the unchanged code, the defect did not reproduce: "
    "commit the test(s) only, change no other code, and end your reply with "
    "exactly this line:\nOUTCOME: NOT_REPRODUCED")

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
                return 0, "You've hit your limit"
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

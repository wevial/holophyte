"""A failed run's cause question after close-out, and the one requeue a
confident infra answer earns, through the real loop on real git."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from failure_triage_fixture import (  # noqa: E402 - after the sys.path insert above
    FAILURES,
    REQUEUE,
    FakeClaude,
    question_guard,
)
from fake_agent import APPROVE, REQUEST_CHANGES, Commit, Idle  # noqa: E402
from loop_fixture import BRANCH, LoopFixture, MergeModeFixture  # noqa: E402

import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above
from holophyte import redact  # noqa: E402 - after the sys.path insert above
from holophyte.board.projection import close_out_failure  # noqa: E402
from holophyte.config.checks import check_config  # noqa: E402
from holophyte.loop.failure_triage import triage_failure  # noqa: E402
from holophyte.serve.views import attention  # noqa: E402

FAILS_A_FIX_ROUND = (Commit("work"), REQUEST_CHANGES, Idle())
STATE_KEYS = {"ticket_title", "reason", "failure_kind", "outcome_class",
              "attempt", "last_review_verdict", "last_review_findings"}


class FailureTriageLoopTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.claude = FakeClaude(self)

    def triage(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'failure_triage'"
            " ORDER BY id")]

    def ticket_status(self):
        return self.read("SELECT status FROM tickets")

    def interventions(self):
        return self.read('SELECT "action", source, note FROM interventions'
                         " WHERE \"action\" != 'migrate'")

    def test_a_confident_infra_failure_is_requeued_on_its_preserved_branch(self):
        self.configure(REQUEUE)
        self.claude.answer("infra", 0.95)

        self.loop(*FAILS_A_FIX_ROUND, guard=question_guard())

        self.assertEqual(self.ticket_status(), [("ready",)])
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        preserved = re.search(rf"branch {BRANCH} preserved at (\w+)", reason)
        head = self.git("rev-parse", BRANCH).strip()
        self.assertTrue(head.startswith(preserved.group(1)), (head, reason))
        self.assertEqual(self.git("log", "--format=%s", f"main..{BRANCH}"),
                         "work\n")
        ((action, source, note),) = self.interventions()
        self.assertEqual((action, source), ("requeue", "factory"))
        self.assertTrue(note.startswith("failure triage: infra (0.95"), note)
        (payload,) = self.triage()
        self.assertEqual((payload["requeued"], payload["why"]), (True, "requeued"))

    def test_a_second_failure_after_an_auto_requeue_waits_for_the_operator(self):
        self.configure(REQUEUE)
        self.claude.answer("infra", 0.95)
        self.loop(*FAILS_A_FIX_ROUND, guard=question_guard())
        self.assertEqual(self.ticket_status(), [("ready",)])

        self.loop(Commit("more work"), REQUEST_CHANGES, Idle(),
                  guard=question_guard())

        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(2,)])
        self.assertEqual(self.ticket_status(), [("blocked_on_operator",)])
        self.assertEqual([row[:2] for row in self.interventions()],
                         [("requeue", "factory")])
        first, second = self.triage()
        self.assertEqual((second["requeued"], second["why"]),
                         (False, "already auto-requeued"))

    def test_infra_below_requeue_confidence_strands_with_its_classification(self):
        self.configure(REQUEUE)
        self.claude.answer("infra", 0.6)

        self.loop(*FAILS_A_FIX_ROUND, guard=question_guard())

        self.assertEqual(self.ticket_status(), [("in_flight",)])
        self.assertEqual(self.interventions(), [])
        self.assertEqual(self.triage(), [{
            "choice": "infra", "confidence": 0.6, "backend": "claude",
            "model": "sonnet", "requeued": False,
            "why": "below requeue_confidence"}])

    def test_without_requeue_a_confident_infra_answer_is_only_recorded(self):
        self.configure(FAILURES)
        self.claude.answer("infra", 0.95)

        self.loop(*FAILS_A_FIX_ROUND, guard=question_guard())

        self.assertEqual(self.ticket_status(), [("in_flight",)])
        self.assertEqual(self.interventions(), [])
        (payload,) = self.triage()
        self.assertEqual((payload["requeued"], payload["why"]),
                         (False, "requeue off"))

    def assert_not_infra_is_never_requeued(self, choice):
        self.configure(REQUEUE)
        self.claude.answer(choice, 0.95)

        self.loop(*FAILS_A_FIX_ROUND, guard=question_guard())

        self.assertEqual(self.ticket_status(), [("in_flight",)])
        self.assertEqual(self.interventions(), [])
        (payload,) = self.triage()
        self.assertEqual((payload["choice"], payload["why"]), (choice, "not infra"))

    def test_a_code_answer_is_never_requeued(self):
        self.assert_not_infra_is_never_requeued("code")

    def test_a_spec_answer_is_never_requeued(self):
        self.assert_not_infra_is_never_requeued("spec")

    def test_a_down_route_strands_the_run_as_today(self):
        self.configure(REQUEUE)
        self.claude.down()

        self.loop(*FAILS_A_FIX_ROUND, guard=question_guard())

        self.assertEqual(self.ticket_status(), [("in_flight",)])
        self.assertEqual(self.interventions(), [])
        (payload,) = self.triage()
        self.assertEqual((payload["requeued"], payload["why"]),
                         (False, "route_down"))

    def test_a_run_swept_before_a_store_write_is_not_triaged(self):
        self.configure(REQUEUE)
        self.claude.answer("infra", 0.95)
        real = store.set_review_round_cap

        def swept_first(conn, run_id, cap):
            other = store.open(self.db)
            try:
                (ticket_id,) = other.execute(
                    "SELECT ticketId FROM runs WHERE id = ?", (run_id,)).fetchone()
                close_out_failure(
                    self.project, other, run_id, ticket_id,
                    "swept by the supervisor in phase working: time_box",
                    failure_kind="swept")
            finally:
                other.close()
            return real(conn, run_id, cap)

        with patch.object(store, "set_review_round_cap", swept_first):
            self.loop(*FAILS_A_FIX_ROUND, guard=question_guard())

        self.assertEqual(self.read("SELECT failureKind FROM runs"), [("swept",)])
        self.assertEqual(self.ticket_status(), [("in_flight",)])
        self.assertEqual(self.interventions(), [])
        self.assertEqual(self.triage(), [])
        self.assertEqual([call for call in self.claude.calls()
                          if call["mode"] == "question"], [])

    def test_without_the_table_nothing_is_asked_or_recorded(self):
        self.claude.answer("infra", 0.95)

        self.loop(*FAILS_A_FIX_ROUND, guard=question_guard())

        self.assertEqual(self.claude.calls(), [])
        self.assertEqual(self.triage(), [])
        self.assertEqual(self.ticket_status(), [("in_flight",)])
        _, body = attention(self.project)
        (item,) = [item for item in body["items"] if item["kind"] == "failed"]
        self.assertIsNone(item["triage"])


class FailureQuestionTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.claude = FakeClaude(self)

    def failed_run(self, reason):
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team", str(self.target))
        ticket = store.tickets.mirror_ticket(
            conn, project, "issue", "KO-1", "Example",
            acceptance_criteria=["works"], verification_commands=["true"])
        store.tickets.transition(conn, ticket, "in_flight")
        run = store.claim(conn, project, ticket)
        store.release(conn, run, "failed", reason=reason)
        return conn, run, ticket

    def test_a_third_failure_after_two_human_requeues_is_refused_not_requeued(self):
        self.configure(REQUEUE)
        self.claude.answer("infra", 0.95)
        conn, run, ticket = self.failed_run("verify timed out")
        (project,) = conn.execute("SELECT projectId FROM tickets").fetchone()
        for _ in range(2):
            store.requeue(conn, ticket, "the verify host was down")
            store.tickets.transition(conn, ticket, "in_flight")
            run = store.claim(conn, project, ticket)
            store.release(conn, run, "failed", reason="verify timed out")

        with patch("sys.stdout"):
            triage_failure(self.project, conn, run, ticket)

        self.assertEqual(conn.execute("SELECT status FROM tickets").fetchone(),
                         ("in_flight",))
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE \"action\" = 'requeue'"
            " AND source = 'factory'").fetchone(), (0,))
        (payload,) = conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'failure_triage'"
            " AND runId = ?", (run,)).fetchone()
        payload = json.loads(payload)
        self.assertIs(payload["requeued"], False)
        self.assertTrue(payload["why"].startswith("requeue refused:"),
                        payload["why"])

    def test_a_registered_secret_in_the_reason_never_reaches_the_cli(self):
        secret = "sk-live-4f9a2c71e3b8"
        self.configure(FAILURES)
        self.claude.answer("code", 0.9)
        conn, run, ticket = self.failed_run(f"push refused: token {secret} expired")
        self.assertIn(secret, conn.execute(
            "SELECT outcomeReason FROM runs").fetchone()[0])

        with redact.values_held([secret]):
            triage_failure(self.project, conn, run, ticket)

        (asked,) = [call for call in self.claude.calls()
                    if call["mode"] == "question"]
        self.assertNotIn(secret, json.dumps(asked["argv"]))
        record = re.search(r"```json\n(.*)\n```", asked["argv"][-1], re.S)
        state = json.loads(record.group(1))
        self.assertEqual(set(state), STATE_KEYS)
        self.assertIn("push refused: token", state["reason"])

    def test_a_failures_route_fallback_is_recorded_against_its_own_seat(self):
        self.configure(FAILURES + 'backend_fallback = "codex"\n')
        self.claude.down()
        self.claude.codex_fallback("code", 0.9)
        conn, run, ticket = self.failed_run("verify failed")

        with patch("sys.stdout"):
            triage_failure(self.project, conn, run, ticket)

        (guidance,) = conn.execute(
            "SELECT guidance FROM interventions WHERE action = 'route_fallback'"
            " AND runId = ?", (run,)).fetchone()
        self.assertEqual(json.loads(guidance)["seat"], "questions.failures")
        (payload,) = conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'failure_triage'").fetchone()
        self.assertEqual(json.loads(payload)["backend"], "codex")

    def test_config_refusals_name_the_key_and_an_empty_table_has_defaults(self):
        for text, key in (("requeue_confidence = 1.5\n", "requeue_confidence"),
                          ("retries = 2\n", "retries")):
            with self.subTest(key=key):
                self.configure("[questions.failures]\n" + text)
                with self.assertRaisesRegex(
                        SystemExit, rf"\[questions\.failures\] {key}\b"):
                    check_config(self.project)
        self.configure("[questions.failures]\n")
        check_config(self.project)
        from holophyte import questions
        options = questions.settings(self.project.config())["failures"]
        self.assertEqual(
            (options["backend"], options["model"], options["effort"],
             options["requeue_confidence"]),
            ("claude", "sonnet", "medium", 0.85))


class ParkedRunTriageTests(MergeModeFixture):
    def test_a_run_parked_on_its_pull_request_is_not_triaged(self):
        claude = FakeClaude(self)
        claude.answer("infra", 0.95)
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n' + REQUEUE)
        self.fake_route()

        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider(), guard=question_guard())

        self.assertEqual(self.read("SELECT phase, prUrl FROM runs"),
                         [("awaiting_merge_approval", self.URL)])
        self.assertEqual(claude.calls(), [])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'failure_triage'"),
            [(0,)])

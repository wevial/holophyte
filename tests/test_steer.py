"""`holo steer KEY -n NOTE`: an amendment every later run's ticket text
carries, a hint one implement turn's opening carries, or a parked run's
send-back; refused for a run on its pull request and a ticket with nothing
left to steer.

Run: python3 -m unittest discover -s tests -p 'test_steer.py' -v
"""
from __future__ import annotations

import contextlib
import io
import re

import holophyte.board.projection
import store
import store.tickets
from holophyte.babysit.maintainer_notes import (
    HINT_PREFIX,
    PREFIX,
    amended_ticket,
    pending_state,
)
from holophyte.holo import cli as holo_cli
from holophyte.loop.runs import open_store
from holophyte.pr.github import PrState
from store.operator_notes import consume as consume_note
from store.steer_notes import consume
from tests.fake_agent import APPROVE, Commit
from tests.loop_fixture import LoopFixture, Refuse, StubProvider, a_task

T0 = 1_700_000_000_000
PR_URL = "https://github.com/example/repo/pull/7"
INTERVENTIONS = ("SELECT runId, projectId, action FROM interventions"
                 " WHERE action != 'migrate' ORDER BY id")
NOTES = ("SELECT ticketId, runId, kind, note, interventionId, eventId,"
         " consumedBy FROM steerNotes ORDER BY id")


class SteerFixture(LoopFixture):
    def setUp(self):
        super().setUp()
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, StubProvider.TEAM, self.target)
        self.ticket = self.mirror(a_task())

    def mirror(self, task):
        ticket = holophyte.board.projection.mirror_task(
            self.conn, self.project_id, task)
        self.conn.commit()
        return ticket

    def holo(self, *words):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = holo_cli.main([*words, "-p", str(self.target)]) or 0
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue() + err.getvalue()

    def steer(self, *words, key="KO-131"):
        code, said = self.holo("steer", key, *words)
        self.assertEqual(code, 0, said)
        return said

    def claim(self, ticket=None):
        ticket = self.ticket if ticket is None else ticket
        store.tickets.transition(self.conn, ticket, "in_flight")
        return store.claim(self.conn, self.project_id, ticket, now=T0)

    def fail_run(self, run):
        store.release(self.conn, run, "failed", "verify failed", now=T0 + 1)

    def park_on_the_pull_request(self, run, ticket=None, pr_url=PR_URL):
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, run, phase)
        store.park(self.conn, run, "awaiting_merge_approval", pr_url=pr_url)
        store.tickets.transition(self.conn, self.ticket if ticket is None
                                 else ticket, "blocked_on_operator")

    def prompts(self, fake):
        return {role: [t.goal for t in fake.turns if t.role == role]
                for role in ("implement", "review")}

    def amended(self, prompt, note):
        return re.search(rf"{re.escape(PREFIX)}\nsteer note \d+ by [^\n]+:\n"
                         rf"{re.escape(note)}", prompt) is not None


class AmendmentTests(SteerFixture):
    def test_a_ready_ticket_steered_carries_the_note_to_implementer_and_reviewer(self):
        self.steer("-n", "also log the port")

        self.assertEqual(self.read(INTERVENTIONS),
                         [(None, self.project_id, "steer")])
        (row,) = self.read(NOTES)
        self.assertEqual(row[:4], (self.ticket, None, "amendment",
                                   "also log the port"))
        fake, _ = self.loop(Commit("the thing", path="app.txt"), APPROVE)
        prompts = self.prompts(fake)
        self.assertTrue(prompts["implement"] and prompts["review"])
        for role, goals in prompts.items():
            for goal in goals:
                self.assertTrue(self.amended(goal, "also log the port"), role)

    def test_a_failed_ticket_awaiting_requeue_is_amended_on_its_ended_run(self):
        run = self.claim()
        self.fail_run(run)

        self.steer("-n", "also log the port")

        self.assertEqual(self.read(INTERVENTIONS), [(run, None, "steer")])
        ((intervention, ),) = self.read("SELECT interventionId FROM steerNotes")
        self.assertEqual(self.read("SELECT runId FROM interventions"
                                   f" WHERE id = {intervention}"), [(run,)])
        store.requeue(self.conn, self.ticket, "rerun it")
        fake, _ = self.loop(Commit("the thing", path="app.txt"), APPROVE)
        prompts = self.prompts(fake)
        self.assertTrue(prompts["implement"] and prompts["review"])
        for goal in prompts["implement"] + prompts["review"]:
            self.assertTrue(self.amended(goal, "also log the port"))


class HintTests(SteerFixture):
    HINT = "the port is in config.toml"

    def test_a_hint_reaches_the_next_implement_turn_and_no_reviewer(self):
        self.steer("--hint", "-n", self.HINT)

        fake, _ = self.loop(Commit("the thing", path="app.txt"), APPROVE)

        prompts = self.prompts(fake)
        (implement,) = prompts["implement"]
        opening = implement[:implement.index("Implement this task in this repo:")]
        self.assertIn(f"{HINT_PREFIX}\nsteer note 1 by ", opening)
        self.assertIn(self.HINT, opening)
        self.assertTrue(prompts["review"])
        for goal in prompts["review"]:
            self.assertNotIn(self.HINT, goal)
        (run,) = self.read("SELECT id FROM runs")
        self.assertEqual(self.read("SELECT consumedBy FROM steerNotes"), [run])

    def test_a_consumed_hint_does_not_reach_the_second_run(self):
        self.steer("--hint", "-n", self.HINT)
        first, _ = self.loop(Refuse())
        self.assertIn(self.HINT, first.turns[0].goal)
        store.requeue(self.conn, self.ticket, "rerun it")

        second, _ = self.loop(Commit("the thing", path="app.txt"), APPROVE,
                              provider=StubProvider(a_task()))

        self.assertEqual(second.roles[0], "implement")
        self.assertNotIn(self.HINT, second.turns[0].goal)


class ParkedTests(SteerFixture):
    def sent_back(self, run):
        intervention = self.read(
            "SELECT runId, source, \"trigger\", action, question, guidance, note"
            f" FROM interventions WHERE runId = {run} AND action != 'claim'")
        event = self.read("SELECT kind, summary, payload FROM runEvents"
                          f" WHERE runId = {run} AND kind = 'operator_note'")
        return [row[1:] for row in intervention], event

    def test_a_parked_run_is_sent_back_as_send_back_sends_it(self):
        run = self.claim()
        self.park_on_the_pull_request(run)
        twin_ticket = self.mirror(a_task(2))
        twin = self.claim(twin_ticket)
        self.park_on_the_pull_request(twin, twin_ticket,
                                      "https://github.com/example/repo/pull/8")

        self.steer("--author", "maintainer", "-n", "address the nit")
        code, said = self.holo("send-back", str(twin), "--author", "maintainer",
                               "-n", "address the nit")

        self.assertEqual(code, 0, said)
        self.assertEqual(self.sent_back(run), self.sent_back(twin))
        self.assertEqual(self.read(f"SELECT status FROM tickets"
                                   f" WHERE id = {self.ticket}"), [("ready",)])
        self.assertEqual(self.read(f"SELECT outcome FROM runs WHERE id = {run}"),
                         [("abandoned",)])
        ((event,),) = self.read("SELECT id FROM runEvents WHERE kind ="
                                f" 'operator_note' AND runId = {run}")
        ((intervention,),) = self.read(
            "SELECT id FROM interventions WHERE action = 'operator_note'"
            f" AND runId = {run}")
        self.assertEqual(self.read(NOTES), [(self.ticket, run, "amendment",
                                             "address the nit", intervention,
                                             event, None)])


    def test_steers_after_a_send_back_reach_the_resumed_babysit_pass(self):
        run = self.claim()
        self.park_on_the_pull_request(run)
        self.steer("--author", "maintainer", "-n", "address the nit")
        self.steer("--author", "maintainer", "-n", "also log the port")
        self.steer("--hint", "--author", "maintainer", "-n", "see config.toml")

        self.assertEqual(self.read(f"SELECT status, activeRunId FROM tickets"
                                   f" WHERE id = {self.ticket}"),
                         [("ready", None)])
        self.assertEqual(self.read("SELECT action FROM interventions"
                                   f" WHERE runId = {run} AND action != 'claim'"),
                         [("operator_note",)] * 3)
        resumed = self.claim()
        threads = pending_state(self.conn, resumed,
                                PrState((), "success", None), PR_URL).threads
        self.assertEqual([t.body for t in threads],
                         ["address the nit", "also log the port",
                          "see config.toml"])
        reviewed = amended_ticket(self.conn, resumed, "the ticket", PR_URL)
        self.assertIn("also log the port", reviewed)
        self.assertNotIn("see config.toml", reviewed)


class RefusalTests(SteerFixture):
    def assert_refused(self, *words, naming, key="KO-131"):
        before = list(self.conn.iterdump())
        code, said = self.holo("steer", key, *words, "-n", "a note")
        self.assertNotEqual(code, 0, said)
        for text in naming:
            self.assertIn(text, said)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_a_live_run_on_its_pull_request_is_refused_naming_pause_and_abort(self):
        run = self.claim()
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, run, phase)
        store.set_pull_request(self.conn, run, PR_URL)
        for words in ((), ("--hint",)):
            with self.subTest(words=words):
                self.assert_refused(*words, naming=("merge_gate",
                                                    "holo pause KO-131",
                                                    "holo abort KO-131"))

    def test_a_merged_ticket_is_refused_naming_its_state(self):
        store.tickets.walk_ticket(self.conn, self.ticket, "merged")
        self.assert_refused(naming=("merged",))

    def test_a_ticket_parked_with_no_pull_request_is_refused_naming_it(self):
        run = self.claim()
        self.park_on_the_pull_request(run, pr_url=None)
        self.assert_refused(naming=("blocked_on_operator", "no pull request"))

    def test_an_approved_local_candidate_is_refused_naming_its_resume(self):
        run = self.claim()
        self.park_on_the_pull_request(run, pr_url=None)
        store.approve(self.conn, self.ticket, "ship it", run_id=run)
        for words in ((), ("--hint",)):
            with self.subTest(words=words):
                self.assert_refused(*words, naming=(
                    f"resumes run {run}'s candidate at merge_gate",))


class ReportTests(SteerFixture):
    def test_report_reads_a_store_from_before_the_steer_table(self):
        self.conn.executescript("DROP TABLE steerNotes;\n"
                                "PRAGMA user_version = 43;\n")

        code, said = self.holo("report", "--notes")

        self.assertEqual(code, 0, said)
        self.assertIn("Steers (0)", said.splitlines())

    def test_report_notes_lists_each_steer_pending_or_consumed(self):
        run = self.claim()
        self.fail_run(run)
        self.steer("--author", "ana", "-n", "pending amendment")
        self.steer("--hint", "--author", "bo", "-n", "consumed hint")
        store.requeue(self.conn, self.ticket, "rerun it")
        later = self.claim()
        consume(self.conn, [2], later)
        self.fail_run(later)
        parked_ticket = self.mirror(a_task(2))
        parked = self.claim(parked_ticket)
        self.park_on_the_pull_request(parked, parked_ticket)
        self.steer("--author", "cy", "-n", "parked note", key="KO-132")
        ((event,),) = self.read("SELECT eventId FROM steerNotes WHERE id = 3")
        consume_note(self.conn, parked, [event], 1)

        code, said = self.holo("report", "--notes")

        self.assertEqual(code, 0, said)
        lines = said.splitlines()
        steers = lines[lines.index("Steers (3)") + 1:][:3]
        for line, expected in zip(steers, (
                "KO-131 amendment by ana, pending: pending amendment",
                f"KO-131 hint by bo, consumed by run {later}: consumed hint",
                f"KO-132 amendment by cy, consumed by run {parked}: parked note")):
            self.assertTrue(line.endswith(expected), line)


if __name__ == "__main__":
    import unittest
    unittest.main()

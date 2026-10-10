"""`holo steer` on a run babysitting its pull request: the note is recorded on
the live run as an operator_note event, and the babysitter's next poll reads
it as a maintainer thread that drives a fix round.

Run: python3 -m unittest discover -s tests -p 'test_steer_babysit.py' -v
"""
from __future__ import annotations

import io
from unittest.mock import patch

import holophyte.pr.github
import store
from holophyte.babysit import maintainer_notes
from holophyte.babysit.maintainer_notes import PREFIX, amended_ticket
from holophyte.cli.operator import steer_ticket
from store.operator_notes import close_for_merge
from tests.fake_agent import APPROVE, Commit, Idle, Reply
from tests.loop_fixture import MergeModeFixture
from tests.test_steer import PR_URL, SteerFixture

NOTE = "also log the port"
HINT = "the port is in config.toml"


class RecordingTests(SteerFixture):
    def test_a_babysitting_run_takes_the_note_and_stays_live(self):
        run = self.claim()
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, run, phase)
        store.set_pull_request(self.conn, run, PR_URL)

        said = self.steer("-n", NOTE)

        self.assertIn("fix round", said)
        ((intervention, on_run),) = self.read(
            "SELECT id, runId FROM interventions WHERE action = 'steer'")
        self.assertEqual(on_run, run)
        events = self.read(f"SELECT id, kind FROM runEvents WHERE runId = {run}"
                           " AND kind IN ('intervention', 'operator_note')"
                           " ORDER BY id")
        self.assertEqual([kind for _, kind in events],
                         ["intervention", "operator_note"])
        self.assertEqual(self.read(
            "SELECT runId, kind, note, interventionId, eventId FROM steerNotes"),
            [(run, "amendment", NOTE, intervention, events[1][0])])
        self.assertEqual(self.read("SELECT phase, endedAt FROM runs"),
                         [("merge_gate", None)])
        self.assertEqual(self.read("SELECT status, activeRunId FROM tickets"),
                         [("in_flight", run)])

    def test_a_run_closed_for_its_merge_refuses_the_note(self):
        run = self.claim()
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, run, phase)
        store.set_pull_request(self.conn, run, PR_URL)
        self.assertTrue(close_for_merge(self.conn, run, PR_URL))
        before = list(self.conn.iterdump())

        code, said = self.holo("steer", "KO-131", "-n", NOTE)

        self.assertNotEqual(code, 0, said)
        self.assertIn("merge_gate", said)
        self.assertEqual(list(self.conn.iterdump()), before)


class BabysitDeliveryTests(MergeModeFixture):
    def babysit_with_a_steer_while_checks_pend(self, note, hint=False):
        """A fix round's push leaves checks pending; the maintainer steers
        during the first poll's nap, and the next poll still reads pending."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]),
                                self.pr_state(checks="PENDING"),
                                self.pr_state(checks="PENDING"),
                                self.pr_state()])
        naps, said = [], io.StringIO()

        def nap(seconds):
            naps.append(seconds)
            if len(naps) == 1:
                steer_ticket(self.project, "KO-131", note, hint=hint,
                             author="maintainer", out=said)

        with patch.object(holophyte.pr.github, "SLEEP", nap):
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                Reply("THREAD 1: ADDRESS -- a real crash"),
                                Commit("fix: default load()"),
                                Commit("apply the maintainer's note"),
                                APPROVE, Idle(""), provider=self.provider())
        self.assertIn("live run", said.getvalue())
        self.assertEqual(naps, [holophyte.pr.github.CHECK_POLL_S])
        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "adjudicate", "implement", "implement",
                                      "review", "implement"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        return fake

    def test_a_note_steered_between_polls_drives_a_fix_and_amends_the_review(self):
        fake = self.babysit_with_a_steer_while_checks_pend(NOTE)

        brief, review = fake.turns[5].goal, fake.turns[6].goal
        self.assertIn(PREFIX, brief)
        self.assertIn(NOTE, brief)
        self.assertIn(f"{PREFIX}\noperator_note event", review)
        self.assertIn(NOTE, review)
        ((run, event_id),) = self.read(
            "SELECT runId, id FROM runEvents WHERE kind = 'operator_note'")
        self.assertEqual(self.read(
            "SELECT runId FROM runEvents WHERE kind = 'operator_note_consumed'"),
            [(run,)])
        with store.open(str(self.project.store_path)) as conn:
            amended = amended_ticket(conn, run, "the ticket", self.URL)
        self.assertIn(f"operator_note event {event_id}", amended)
        self.assertIn(NOTE, amended)

    def test_a_hint_steered_between_polls_reaches_the_fix_but_not_the_review(self):
        fake = self.babysit_with_a_steer_while_checks_pend(HINT, hint=True)

        brief, review = fake.turns[5].goal, fake.turns[6].goal
        self.assertIn(HINT, brief)
        self.assertNotIn(HINT, review)
        ((run,),) = self.read(
            "SELECT runId FROM runEvents WHERE kind = 'operator_note'")
        with store.open(str(self.project.store_path)) as conn:
            self.assertEqual(amended_ticket(conn, run, "the ticket", self.URL),
                             "the ticket")

    def test_a_note_steered_after_the_last_poll_is_fixed_before_the_merge(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state()])
        read = maintainer_notes.pending_state
        polls = []

        def read_then_steer(conn, run_id, state, url):
            state = read(conn, run_id, state, url)
            polls.append(state)
            if len(polls) == 1:
                steer_ticket(self.project, "KO-131", NOTE, author="maintainer",
                             out=io.StringIO())
            return state

        with patch.object(maintainer_notes, "pending_state", read_then_steer):
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                Commit("apply the maintainer's note"),
                                APPROVE, Idle(""), provider=self.provider())

        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement", "review", "implement"])
        self.assertIn(NOTE, fake.turns[3].goal)
        self.assertEqual(self.read(
            "SELECT count(*) FROM runEvents WHERE kind = 'operator_note_consumed'"),
            [(1,)])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

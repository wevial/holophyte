"""`holo steer` on a live run before its pull request: the note is recorded on
the run at once and carried by its next implementer turn (the implement
turn, a fix turn, or a steer turn the loop starts for it), and an amendment
joins the ticket text the later review rounds and the adjudicator read.

Run: python3 -m unittest discover -s tests -p 'test_steer_live.py' -v
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import holophyte.loop.pipeline
import store
from holophyte.babysit.maintainer_notes import HINT_PREFIX
from holophyte.loop.stop import resume_paused
from tests.fake_agent import APPROVE, PASS, REQUEST_CHANGES, Commit
from tests.test_steer import SteerFixture

NOTE = "also log the port"
HINT = "the port is in config.toml"
NO_STEER_PROMPTS = (Path(__file__).resolve().parent / "fixtures" / "steer_live"
                    / "no_steer_prompts.json")
SHA = re.compile(r"\b[0-9a-f]{40}\b")


class Steering:
    """A scripted turn during which the maintainer runs `holo steer`, and with
    `pause` then asks the run to pause."""

    def __init__(self, test, step, *words, pause=False):
        self.test, self.step, self.words, self.pause = test, step, words, pause
        self.role = step.role

    def play(self, cwd, turn):
        reply = self.step.play(cwd, turn)
        self.test.steer(*self.words)
        if self.pause:
            run = self.test.conn.execute(
                "SELECT id FROM runs WHERE endedAt IS NULL").fetchone()[0]
            store.pause(self.test.conn, run, "reboot writer")
        return reply


class Phased(Commit):
    """An implementer commit that notes the run's phase as it plays."""

    def __init__(self, test, message):
        super().__init__(message)
        self.test, self.phase = test, None

    def play(self, cwd, turn):
        ((self.phase,),) = self.test.read(
            "SELECT phase FROM runs ORDER BY id DESC LIMIT 1")
        return super().play(cwd, turn)


class LiveSteerFixture(SteerFixture):
    def goals(self, fake, role):
        return [turn.goal for turn in fake.turns if turn.role == role]

    def consumed_by(self):
        return self.read("SELECT consumedBy FROM steerNotes ORDER BY id")

    def only_run(self):
        ((run,),) = self.read("SELECT id FROM runs")
        return run

    def walk(self, run, *phases):
        for phase in phases:
            store.set_phase(self.conn, run, phase)


class RecordingTests(LiveSteerFixture):
    def test_a_reviewing_run_records_the_note_on_itself_for_its_next_turn(self):
        run = self.claim()
        self.walk(run, "working", "verifying", "reviewing")

        said = self.steer("-n", NOTE)

        self.assertIn("next implementer turn", said)
        ((intervention, on_run),) = self.read(
            "SELECT id, runId FROM interventions WHERE action = 'steer'")
        self.assertEqual(on_run, run)
        self.assertEqual(self.read("SELECT runId, kind, note, interventionId,"
                                   " eventId, consumedBy FROM steerNotes"),
                         [(run, "amendment", NOTE, intervention, None, None)])

    def test_a_run_at_the_merge_gate_with_no_pull_request_is_refused_naming_it(self):
        run = self.claim()
        self.walk(run, "working", "verifying", "reviewing", "merge_gate")
        before = list(self.conn.iterdump())

        code, said = self.holo("steer", "KO-131", "-n", NOTE)

        self.assertNotEqual(code, 0, said)
        self.assertIn("merge_gate", said)
        self.assertEqual(list(self.conn.iterdump()), before)


class DeliveryTests(LiveSteerFixture):
    def test_a_note_steered_while_claimed_is_in_the_implement_prompt_and_consumed(self):
        phases = []
        cut = holophyte.loop.pipeline._cut_worktree

        def steered_cut(*args):
            phases.extend(self.read("SELECT phase FROM runs"))
            self.steer("-n", NOTE)
            return cut(*args)

        with patch.object(holophyte.loop.pipeline, "_cut_worktree", steered_cut):
            fake, _ = self.loop(Commit("the thing", path="app.txt"), APPROVE)

        self.assertEqual(phases, [("claimed",)])
        (implement,) = self.goals(fake, "implement")
        self.assertTrue(self.amended(implement, NOTE))
        self.assertEqual(self.consumed_by(), [(self.only_run(),)])

    def test_a_fix_prompt_carries_the_findings_and_a_note_steered_during_review(self):
        fake, _ = self.loop(
            Commit("the thing", path="app.txt"),
            Steering(self, REQUEST_CHANGES, "-n", NOTE),
            Commit("the fix", path="app.txt", body="fixed\n"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement", "review"])
        fix = fake.turns[2].goal
        self.assertIn("Reviewer findings:", fix)
        self.assertIn("Blocker: the scripted change is incomplete.", fix)
        self.assertTrue(self.amended(fix, NOTE))
        self.assertEqual(self.consumed_by(), [(self.only_run(),)])

    def test_a_note_steered_during_implement_gets_a_steer_turn_before_round_one(self):
        steer_turn = Phased(self, "the steer")
        fake, _ = self.loop(
            Steering(self, Commit("the thing", path="app.txt"), "-n", NOTE),
            steer_turn, APPROVE)

        self.assertEqual(fake.roles, ["implement", "implement", "review"])
        implement, steered = self.goals(fake, "implement")
        self.assertFalse(self.amended(implement, NOTE))
        self.assertTrue(self.amended(steered, NOTE))
        self.assertEqual(steer_turn.phase, "working")
        (review,) = self.goals(fake, "review")
        self.assertTrue(self.amended(review, NOTE))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_an_approval_with_a_note_pending_runs_a_steer_turn_and_round_two(self):
        fake, _ = self.loop(
            Commit("the thing", path="app.txt"),
            Steering(self, APPROVE, "-n", NOTE),
            Commit("the steer", path="app.txt", body="steered\n"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement", "review"])
        first, second = self.goals(fake, "review")
        self.assertFalse(self.amended(first, NOTE))
        self.assertTrue(self.amended(fake.turns[2].goal, NOTE))
        self.assertTrue(self.amended(second, NOTE))
        left_review = [edge for edge in self.transitions()
                       if edge.startswith("reviewing -> ")]
        self.assertEqual(left_review, ["reviewing -> verifying",
                                       "reviewing -> merge_gate"])

    def test_a_pause_on_an_approval_with_a_note_pending_resumes_to_deliver_it(self):
        paused, _ = self.loop(
            Commit("the thing", path="app.txt"),
            Steering(self, APPROVE, "-n", NOTE, pause=True))
        self.assertEqual(paused.roles, ["implement", "review"])
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "verifying")])
        resume_paused(self.project, self.conn, self.ticket, "go on")

        resumed, _ = self.loop(
            Commit("the steer", path="app.txt", body="steered\n"), APPROVE)

        self.assertEqual(resumed.roles, ["implement", "review"])
        steered, review = (turn.goal for turn in resumed.turns)
        self.assertTrue(self.amended(steered, NOTE))
        self.assertTrue(self.amended(review, NOTE))
        (_, paused_outcome), (second, outcome) = self.read(
            "SELECT id, outcome FROM runs ORDER BY id")
        self.assertEqual((paused_outcome, outcome), ("paused", "merged"))
        self.assertEqual(self.consumed_by(), [(second,)])

    def test_a_note_steered_in_the_last_fix_turn_is_carried_before_adjudication(self):
        fake, _ = self.loop(
            Commit("the thing", path="app.txt"), REQUEST_CHANGES,
            Commit("the fix", path="app.txt", body="fixed\n"), REQUEST_CHANGES,
            Steering(self, Commit("the second fix", path="app.txt",
                                  body="fixed again\n"), "-n", NOTE),
            Commit("the steer", path="app.txt", body="steered\n"), PASS)

        self.assertEqual(fake.roles[-3:], ["implement", "implement", "adjudicate"])
        self.assertTrue(self.amended(fake.turns[-2].goal, NOTE))
        self.assertTrue(self.amended(fake.turns[-1].goal, NOTE))
        self.assertEqual(self.consumed_by(), [(self.only_run(),)])

    def test_a_hint_a_steer_turn_carried_reaches_no_later_reviewer_or_adjudicator(self):
        fake, _ = self.loop(
            Steering(self, Commit("the thing", path="app.txt"), "--hint",
                     "-n", HINT),
            Commit("the steer", path="app.txt", body="steered\n"),
            REQUEST_CHANGES, Commit("the fix", path="app.txt", body="fixed\n"),
            REQUEST_CHANGES,
            Commit("the second fix", path="app.txt", body="fixed again\n"), PASS)

        steered = fake.turns[1]
        self.assertEqual(steered.role, "implement")
        self.assertIn(f"{HINT_PREFIX}\nsteer note 1 by ", steered.goal)
        self.assertIn(HINT, steered.goal)
        judged = self.goals(fake, "review") + self.goals(fake, "adjudicate")
        self.assertEqual(len(judged), 3)
        for goal in judged:
            self.assertNotIn(HINT, goal)
        self.assertEqual(self.consumed_by(), [(self.only_run(),)])


class UnsteeredTests(LiveSteerFixture):
    def test_with_no_steer_notes_every_prompt_is_the_one_recorded_before_steering(self):
        """The fixture holds the loop's prompts for this script from the code
        with no live steering, the temporary root and commit shas normalized."""
        fake, _ = self.loop(
            Commit("the thing", path="app.txt"), REQUEST_CHANGES,
            Commit("the fix", path="app.txt", body="fixed\n"), REQUEST_CHANGES,
            Commit("the second fix", path="app.txt", body="fixed again\n"), PASS)

        root = str(self.target.parent)
        prompts = [[turn.role, SHA.sub("<sha>", turn.goal.replace(root, "<root>"))]
                   for turn in fake.turns]
        self.assertEqual(prompts, json.loads(NO_STEER_PROMPTS.read_text()))


if __name__ == "__main__":
    import unittest
    unittest.main()

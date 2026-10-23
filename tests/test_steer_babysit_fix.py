"""`holo steer` during the fix turn the babysitter runs after a review of a fix
asks for changes: the run is in reviewing on its pull request, and the note
drives the babysitter's next fix round before the merge.

Run: python3 -m unittest discover -s tests -p 'test_steer_babysit_fix.py' -v
"""
from __future__ import annotations

import io

import store
from holophyte.babysit.maintainer_notes import PREFIX
from holophyte.cli.operator import steer_ticket
from store import steer_notes
from tests.babysit_fixture import BabysitHelpers
from tests.fake_agent import APPROVE, REQUEST_CHANGES, Commit, Idle
from tests.loop_fixture import MergeModeFixture
from tests.test_steer import PR_URL, SteerFixture

NOTE = "also log the port"


class RecordingTests(SteerFixture):
    def test_a_run_verifying_a_fix_on_its_pull_request_takes_the_note(self):
        run = self.claim()
        for phase in ("working", "verifying"):
            store.set_phase(self.conn, run, phase)
        self.assertTrue(steer_notes.close(self.conn, run))
        store.set_pull_request(self.conn, run, PR_URL)

        self.steer("-n", NOTE)

        self.assertEqual(self.read("SELECT runId FROM interventions"
                                   " WHERE action = 'steer'"), [(run,)])
        ((event_id,),) = self.read(f"SELECT id FROM runEvents WHERE runId = {run}"
                                   " AND kind = 'operator_note'")
        self.assertEqual(self.read("SELECT runId, note, eventId FROM steerNotes"),
                         [(run, NOTE, event_id)])
        self.assertEqual(self.read("SELECT phase, endedAt FROM runs"),
                         [("verifying", None)])


class SteerBabysitFixTests(BabysitHelpers, MergeModeFixture):
    def steering_commit(self, said, phases):
        fixture = self

        class SteeringCommit(Commit):
            def play(self, cwd, turn):
                phases.append(fixture.read(
                    "SELECT phase FROM runs ORDER BY id DESC LIMIT 1")[0][0])
                try:
                    steer_ticket(fixture.project, "KO-131", NOTE,
                                 author="maintainer", out=said)
                except SystemExit as refused:
                    said.write(str(refused))
                return super().play(cwd, turn)

        return SteeringCommit

    def test_a_note_steered_in_a_review_fix_turn_drives_a_fix_before_the_merge(self):
        self.resume_rejected_fix()
        said, phases = io.StringIO(), []
        SteeringCommit = self.steering_commit(said, phases)

        fake, _ = self.loop(REQUEST_CHANGES, SteeringCommit("review fix"),
                            APPROVE, Idle(""),
                            Commit("apply the maintainer's note"),
                            APPROVE, Idle(""), provider=self.provider())

        self.assertEqual(phases, ["reviewing"])
        self.assertIn("landing at its babysitter's next fix round",
                      said.getvalue())
        self.assertEqual(fake.roles, ["review", "implement", "review",
                                      "implement", "implement", "review",
                                      "implement"])
        self.assertNotIn(NOTE, fake.turns[1].goal)
        ((event_id,),) = self.read(
            "SELECT id FROM runEvents WHERE kind = 'operator_note'"
            " AND payload LIKE '%also log the port%'")
        self.assertIn(f"{PREFIX}\noperator_note event {event_id}",
                      fake.turns[4].goal)
        self.assertIn(NOTE, fake.turns[4].goal)
        self.assertEqual(self.read(
            "SELECT count(*) FROM runEvents WHERE kind = 'operator_note_consumed'"
            " AND json_extract(payload, '$.event_id') = %d" % event_id), [(1,)])
        self.assertEqual(self.read(
            "SELECT outcome FROM runs ORDER BY id DESC LIMIT 1"), [("merged",)])
        (merged,) = [v["sha"] for kind, v in self.api_calls() if kind == "merge"]
        self.assertEqual(merged, fake.turns[5].candidate_sha)
        note_fix = self.git("log", "-1", "--format=%H", "--grep",
                            "apply the maintainer's note", merged).strip()
        self.assertTrue(note_fix)

    def test_a_note_steered_in_a_conflict_merge_turn_drives_a_fix_before_the_merge(
            self):
        review = self.conflict_refusal(conflict=True)
        said, phases = io.StringIO(), []
        SteeringCommit = self.steering_commit(said, phases)
        path = "tests/test_file_sizes.py"

        fake, _ = self.loop(
            self.ratchet_work(), review, Idle(""),
            SteeringCommit("Merge main: retain both ratchets", path=path,
                           body="branch's line\nmain's line\n"),
            Commit("apply the maintainer's note"), APPROVE, Idle(""),
            provider=self.provider())

        self.assertEqual(phases, ["merge_gate"])
        self.assertIn("landing at its babysitter's next fix round",
                      said.getvalue())
        self.assertIn("mid-merge", fake.turns[3].goal)
        self.assertNotIn(NOTE, fake.turns[3].goal)
        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement", "implement", "review",
                                      "implement"])
        self.assertIn(NOTE, fake.turns[4].goal)
        self.assertEqual(self.read(
            "SELECT outcome FROM runs ORDER BY id DESC LIMIT 1"), [("merged",)])

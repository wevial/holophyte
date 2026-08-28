"""Resume semantics for the v2 store (docs/v2/state-model.md §5).

The invariant under test, in the doc's words: "the resume mutation rejects a
`guidance` argument unless the run is in `blocked_on_operator` ... This is a
validation error, not a silent drop." Plus §5's other half: a bare resume
takes no input and re-enters the phase the run left.

The phases below are transcribed from §4's diagram and §5's prose by hand, on
purpose — walking `store.RESUMABLE_PHASES` to generate them would only prove
the module agrees with itself. This transcription is the independent oracle,
and every assertion reads the stored `runs.phase` and `interventions` rows
rather than the value `resume()` returned.

Run: python3 -m unittest discover -s tests -p 'test_store*' -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import store

# §4's phase list, split by §5's rule about which of them a resume applies to.
# "Mechanically resumable — `failed`, or any of working/verifying/reviewing/
# addressing where lastHeartbeat is older than staleThresholdMs", and
# `blocked_on_operator` is the phase that is waiting for an answer.
RESUMABLE = {"failed", "working", "verifying", "reviewing", "addressing",
             "blocked_on_operator"}
NOT_RESUMABLE = {"claimed", "merge_gate", "awaiting_merge_approval", "merging",
                 "squashing", "done", "killed"}


class ResumeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = self.conn.execute(
            "INSERT INTO projects"
            " (linearTeamId, repoPath, defaultBranch, autonomyProfile)"
            " VALUES ('team_abc', '/srv/dev/holophyte', 'main', 'personal')"
        ).lastrowid
        self.ticket_id = store.mirror_ticket(
            self.conn,
            self.project_id,
            "iss_1",
            linear_identifier="HOL-1",
            title="a ticket",
            acceptance_criteria=["given/when/then"],
            verification_commands=["python3 -m unittest discover tests"],
        )
        self.attempts = 0
        self.conn.commit()

    def a_run(self, phase, resume_phase=None):
        """A run parked in `phase` the way whoever parked it would leave it.

        Written straight to the table rather than through `claim()`: these
        tests need many runs of one project and §7's lease only allows one
        active at a time, which is `claim()`'s contract to enforce, not this
        fixture's to work around.
        """
        self.attempts += 1
        run_id = self.conn.execute(
            "INSERT INTO runs"
            " (ticketId, projectId, attempt, phase, resumePhase, startedAt,"
            "  lastHeartbeat)"
            " VALUES (?, ?, ?, ?, ?, 1000, 2000)",
            (self.ticket_id, self.project_id, self.attempts, phase, resume_phase),
        ).lastrowid
        self.conn.commit()
        return run_id

    def run_row(self, run_id):
        return self.conn.execute(
            "SELECT phase, resumePhase, lastHeartbeat FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()

    def interventions(self, run_id):
        return self.conn.execute(
            'SELECT source, "trigger", "action", guidance FROM interventions'
            " WHERE runId = ? ORDER BY id",
            (run_id,),
        ).fetchall()

    # --- the invariant: guidance only where it was asked for -------------

    def test_guidance_on_a_working_run_is_refused_and_writes_nothing(self):
        run_id = self.a_run("working")
        before = self.run_row(run_id)

        with self.assertRaises(store.GuidanceNotAccepted):
            store.resume(self.conn, run_id, guidance="try the other library")

        self.assertEqual(self.run_row(run_id), before)
        self.assertEqual(self.interventions(run_id), [])

    def test_guidance_is_refused_by_every_phase_but_blocked_on_operator(self):
        for phase in (RESUMABLE | NOT_RESUMABLE) - {"blocked_on_operator"}:
            with self.subTest(phase=phase):
                run_id = self.a_run(phase)

                with self.assertRaises(store.GuidanceNotAccepted):
                    store.resume(self.conn, run_id, guidance="do it this way")

                self.assertEqual(self.run_row(run_id)[0], phase)
                self.assertEqual(self.interventions(run_id), [])

    def test_guidance_on_a_blocked_run_resumes_working_and_is_recorded(self):
        run_id = self.a_run("blocked_on_operator")

        store.resume(self.conn, run_id, guidance="use the staging bucket")

        # §4: `blocked_on_operator --> working : guidance provided`.
        self.assertEqual(self.run_row(run_id)[0], "working")
        self.assertEqual(
            self.interventions(run_id),
            [("human", "manual", "resume", "use the staging bucket")],
        )

    # --- the bare resume: back to the phase it left ----------------------

    def test_bare_resume_of_a_failed_run_re_enters_its_parked_phase(self):
        run_id = self.a_run("failed", resume_phase="verifying")

        store.resume(self.conn, run_id)

        phase, resume_phase, _ = self.run_row(run_id)
        self.assertEqual(phase, "verifying")
        # Consumed: a later failure that records nothing must not resume into
        # the phase this one left behind.
        self.assertIsNone(resume_phase)
        self.assertEqual(
            self.interventions(run_id), [("human", "manual", "resume", None)]
        )

    def test_bare_resume_of_a_failed_run_defaults_to_working(self):
        run_id = self.a_run("failed")

        store.resume(self.conn, run_id)

        # §4 draws exactly one edge out of `failed`: `failed --> working`.
        self.assertEqual(self.run_row(run_id)[0], "working")

    def test_bare_resume_of_a_stale_run_leaves_it_in_the_same_phase(self):
        run_id = self.a_run("reviewing")

        store.resume(self.conn, run_id)

        self.assertEqual(self.run_row(run_id)[0], "reviewing")
        self.assertEqual(
            self.interventions(run_id), [("human", "manual", "resume", None)]
        )

    # --- what §5 gives no resume for -------------------------------------

    def test_a_phase_with_no_resume_edge_is_refused(self):
        for phase in NOT_RESUMABLE:
            with self.subTest(phase=phase):
                run_id = self.a_run(phase)

                with self.assertRaises(store.ResumeRefused):
                    store.resume(self.conn, run_id)

                self.assertEqual(self.run_row(run_id)[0], phase)
                self.assertEqual(self.interventions(run_id), [])

    def test_resuming_a_run_that_does_not_exist_is_refused(self):
        with self.assertRaises(store.ResumeRefused):
            store.resume(self.conn, 404)

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM interventions").fetchone(),
            (0,),
        )

    def test_empty_guidance_is_not_an_answer(self):
        run_id = self.a_run("blocked_on_operator")

        with self.assertRaises(ValueError):
            store.resume(self.conn, run_id, guidance="   ")

        self.assertEqual(self.run_row(run_id)[0], "blocked_on_operator")
        self.assertEqual(self.interventions(run_id), [])

    def test_a_supervisor_resume_is_recorded_as_its_own_source(self):
        run_id = self.a_run("failed")

        store.resume(self.conn, run_id, source="supervisor")

        self.assertEqual(
            self.interventions(run_id), [("supervisor", "manual", "resume", None)]
        )


if __name__ == "__main__":
    unittest.main()

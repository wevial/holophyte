import io
import unittest

import holophyte.cli.operator
import store
from holophyte.babysit.thread_text import fix_brief
from holophyte.pr.github import Thread
from holophyte.pr.pr_status import parse_pr_url
from store.operator_notes import notes, send_back
from tests import babysit_fixture as cases
from tests.fake_agent import Commit, Idle
from tests.loop_fixture import MergeModeFixture, StubProvider, a_task


class RefreshFixNoteTests(cases.ConflictingMainHelpers, MergeModeFixture):
    def test_instruction_carried_by_refresh_fix_is_not_dispatched_again(self):
        self.parked_on_a_nit(Commit("candidate", path="THING.md"))
        command = "test -e FIXED.md -o ! -e THING.md"
        self.provider = lambda: StubProvider(
            dict(a_task(), body=self.BODY, verify=command))
        moved = self.remote_main("MOVED.md", "main moved\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        note = "Add FIXED.md to record that the merged candidate is fixed."
        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", note, out=io.StringIO())

        fake, _ = self.loop(Commit("fix merge", path="FIXED.md"), Idle(""),
                            provider=self.provider())

        self.assertIn("The merge of main introduced a verification failure",
                      fake.turns[0].goal)
        self.assertIn(f"main at {moved} passes", fake.turns[0].goal)
        self.assertIn(note, fake.turns[0].goal)
        self.assertEqual(self.git("show", f"{self.pushed()[-1][1]}:FIXED.md"),
                         "fix merge\n")
        with store.open(str(self.project.store_path)) as conn:
            current = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            instruction, = notes(conn, current)
            self.assertEqual(notes(conn, current, pending=True), [])
            self.assertTrue(instruction["consumed"])
            self.assertEqual(instruction["run_id"], current)
            count, = conn.execute(
                "SELECT COUNT(*) FROM runEvents WHERE runId = ?"
                " AND kind = 'operator_note_consumed'"
                " AND json_extract(payload, '$.event_id') = ?",
                (current, instruction["event_id"])).fetchone()
            self.assertEqual(count, 1)
        for turn in fake.turns[1:]:
            self.assertNotIn("Threads to address", turn.goal)

    def test_hint_stays_pending_until_the_thread_fix_receives_it(self):
        self.parked_on_a_nit(Commit("candidate", path="THING.md"))
        command = "test -e FIXED.md -o ! -e THING.md"
        self.provider = lambda: StubProvider(
            dict(a_task(), body=self.BODY, verify=command))
        self.remote_main("MOVED.md", "main moved\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        hint = "Consider documenting the merged candidate in HINT.md."
        with store.open(str(self.project.store_path)) as conn:
            parked = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            note_id = send_back(conn, parked, hint, "maintainer", hint=True)
        db = str(self.project.store_path)
        pending_at_refresh = []

        class RefreshCommit(Commit):
            def play(self, cwd, turn):
                with store.open(db) as conn:
                    current = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
                    pending_at_refresh.extend(notes(conn, current, pending=True))
                return super().play(cwd, turn)

        fake, _ = self.loop(RefreshCommit("fix merge", path="FIXED.md"),
                            Commit("apply hint", path="HINT.md"), Idle(""),
                            provider=self.provider())

        self.assertIn("The merge of main introduced a verification failure",
                      fake.turns[0].goal)
        self.assertNotIn(hint, fake.turns[0].goal)
        self.assertEqual([n["event_id"] for n in pending_at_refresh], [note_id])
        self.assertIn("Threads to address", fake.turns[1].goal)
        self.assertIn(hint, fake.turns[1].goal)
        with store.open(db) as conn:
            current = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            instruction, = notes(conn, current)
            self.assertTrue(instruction["consumed"])
            self.assertEqual(instruction["run_id"], current)
            self.assertEqual(notes(conn, current, pending=True), [])


class ThreadFixBriefTests(unittest.TestCase):
    def test_brief_forbids_history_rewrites_and_keeps_thread_summary_suffix(self):
        pull = parse_pr_url("https://github.com/example/repo/pull/7")
        thread = Thread("thread-1", "app.py", 4, "reviewer",
                        "Handle empty input", pull.url)
        prompt = fix_brief(pull, [(1, thread, "Empty input crashes")], "Ticket")
        self.assertIn("Never amend, rebase or squash commits already on the"
                      " branch: the factory only fast-forwards it.", prompt)
        self.assertTrue(prompt.endswith(
            "THREAD n: one sentence saying what changed"))


if __name__ == "__main__":
    unittest.main()

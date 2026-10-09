"""What a turn leaves in its worktree: a clean tree, a WIP, a merge or a backup."""
import re
import subprocess
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from holophyte.agents.agent_output import ImplementerOutput
from holophyte.loop import implement
from holophyte.loop.runs import RunSwept
from tests.sweep_fixture import SweepTestCase

SWEEP_EVENTS = ("wip_committed", "merge_completed", "merge_aborted")


class TurnTreeSweepTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.run_id = self.a_run()
        for args in (["init", "-q", "-b", "main"],
                     ["config", "user.name", "tester"],
                     ["config", "user.email", "tester@example.invalid"]):
            self.git(*args)
        self.write("a.txt", "base a\n")
        self.write("b.txt", "base b\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "task")
        self.write("a.txt", "branch a\n")
        self.write("b.txt", "branch b\n")
        self.git("commit", "-q", "-am", "branch work")
        self.git("checkout", "-q", "main")
        self.write("a.txt", "main a\n")
        self.write("b.txt", "main b\n")
        self.git("commit", "-q", "-am", "main work")
        self.main = self.git("rev-parse", "main")
        self.git("checkout", "-q", "task")
        self.tip = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.target, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, name, text):
        (self.target / name).write_text(text)

    def merge_main(self):
        subprocess.run(["git", "merge", "-q", "main"], cwd=self.target,
                       capture_output=True)

    def turn(self, play, role="implement"):
        def agent(*args, **kwargs):
            return play() or "turn over"
        with patch.object(implement, "agent", side_effect=agent), \
                patch.object(implement, "heartbeat_while",
                             return_value=nullcontext()):
            implement._timed(self.project, self.conn, self.run_id, 1,
                             self.target, 1, "goal", role=role)

    def assert_one_wip_and_clean(self):
        self.assertEqual(self.git("rev-list", f"{self.tip}..HEAD"),
                         self.git("rev-parse", "HEAD"))
        self.assertTrue(self.git("log", "-1", "--format=%s").startswith("WIP:"))
        self.assertEqual(self.git("status", "--porcelain"), "")

    def mid_merge(self):
        return subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                              cwd=self.target).returncode == 0

    def sweep_events(self):
        marks = ",".join("?" * len(SWEEP_EVENTS))
        return self.conn.execute(
            f"SELECT kind, summary FROM runEvents WHERE runId = ? AND kind IN"
            f" ({marks})", (self.run_id, *SWEEP_EVENTS)).fetchall()

    def test_uncommitted_edits_become_one_wip_commit(self):
        def edit():
            self.write("a.txt", "an edit the turn never committed\n")
            self.write("new.txt", "a file the turn created\n")
        self.turn(edit)

        self.assert_one_wip_and_clean()
        self.assertEqual(self.git("show", "HEAD:new.txt"), "a file the turn created")

    def test_a_turn_that_exits_failing_still_has_its_edits_committed(self):
        def edit_then_fail():
            self.write("a.txt", "an edit before the turn gave up\n")
            return ImplementerOutput("gave up", 1, "fake")
        self.turn(edit_then_fail)

        self.assert_one_wip_and_clean()

    def test_a_turn_ended_by_a_swept_run_still_has_its_edits_committed(self):
        def edit_then_get_swept():
            self.write("a.txt", "an edit before the run was swept\n")
            raise RunSwept(self.run_id, "failed", "stale heartbeat")
        with self.assertRaises(RunSwept):
            self.turn(edit_then_get_swept)

        self.assert_one_wip_and_clean()

    def test_a_trim_turn_s_edits_are_committed_too(self):
        self.turn(lambda: self.write("a.txt", "a trim edit\n"), role="trim")

        self.assert_one_wip_and_clean()

    def test_a_resolved_but_uncommitted_merge_is_committed(self):
        def resolve():
            self.merge_main()
            self.write("a.txt", "branch a\nmain a\n")
            self.write("b.txt", "branch b\nmain b\n")
            self.git("add", "a.txt", "b.txt")
        self.turn(resolve)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("log", "-1", "--format=%P").split(),
                         [self.tip, self.main])
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("show", "HEAD:a.txt"), "branch a\nmain a")

    def test_a_still_conflicted_merge_is_backed_up_then_aborted(self):
        def half_resolve():
            self.merge_main()
            self.write("b.txt", "branch b\nmain b\n")
            self.git("add", "b.txt")
        self.turn(half_resolve)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.target / "a.txt").read_text(), "branch a\n")
        self.assertEqual((self.target / "b.txt").read_text(), "branch b\n")
        ((kind, summary),) = self.sweep_events()
        self.assertEqual(kind, "merge_aborted")
        (backup,) = re.findall(r"\b[0-9a-f]{40}\b", summary)
        self.git("cat-file", "-e", f"{backup}^{{commit}}")
        self.assertEqual(self.git("show", f"{backup}:b.txt"), "branch b\nmain b")
        self.assertIn("<<<<<<<", self.git("show", f"{backup}:a.txt"))

    def test_a_conflicted_merge_with_a_staged_then_re_edited_file_is_aborted(self):
        def stage_then_re_edit():
            self.merge_main()
            self.write("b.txt", "branch b\nmain b\n")
            self.git("add", "b.txt")
            self.write("b.txt", "edited after staging\n")
        self.turn(stage_then_re_edit)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.target / "b.txt").read_text(), "branch b\n")
        ((kind, summary),) = self.sweep_events()
        self.assertEqual(kind, "merge_aborted")
        (backup,) = re.findall(r"\b[0-9a-f]{40}\b", summary)
        self.assertEqual(self.git("show", f"{backup}:b.txt"), "edited after staging")
        self.assertEqual(self.git("show", f"{backup}^3:b.txt"), "branch b\nmain b")

    def test_a_conflicted_merge_is_not_discarded_when_its_backup_is_not_recorded(self):
        def leave_conflicted():
            self.merge_main()
            self.write("b.txt", "branch b\nmain b\n")
        refused = RuntimeError("the store refused the write")
        with patch.object(implement.store, "record_event", side_effect=refused):
            self.turn(leave_conflicted)

        self.assertTrue(self.mid_merge())
        self.assertEqual((self.target / "b.txt").read_text(), "branch b\nmain b\n")

    def test_a_clean_turn_leaves_head_and_the_record_alone(self):
        self.turn(lambda: None)

        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual(self.sweep_events(), [])


if __name__ == "__main__":
    unittest.main()

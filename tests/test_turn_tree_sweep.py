"""What a turn leaves in its worktree: a clean tree, a WIP, a merge or a backup."""
import json
import re
import subprocess
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from holophyte.agents.agent_output import ImplementerOutput
from holophyte.loop import implement
from holophyte.loop.gates import InfraFailure
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

    def merge_main(self, *flags):
        subprocess.run(["git", "merge", "-q", *flags, "main"], cwd=self.target,
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

    def test_an_aborted_merge_keeps_an_edit_made_before_the_merge(self):
        self.write("c.txt", "base c\n")
        self.git("add", "c.txt")
        self.git("commit", "-q", "-m", "add c")
        self.tip = self.git("rev-parse", "HEAD")

        def edit_then_merge():
            self.write("c.txt", "edited before the merge\n")
            self.merge_main()
            self.write("b.txt", "branch b\nmain b\n")
            self.git("add", "b.txt")
        self.turn(edit_then_merge)

        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / "a.txt").read_text(), "branch a\n")
        self.assertEqual((self.target / "b.txt").read_text(), "branch b\n")
        self.assertEqual((self.target / "c.txt").read_text(),
                         "edited before the merge\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("show", "HEAD:c.txt"), "edited before the merge")
        self.assertEqual([kind for kind, _ in self.sweep_events()],
                         ["merge_aborted", "wip_committed"])

    def test_an_aborted_merge_keeps_a_pre_merge_edit_staged_with_everything(self):
        self.write("c.txt", "base c\n")
        self.git("add", "c.txt")
        self.git("commit", "-q", "-m", "add c")
        self.tip = self.git("rev-parse", "HEAD")

        def edit_merge_then_stage_all():
            self.write("c.txt", "edited before the merge\n")
            self.merge_main()
            self.write("b.txt", "branch b\nmain b\n")
            self.git("add", "-A")
        self.turn(edit_merge_then_stage_all)

        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / "a.txt").read_text(), "branch a\n")
        self.assertEqual(self.git("show", "HEAD:c.txt"), "edited before the merge")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_an_aborted_merge_keeps_an_edit_to_a_file_both_sides_changed_alike(self):
        for branch in ("main", "task"):
            self.git("checkout", "-q", branch)
            self.write("s.txt", "same\n")
            self.git("add", "s.txt")
            self.git("commit", "-q", "-m", "same change")
        self.tip = self.git("rev-parse", "HEAD")

        def edit_then_merge():
            self.write("s.txt", "edited before the merge\n")
            self.merge_main()
        self.turn(edit_then_merge)

        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / "a.txt").read_text(), "branch a\n")
        self.assertEqual((self.target / "s.txt").read_text(),
                         "edited before the merge\n")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_an_aborted_merge_leaves_none_of_main_s_changes_in_the_tree(self):
        self.git("checkout", "-q", "-b", "shared", "main~1")
        self.write("r.txt", "".join(f"line {n}\n" for n in range(9)))
        (self.target / "bin.dat").write_bytes(b"\0shared")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "add r and bin")
        for branch in ("main", "task"):
            self.git("checkout", "-q", branch)
            self.git("merge", "-q", "--no-edit", "shared")
        self.git("mv", "r.txt", "r2.txt")
        (self.target / "bin.dat").write_bytes(b"\0task")
        self.git("commit", "-q", "-am", "rename r, change bin")
        self.tip = self.git("rev-parse", "HEAD")
        renamed = (self.target / "r2.txt").read_text()
        self.git("checkout", "-q", "main")
        self.write("r.txt", "main line 0\n" + renamed.split("\n", 1)[1])
        (self.target / "bin.dat").write_bytes(b"\0main")
        self.git("commit", "-q", "-am", "edit r and bin")
        self.git("checkout", "-q", "task")

        def take_main_s_binary():
            self.merge_main()
            self.git("checkout", "-q", "--theirs", "bin.dat")
            self.git("add", "bin.dat")
        self.turn(take_main_s_binary)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual((self.target / "r2.txt").read_text(), renamed)
        self.assertFalse((self.target / "r.txt").exists())
        self.assertEqual((self.target / "bin.dat").read_bytes(), b"\0task")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_a_merge_staged_with_its_conflict_markers_is_backed_up_then_aborted(self):
        def stage_the_markers():
            self.merge_main()
            self.git("add", "a.txt", "b.txt")
        self.turn(stage_the_markers)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual((self.target / "a.txt").read_text(), "branch a\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        ((kind, summary),) = self.sweep_events()
        self.assertEqual(kind, "merge_aborted")
        (backup,) = re.findall(r"\b[0-9a-f]{40}\b", summary)
        self.assertIn("<<<<<<<", self.git("show", f"{backup}:a.txt"))

    def test_staged_markers_of_a_configured_size_are_backed_up_then_aborted(self):
        attributes = self.git("rev-parse", "--git-path", "info/attributes")
        (self.target / attributes).parent.mkdir(exist_ok=True)
        (self.target / attributes).write_text("*.txt conflict-marker-size=9\n")

        def stage_the_markers():
            self.merge_main()
            self.git("add", "a.txt", "b.txt")
        self.turn(stage_the_markers)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual((self.target / "a.txt").read_text(), "branch a\n")
        ((kind, summary),) = self.sweep_events()
        self.assertEqual(kind, "merge_aborted")
        (backup,) = re.findall(r"\b[0-9a-f]{40}\b", summary)
        self.assertIn("<" * 9 + " ", self.git("show", f"{backup}:a.txt"))

    def test_an_aborted_autostashed_merge_restores_the_stashed_edit(self):
        def edit_then_autostash_merge():
            self.write("a.txt", "edited before the merge\n")
            self.merge_main("--autostash")
        self.turn(edit_then_autostash_merge)

        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / "a.txt").read_text(),
                         "edited before the merge\n")
        self.assertEqual((self.target / "b.txt").read_text(), "branch b\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("stash", "list"), "")

    def test_a_resolved_autostashed_merge_wips_its_stash_without_markers(self):
        self.write("c.txt", "base c\n")
        self.git("add", "c.txt")
        self.git("commit", "-q", "-m", "add c")
        self.tip = self.git("rev-parse", "HEAD")

        def autostash_merge_then_resolve():
            self.write("a.txt", "edited before the merge\n")
            self.write("c.txt", "edited before the merge\n")
            self.merge_main("--autostash")
            self.write("a.txt", "branch a\nmain a\n")
            self.write("b.txt", "branch b\nmain b\n")
            self.git("add", "a.txt", "b.txt")
        self.turn(autostash_merge_then_resolve)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "HEAD~1^1", "HEAD~1^2").split(),
                         [self.tip, self.main])
        self.assertEqual((self.target / "a.txt").read_text(), "branch a\nmain a\n")
        self.assertEqual(self.git("show", "HEAD:c.txt"), "edited before the merge")
        self.assertEqual(len(self.git("stash", "list").splitlines()), 1)

    def test_markers_moved_to_a_renamed_path_still_abort_the_merge(self):
        def rename_the_markers_then_resolve_the_rest():
            self.merge_main()
            (self.target / "a.txt").rename(self.target / "c.txt")
            self.write("b.txt", "branch b\nmain b\n")
            self.git("add", "-A")
        self.turn(rename_the_markers_then_resolve_the_rest)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual((self.target / "a.txt").read_text(), "branch a\n")
        self.assertFalse((self.target / "c.txt").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        ((kind, summary),) = self.sweep_events()
        self.assertEqual(kind, "merge_aborted")
        (backup,) = re.findall(r"\b[0-9a-f]{40}\b", summary)
        self.assertIn("<<<<<<<", self.git("show", f"{backup}:c.txt"))

    def test_an_aborted_merge_drops_a_resolution_staged_under_a_new_name(self):
        def resolve_b_into_a_new_name():
            self.merge_main()
            self.write("d.txt", "branch b\nmain b\n")
            (self.target / "b.txt").unlink()
            self.git("add", "-A", "b.txt", "d.txt")
        self.turn(resolve_b_into_a_new_name)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.tip)
        self.assertEqual((self.target / "b.txt").read_text(), "branch b\n")
        self.assertFalse((self.target / "d.txt").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        ((kind, summary),) = self.sweep_events()
        (backup,) = re.findall(r"\b[0-9a-f]{40}\b", summary)
        self.assertEqual(self.git("show", f"{backup}:d.txt"), "branch b\nmain b")
        self.assertIn("d.txt", summary)

    def test_a_stash_clash_on_a_non_ascii_path_is_kept_out_of_the_tree(self):
        name = "café.txt"
        base = self.git("merge-base", "task", "main")
        self.git("checkout", "-q", "-b", "with-cafe", base)
        self.write(name, "c\n")
        self.git("add", name)
        self.git("commit", "-q", "-m", "add the file")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "with-cafe")
        self.write(name, "main c\n")
        self.git("commit", "-q", "-am", "main edits the file")
        self.main = self.git("rev-parse", "main")
        self.git("checkout", "-q", "task")
        self.git("merge", "-q", "with-cafe")
        self.tip = self.git("rev-parse", "HEAD")

        def autostash_merge_then_resolve():
            self.write(name, "edited before the merge\n")
            self.merge_main("--autostash")
            self.write("a.txt", "branch a\nmain a\n")
            self.write("b.txt", "branch b\nmain b\n")
            self.git("add", "a.txt", "b.txt")
        self.turn(autostash_merge_then_resolve)

        self.assertFalse(self.mid_merge())
        self.assertEqual(self.git("rev-parse", "HEAD^1", "HEAD^2").split(),
                         [self.tip, self.main])
        self.assertEqual((self.target / name).read_text(), "main c\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(len(self.git("stash", "list").splitlines()), 1)

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

    def test_no_wip_is_committed_on_a_branch_that_carries_the_environment(self):
        source = self.root / "source.env"
        source.write_text("PUBLIC=value\n")
        self.configure(f'[worktree]\nenv_source = "{source}"\n')
        self.write(".env", "PUBLIC=value\n")
        self.git("add", "-f", ".env")
        self.git("commit", "-q", "-m", "carries the environment")
        carried = self.git("rev-parse", "HEAD")

        with self.assertRaisesRegex(InfraFailure, r"\.env"):
            self.turn(lambda: self.write("a.txt", "an uncommitted edit\n"))

        self.assertEqual(self.git("rev-parse", "HEAD"), carried)
        self.assertEqual(self.sweep_events(), [])

    def ignoring(self):
        self.write(".gitignore", "gen.css\n*.log\ndeps/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "ignore generated files")
        self.write("old.log", "before the turn\n")
        (self.target / "deps").mkdir()
        self.write("deps/kept.txt", "set up before the turn\n")

    def leftover_events(self):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId = ? AND kind ="
            " 'ignored_leftovers_removed'", (self.run_id,))]

    def present(self, *names):
        return [name for name in names if (self.target / name).exists()]

    def test_a_crashed_turn_s_new_ignored_files_are_removed(self):
        self.ignoring()

        def write_then_crash():
            self.write("gen.css", "generated\n")
            self.write("new.log", "written by the turn\n")
            self.write("deps/added.txt", "added inside an existing ignored dir\n")
            return ImplementerOutput("crashed", -4, "fake")
        self.turn(write_then_crash)

        self.assertEqual(self.present("gen.css", "new.log", "old.log",
                                      "deps/kept.txt", "deps/added.txt"),
                         ["old.log", "deps/kept.txt", "deps/added.txt"])
        self.assertEqual(self.leftover_events(),
                         [{"cause": "crashed", "paths": ["gen.css", "new.log"]}])

    def test_a_timed_out_turn_s_new_ignored_files_are_removed(self):
        self.ignoring()

        def write_then_time_out():
            self.write("gen.css", "generated\n")
            raise subprocess.TimeoutExpired("agent", 1)
        self.turn(write_then_time_out)

        self.assertEqual(self.present("gen.css", "old.log"), ["old.log"])
        self.assertEqual(self.leftover_events(),
                         [{"cause": "budget fired", "paths": ["gen.css"]}])

    def test_a_turn_ended_by_a_swept_run_keeps_only_its_older_ignored_files(self):
        self.ignoring()

        def write_then_get_swept():
            self.write("gen.css", "generated\n")
            raise RunSwept(self.run_id, "failed", "stale heartbeat")
        with self.assertRaises(RunSwept):
            self.turn(write_then_get_swept)

        self.assertEqual(self.present("gen.css", "old.log", "deps/kept.txt"),
                         ["old.log", "deps/kept.txt"])
        self.assertEqual(self.leftover_events(),
                         [{"cause": "ended", "paths": ["gen.css"]}])

    def test_a_turn_that_exits_cleanly_keeps_its_new_ignored_files(self):
        self.ignoring()

        def write_then_stop():
            self.write("gen.css", "generated\n")
            return ImplementerOutput("done", 0, "fake")
        self.turn(write_then_stop)

        self.assertEqual(self.present("gen.css"), ["gen.css"])
        self.assertEqual(self.leftover_events(), [])

    def test_a_turn_that_stops_mid_edit_drops_its_new_ignored_files(self):
        self.ignoring()
        self.tip = self.git("rev-parse", "HEAD")

        def edit_write_then_stop():
            self.write("a.txt", "an edit the turn never committed\n")
            self.write("gen.css", "generated\n")
            return ImplementerOutput("done", 0, "fake")
        self.turn(edit_write_then_stop)

        self.assert_one_wip_and_clean()
        self.assertEqual(self.present("gen.css", "old.log"), ["old.log"])
        self.assertEqual(self.leftover_events(),
                         [{"cause": "stopped", "paths": ["gen.css"]}])

    def test_a_turn_that_fails_mid_edit_drops_its_new_ignored_files(self):
        self.ignoring()
        self.tip = self.git("rev-parse", "HEAD")

        def edit_write_then_fail():
            self.write("a.txt", "an edit before the turn gave up\n")
            self.write("gen.css", "generated\n")
            return ImplementerOutput("gave up", 1, "fake")
        self.turn(edit_write_then_fail)

        self.assert_one_wip_and_clean()
        self.assertEqual(self.present("gen.css", "old.log"), ["old.log"])
        self.assertEqual(self.leftover_events(),
                         [{"cause": "failed", "paths": ["gen.css"]}])

    def test_a_crashed_turn_keeps_what_it_wrote_under_a_carry_directory(self):
        self.write(".gitignore", "gen.css\ndeps/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "ignore generated files")
        self.configure('[worktree]\ncarry = ["deps"]\n')

        def install_then_crash():
            (self.target / "deps").mkdir()
            self.write("deps/added.txt", "installed by the turn\n")
            self.write("gen.css", "generated\n")
            return ImplementerOutput("crashed", -4, "fake")
        self.turn(install_then_crash)

        self.assertEqual(self.present("deps/added.txt", "gen.css"),
                         ["deps/added.txt"])
        self.assertEqual(self.leftover_events(),
                         [{"cause": "crashed", "paths": ["gen.css"]}])

    def test_a_crashed_turn_s_ignored_directory_is_removed_without_captures(self):
        self.write(".gitignore", "e2e/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-q", "-m", "ignore e2e")
        self.configure('[merge]\nui_capture_dir = "e2e/capture"\n')

        def write_then_crash():
            (self.target / "e2e").mkdir()
            self.write("e2e/temp.log", "written by the turn\n")
            return ImplementerOutput("crashed", -4, "fake")
        self.turn(write_then_crash)

        self.assertEqual(self.present("e2e"), [])
        self.assertEqual(self.leftover_events(),
                         [{"cause": "crashed", "paths": ["e2e/"]}])

    def test_a_crashed_turn_s_ignored_file_with_a_carriage_return_is_removed(self):
        self.ignoring()

        def write_then_crash():
            self.write("new\r.log", "written by the turn\n")
            return ImplementerOutput("crashed", -4, "fake")
        self.turn(write_then_crash)

        self.assertEqual(self.present("new\r.log"), [])
        self.assertEqual(self.leftover_events(),
                         [{"cause": "crashed", "paths": ["new\r.log"]}])


if __name__ == "__main__":
    unittest.main()

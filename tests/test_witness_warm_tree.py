"""Loop witness passes reuse one warm tree per project, rerunning a declared
setup step only when its inputs changed at the commit under test.

Run: python3 -m unittest discover -s tests -p 'test_witness_warm_tree.py' -v
"""
import fcntl
import json
import os
import shlex
import shutil
import unittest
from pathlib import Path

from holophyte.config.checks import check_worktree_setup
from holophyte.loop.task_worktree import prepare_worktree
from holophyte.story.witness import run_witnesses, witness_pass
from tests.test_witness_runner import PASSES, WitnessRunnerFixture, commit_file, git


def toml_string(text):
    return json.dumps(text)


def table_step(run, inputs, keep=()):
    return (f"{{ run = {toml_string(run)}, inputs = {json.dumps(list(inputs))},"
            f" keep = {json.dumps(list(keep))} }}")


def setup_config(*entries):
    return "[worktree]\nsetup = [\n" + "".join(
        f"  {entry},\n" for entry in entries) + "]\n"


class WarmTreeFixture(WitnessRunnerFixture):
    def setUp(self):
        super().setUp()
        self.table_count = self.root / "table-count"
        self.plain_count = self.root / "plain-count"

    def count(self, path):
        return len(path.read_text().splitlines()) if path.exists() else 0

    def appends(self, counter):
        return f"echo ran >> {shlex.quote(str(counter))}"

    def deps_step(self, counter=None, inputs=("deps.lock",), extra=""):
        run = (f"{self.appends(counter or self.table_count)}"
               f" && mkdir -p deps && touch deps/marker{extra}")
        return table_step(run, inputs, ["deps"])

    def marker_story(self):
        for key in ("w1", "w2"):
            commit_file(self.repo, f"tests/test_{key}.py", PASSES, f"{key} lands")
        self.approve([(key, f"tests/test_{key.lower()}.py", "test -f deps/marker",
                       PASSES) for key in ("W1", "W2")])

    def loop_pass(self, target, sha):
        return [(row.witnessKey, row.verdict) for row in run_witnesses(
            target, self.conn, self.story_id, sha, "loop")]

    def warm(self, target):
        return Path(target.holo_dir) / "witness-tree"


class WarmTreeReuseTests(WarmTreeFixture, unittest.TestCase):
    def test_a_table_step_runs_once_until_its_input_or_keep_path_changes(self):
        commit_file(self.repo, "deps.lock", "v1\n", "lock")
        target = self.project(setup_config(
            self.deps_step(), toml_string(self.appends(self.plain_count))))
        self.marker_story()
        green = [("W1", "green"), ("W2", "green")]
        a = commit_file(self.repo, "notes.txt", "a\n", "a")
        b = commit_file(self.repo, "notes.txt", "b\n", "b")

        self.assertEqual(self.loop_pass(target, a) + self.loop_pass(target, b),
                         green + green)
        self.assertEqual((self.count(self.table_count),
                          self.count(self.plain_count)), (1, 4))

        c = commit_file(self.repo, "deps.lock", "v2\n", "lock moves")
        self.assertEqual(self.loop_pass(target, c), green)
        self.assertEqual(self.count(self.table_count), 2)

        shutil.rmtree(self.warm(target) / "deps")
        d = commit_file(self.repo, "notes.txt", "d\n", "d")
        self.assertEqual(self.loop_pass(target, d), green)
        self.assertEqual(self.count(self.table_count), 3)

    def test_a_rerun_table_step_reruns_every_later_table_step(self):
        commit_file(self.repo, "first.lock", "v1\n", "first")
        commit_file(self.repo, "second.lock", "v1\n", "second")
        second_count = self.root / "second-count"
        target = self.project(setup_config(
            self.deps_step(inputs=["first.lock"]),
            table_step(self.appends(second_count), ["second.lock"], ["deps"])))
        self.marker_story()
        a = commit_file(self.repo, "notes.txt", "a\n", "a")
        self.loop_pass(target, a)

        b = commit_file(self.repo, "first.lock", "v2\n", "first moves")
        self.loop_pass(target, b)

        self.assertEqual((self.count(self.table_count),
                          self.count(second_count)), (2, 2))

    def test_a_failed_table_step_runs_again_at_the_next_commit(self):
        commit_file(self.repo, "deps.lock", "v1\n", "lock")
        fixed = self.root / "fixed"
        target = self.project(setup_config(self.deps_step(
            extra=f" && test -f {shlex.quote(str(fixed))}")))
        self.marker_story()
        a = commit_file(self.repo, "notes.txt", "a\n", "a")
        self.assertEqual([verdict for _, verdict in self.loop_pass(target, a)],
                         ["red", "red"])
        runs_at_a = self.count(self.table_count)
        fixed.touch()

        b = commit_file(self.repo, "notes.txt", "b\n", "b")

        self.assertEqual(self.loop_pass(target, b),
                         [("W1", "green"), ("W2", "green")])
        self.assertEqual(self.count(self.table_count), runs_at_a + 1)


class WarmTreeCleanTests(WarmTreeFixture, unittest.TestCase):
    def test_a_witness_leaves_no_untracked_or_ignored_file_for_the_next(self):
        commit_file(self.repo, ".gitignore", "build/\ndeps/\n", "ignore")
        commit_file(self.repo, "deps.lock", "v1\n", "lock")
        target = self.project(setup_config(self.deps_step()))
        absent = "test ! -e scratch.txt && test ! -e build/out"
        leaves = "touch scratch.txt && mkdir -p build && touch build/out"
        for key in ("w1", "w2"):
            commit_file(self.repo, f"tests/test_{key}.py", PASSES, f"{key} lands")
        self.approve([("W1", "tests/test_w1.py", f"{absent} && {leaves}", PASSES),
                      ("W2", "tests/test_w2.py", f"{absent} && {leaves}", PASSES)])
        green = [("W1", "green"), ("W2", "green")]

        self.assertEqual(self.loop_pass(target, git(self.repo, "rev-parse", "HEAD")),
                         green)
        b = commit_file(self.repo, "notes.txt", "b\n", "b")
        self.assertEqual(self.loop_pass(target, b), green)


class WarmTreeFallbackTests(WarmTreeFixture, unittest.TestCase):
    def test_a_held_warm_tree_runs_full_setup_and_leaves_the_stamps_alone(self):
        commit_file(self.repo, "deps.lock", "v1\n", "lock")
        target = self.project(setup_config(self.deps_step()))
        self.marker_story()
        self.loop_pass(target, git(self.repo, "rev-parse", "HEAD"))
        stamps = Path(target.holo_dir) / "witness-tree.json"
        before = stamps.read_bytes()
        runs = self.count(self.table_count)
        holder = os.open(Path(target.holo_dir) / "witness-tree.lock", os.O_RDWR)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        b = commit_file(self.repo, "notes.txt", "b\n", "b")

        self.assertEqual(self.loop_pass(target, b),
                         [("W1", "green"), ("W2", "green")])
        self.assertEqual(self.count(self.table_count), runs + 2)
        self.assertEqual(stamps.read_bytes(), before)

    def test_a_hand_run_pass_makes_no_warm_tree(self):
        commit_file(self.repo, "deps.lock", "v1\n", "lock")
        target = self.project(setup_config(self.deps_step()))
        self.marker_story()

        rows = witness_pass(target, self.conn, self.story_id, "operator")

        self.assertEqual([row.verdict for row in rows], ["green", "green"])
        self.assertFalse(self.warm(target).exists())


class SetupTableConfigTests(WarmTreeFixture, unittest.TestCase):
    def test_a_malformed_table_entry_is_refused_naming_its_position(self):
        valid = table_step("true", ["deps.lock"])
        for entry, problem in (
                ('{ run = "  ", inputs = ["deps.lock"] }', "run"),
                ('{ run = "true", inputs = "deps.lock" }', "inputs must be a list"),
                ('{ run = "true", inputs = ["/etc/passwd"] }', "'/etc/passwd'"),
                ('{ run = "true", inputs = ["a"], keep = ["../deps"] }',
                 "keep: '../deps'"),
                ('{ run = "true", inputs = ["a"], cache = true }', "'cache'")):
            with self.subTest(entry=entry):
                target = self.project(setup_config(valid, entry))

                with self.assertRaises(SystemExit) as raised:
                    check_worktree_setup(target)

                self.assertIn("entry 2", str(raised.exception))
                self.assertIn(problem, str(raised.exception))

    def test_a_task_worktree_runs_a_table_step_on_every_preparation(self):
        target = self.project(setup_config(
            table_step(self.appends(self.table_count), ["README.md"])))
        wt = self.root / "task"
        git(self.repo, "worktree", "add", "-q", "--detach", str(wt))

        for _ in range(2):
            self.assertTrue(prepare_worktree(target, wt)[0])

        self.assertEqual(self.count(self.table_count), 2)


if __name__ == "__main__":
    unittest.main()

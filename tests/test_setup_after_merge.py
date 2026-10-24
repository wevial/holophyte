"""`[worktree] setup` reruns in the task worktree after a merge of main
that moved the branch, and a main that fails a different verify command
than the merged tree is not read as a red main."""
import io
import json
import unittest
from pathlib import Path

import holophyte.cli.operator
import tests.babysit_fixture as cases
from holophyte.loop import merge_gate
from holophyte.loop.gates import RunFailure
from tests.fake_agent import APPROVE, Commit, Idle
from tests.loop_fixture import (
    BRANCH,
    LoopFixture,
    MergeModeFixture,
    StubProvider,
    a_task,
)

SETUP = '[worktree]\nsetup = ["cp schema.txt generated.txt"]\n'
CMP = "cmp schema.txt generated.txt"
RERUN = "worktree setup rerun after merging main"


def ignore_generated(fixture):
    common = Path(fixture.git("rev-parse", "--path-format=absolute",
                              "--git-common-dir").strip())
    (common / "info").mkdir(exist_ok=True)
    with open(common / "info" / "exclude", "a") as exclude:
        exclude.write("generated.txt\n")


def reruns(fixture):
    return [summary for (summary,) in fixture.read(
        "SELECT summary FROM runEvents WHERE kind = 'merge_gate'"
        f" AND summary LIKE '{RERUN}%'")]


class MoveMain:
    """A review turn that commits `path` on the target's `main`, then approves."""
    role = APPROVE.role

    def __init__(self, fixture, path, body):
        self.fixture, self.path, self.body = fixture, path, body

    def play(self, cwd, turn):
        self.fixture.commit_on_main(self.path, self.body)
        return APPROVE.play(cwd, turn)


class DirectGateTests(LoopFixture):
    """Direct mode: the gate merges the local `main` into the candidate."""

    def setUp(self):
        super().setUp()
        self.commit_on_main("schema.txt", "schema v1\n")
        ignore_generated(self)

    def commit_on_main(self, path, body):
        (self.target / path).write_text(body)
        self.git("add", path)
        self.git("commit", "-q", "-m", f"main changes {path}")

    def provider(self, verify=CMP):
        return StubProvider(dict(a_task(), verify=verify))

    def test_setup_reruns_after_the_gate_merges_a_moved_main(self):
        self.configure(SETUP)
        self.loop(Commit("the scripted work"),
                  MoveMain(self, "schema.txt", "schema v2\n"),
                  provider=self.provider())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        merged = self.git("rev-parse", "main^2").strip()
        self.assertEqual(self.git("show", f"{merged}:schema.txt"), "schema v2\n")
        (event,) = reruns(self)
        self.assertIn(merged, event)
        self.assertNotIn("FAILED", event)

    def test_a_gate_that_merges_nothing_does_not_rerun_setup(self):
        self.configure(SETUP)
        self.loop(Commit("the scripted work"), APPROVE, provider=self.provider())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(reruns(self), [])

    def test_an_approved_candidate_resumed_over_a_moved_main_reruns_setup(self):
        self.configure('[merge]\napprove = "human"\n' + SETUP)
        self.loop(Commit("the scripted work"), APPROVE, provider=self.provider())
        self.commit_on_main("schema.txt", "schema v2\n")
        holophyte.cli.operator.approve(self.project, "KO-131", "ok",
                                       out=io.StringIO())
        self.loop(provider=self.provider())
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "abandoned"), (2, "merged")])
        merged = self.git("rev-parse", "main^2").strip()
        (event,) = reruns(self)
        self.assertIn(merged, event)

    def test_a_failing_gate_verify_names_the_merged_main(self):
        self.loop(Commit("the scripted work"),
                  MoveMain(self, "MOVED.md", "main moved\n"),
                  provider=self.provider("test ! -e MOVED.md"))
        main12 = self.git("rev-parse", "main").strip()[:12]
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertTrue(reason.startswith(
            "verify failed before merge on the candidate merged with main at"
            f" {main12}; branch"), reason)
        ((question,),) = self.read("SELECT blockedQuestion FROM tickets")
        self.assertTrue(question.startswith(
            "verify failed at the merge gate on the candidate merged with main"
            f" at {main12}"), question)

    def test_a_failing_gate_verify_that_merged_nothing_keeps_its_reason(self):
        wt = self.worktrees / "direct"
        self.git("worktree", "add", "-q", "-b", "direct", str(wt), "main")
        (wt / "work.txt").write_text("work\n")
        self.git("add", "work.txt", cwd=wt)
        self.git("commit", "-q", "-m", "work", cwd=wt)
        sha = self.git("rev-parse", "HEAD", cwd=wt).strip()
        with self.assertRaises(RunFailure) as failed:
            merge_gate._merge_gate(self.project, None, None, None, "KO-1",
                                   "issue-1", "direct", wt, 60, sha, "false",
                                   [], "ticket", 10)
        self.assertEqual(str(failed.exception),
                         f"verify failed before merge; branch direct preserved"
                         f" at {sha[:12]}")


class BabysitterMergeTests(cases.ConflictingMainHelpers, MergeModeFixture):
    """A conflicting pull request: the babysitter merges `origin/main`."""

    def parked(self, verify, setup, work=None):
        self.parked_on_a_nit(work or Commit("candidate", path="THING.md"))
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       + (f"[worktree]\nsetup = {json.dumps(setup)}\n"
                          if setup else ""))
        self.provider = lambda: StubProvider(
            dict(a_task(), body=self.BODY, verify=verify))
        ignore_generated(self)

    def conflicting(self, path, body):
        moved = self.remote_main(path, body)
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        return moved

    def test_setup_reruns_before_the_merged_trees_verify(self):
        self.parked(CMP, ["cp schema.txt generated.txt"])
        self.conflicting("schema.txt", "schema from main\n")
        self.resume()
        self.assertIn("waiting for a human to say merge", self.question())
        merged = self.git("rev-parse", BRANCH).strip()
        (event,) = reruns(self)
        self.assertIn(merged, event)
        self.assertNotIn("FAILED", event)

    def test_a_failing_rerun_is_recorded_and_the_verify_still_runs(self):
        self.parked("test -e FIXED.md", ["echo no-venv >&2; exit 3"])
        self.conflicting("MOVED.md", "main moved\n")
        fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertIn("test -e FIXED.md", fake.turns[0].goal)
        (event,) = reruns(self)
        self.assertIn("FAILED", event)
        self.assertIn("echo no-venv >&2; exit 3", event)

    def test_main_failing_a_different_command_is_not_red(self):
        verify = "grep -q branch README.md\ntest -e FIXED.md"
        self.parked(verify, None, Commit("candidate", path="README.md",
                                         body="the branch's line\n"))
        moved = self.conflicting("MOVED.md", "main moved\n")
        fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        goal = fake.turns[0].goal
        self.assertIn(f"The candidate merged with main at {moved}", goal)
        self.assertIn("fails a different command", goal)
        self.assertIn("grep -q branch README.md", goal)
        self.assertNotIn("main is red", self.question())

    def test_main_failing_the_same_trimmed_unittest_command_is_red(self):
        verify = "python3 -m unittest tests.test_shared tests.test_new"
        self.parked(verify, None, Commit(
            "candidate", path="tests/test_new.py",
            body="def test_new():\n    pass\n"))
        moved = self.conflicting(
            "tests/test_shared.py",
            "import unittest\n\n\nclass Shared(unittest.TestCase):\n"
            "    def test_shared(self):\n        self.fail('red')\n")
        fake, _ = self.resume()
        self.assertEqual(fake.roles, [])
        self.assertIn(f"main is red at {moved}", self.question())

    def test_main_failing_the_and_line_left_after_a_dropped_line_is_red(self):
        verify = ("python3 -m unittest tests.test_new\n"
                  "python3 -m unittest tests.test_shared && test -e README.md")
        self.parked(verify, None, Commit(
            "candidate", path="tests/test_new.py",
            body="import unittest\n\n\nclass New(unittest.TestCase):\n"
                 "    def test_new(self):\n        pass\n"))
        moved = self.conflicting(
            "tests/test_shared.py",
            "import unittest\n\n\nclass Shared(unittest.TestCase):\n"
            "    def test_shared(self):\n        self.fail('red')\n")
        fake, _ = self.resume()
        self.assertEqual(fake.roles, [])
        self.assertIn(f"main is red at {moved}", self.question())


if __name__ == "__main__":
    unittest.main()

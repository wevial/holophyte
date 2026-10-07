"""The trim step between the implement turn and the first review round.

Run: python3 -m unittest discover -s tests -p 'test_trim.py' -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
    REVIEW_ROLES,
    Commit,
    Idle,
    block_until_killed,
)
from loop_fixture import LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
from holophyte.agents.agent_output import ImplementerOutput  # noqa: E402
from holophyte.agents.roles import agent  # noqa: E402
from holophyte.loop.trim import trim  # noqa: E402
from tests.test_harness import CONFIG, FAKE_CLAUDE  # noqa: E402


def lines(count):
    return "".join(f"line {n}\n" for n in range(count))


TRIMMED = "[trim]\nbudget_min = 1\n"
WORK = Commit("work", path="work.txt", body=lines(60))


class Commits:
    """One turn that plays several scripted commits in order."""

    role = "implement"

    def __init__(self, *commits):
        self.commits = commits

    def play(self, cwd, turn):
        for commit in self.commits:
            commit.play(cwd, turn)
        return "trimmed"


class LeaveDirtyThen(Commits):
    """A trim turn that commits, edits without committing, then fails."""

    def __init__(self, failure, *commits):
        super().__init__(*commits)
        self.failure = failure

    def play(self, cwd, turn):
        super().play(cwd, turn)
        (cwd / "work.txt").write_text("uncommitted edit\n")
        (cwd / "stray.txt").write_text("a file the turn created\n")
        subprocess.run(["git", "init", "-q", "nested"], cwd=cwd, check=True)
        (cwd / "artifact.log").write_text("an ignored file the turn created\n")
        if self.failure == "timeout":
            block_until_killed(cwd, "cut short")
        raise holophyte.loop.gates.InfraFailure("the implementer route did not start")


class CommitThenExit(Commits):
    """A trim turn that commits a pass, then exits unsuccessfully."""

    def play(self, cwd, turn):
        super().play(cwd, turn)
        return ImplementerOutput("gave up", 1, "fake")


class WorkWithUntracked(Commit):
    """The implement turn, leaving an untracked and an ignored file behind."""

    def play(self, cwd, turn):
        (cwd / ".gitignore").write_text("*.log\n")
        reply = super().play(cwd, turn)
        (cwd / "keep.txt").write_text("there before the trim\n")
        (cwd / "carried.log").write_text("carried before the trim\n")
        return reply


class UnrelatedCommit:
    """Points the branch at a commit that shares no history with it."""

    def play(self, cwd, turn):
        def git(*args):
            return subprocess.run(["git", *args], cwd=cwd, check=True,
                                  capture_output=True, text=True).stdout.strip()
        orphan = git("commit-tree", git("write-tree"), "-m", "refactor helpers")
        git("reset", "-q", "--hard", orphan)


class Remove(Commit):
    def play(self, cwd, turn):
        subprocess.run(["git", "rm", "-q", self.path], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-q", "-m", self.message], cwd=cwd,
                       check=True)
        return f"removed {self.path}"


class ObservedApproval:
    """The review turn, noting the worktree it was handed."""

    role = REVIEW_ROLES

    def __init__(self):
        self.status = None

    def play(self, cwd, turn):
        self.status = set(subprocess.run(
            ["git", "status", "--porcelain", "--ignored"], cwd=cwd,
            capture_output=True, text=True, check=True).stdout.splitlines())
        return APPROVE.text


class TrimLoopTests(LoopFixture):
    def trimmed(self, *script, config=TRIMMED, task=None):
        self.configure(config)
        fake, _ = self.loop(*script, provider=StubProvider(task or a_task()))
        return fake

    def shas(self, rev="main"):
        return {subject: sha for sha, subject in (
            line.split(" ", 1) for line in
            self.git("log", rev, "--format=%H %s").splitlines())}

    def results(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'trim_result'"
            " ORDER BY seq")]

    def trim_summaries(self):
        return [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'trim' ORDER BY seq")]

    def assert_merged(self):
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def review_candidates(self, fake):
        return [turn.candidate_sha for turn in fake.turns if turn.role == "review"]

    def test_kept_passes_are_the_first_reviews_candidate_and_trim_runs_once(self):
        fake = self.trimmed(
            WORK,
            Commits(Commit("trim: delete", "work.txt", lines(40)),
                    Commit("trim: comments", "work.txt", lines(30))),
            REQUEST_CHANGES, Commit("fix", "fix.txt"), APPROVE)
        self.assert_merged()
        shas = self.shas()
        self.assertEqual(self.review_candidates(fake)[0], shas["trim: comments"])
        self.assertTrue(fake.turns[1].goal.startswith("Trim the change"))
        self.assertEqual(sum(t.goal.startswith("Trim the change")
                             for t in fake.turns), 1)
        [result] = self.results()
        self.assertEqual(result["outcome"], "kept")
        self.assertEqual([p["pass"] for p in result["kept"]],
                         ["delete", "comments"])
        self.assertEqual((result["lines_before"], result["lines_after"]), (60, 30))
        self.assertTrue(self.trim_summaries()[0].startswith("trim kept"))

    def test_a_pass_that_turns_verify_red_is_dropped_and_its_elders_kept(self):
        task = dict(a_task(), verify="grep -qvx red work.txt")
        fake = self.trimmed(
            WORK,
            Commits(Commit("trim: delete", "work.txt", lines(40)),
                    Commit("trim: tests", "work.txt", "red\n")),
            APPROVE, task=task)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: delete"]])
        self.assertNotIn("trim: tests", self.shas())
        [result] = self.results()
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual([p["pass"] for p in result["reverted"]], ["tests"])
        self.assertIn("verify was red at trim: tests", result["reason"])
        [summary] = self.trim_summaries()
        self.assertIn("reverted tests", summary)

    def on_main(self, path, body):
        (self.target / path).write_text(body)
        self.git("add", path)
        self.git("commit", "-q", "-m", f"add {path}")

    def test_a_pass_that_edits_a_module_outside_the_runs_diff_is_reverted(self):
        self.on_main("helpers.py", "def helper():\n    return 1\n")
        fake = self.trimmed(
            WORK,
            Commits(Commit("trim: delete", "work.txt", lines(40)),
                    Commit("trim: merge", "helpers.py",
                           "def helper():\n    return 2\n")),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: delete"]])
        self.assertNotIn("trim: merge", self.shas())
        [summary] = self.trim_summaries()
        self.assertTrue(summary.startswith("trim partial; kept delete; reverted merge"))
        self.assertIn("trim: merge changed helpers.py outside the run's diff", summary)

    def test_a_pass_that_only_swaps_an_import_outside_the_runs_diff_is_kept(self):
        self.on_main("helpers.py", "from a import x\n\n\ndef helper():\n"
                                   "    return x\n")
        fake = self.trimmed(
            WORK,
            Commits(Commit("trim: merge", "helpers.py",
                           "from b import x\n\n\ndef helper():\n"
                           "    return x\n")),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: merge"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "kept")

    def assert_failed_turn_reverted(self, failure, named):
        review = ObservedApproval()
        fake = self.trimmed(
            WorkWithUntracked("work", "work.txt", lines(60)),
            LeaveDirtyThen(failure, Commit("trim: delete", "work.txt", lines(40))),
            review)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work"]])
        self.assertEqual(review.status, {"?? keep.txt", "!! carried.log"})
        [result] = self.results()
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn(named, result["reason"])

    def test_a_timed_out_trim_is_undone_and_the_run_goes_on(self):
        self.assert_failed_turn_reverted("timeout", "timed out")

    def test_a_route_failure_is_undone_and_the_run_goes_on(self):
        self.assert_failed_turn_reverted("infra", "the route failed")

    def test_a_turn_that_exits_unsuccessfully_is_undone(self):
        fake = self.trimmed(
            WORK, CommitThenExit(Commit("trim: delete", "work.txt", lines(40))),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn("exited with status 1", result["reason"])

    def assert_malformed_turn_reverted(self, second, named):
        fake = self.trimmed(
            WORK, Commits(Commit("trim: delete", "work.txt", lines(40)), second),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work"]])
        self.assertNotIn("trim: delete", self.shas())
        [result] = self.results()
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn(named, result["reason"])

    def test_a_subject_that_is_not_a_pass_reverts_every_pass(self):
        self.assert_malformed_turn_reverted(
            Commit("refactor helpers", "work.txt", lines(30)),
            "'refactor helpers' is not a trim pass")

    def test_a_head_with_no_shared_history_reverts_every_pass(self):
        self.assert_malformed_turn_reverted(UnrelatedCommit(), "rewrote history")

    def test_a_pass_made_twice_reverts_every_pass(self):
        self.assert_malformed_turn_reverted(
            Commit("trim: delete", "work.txt", lines(30)), "'delete' appears twice")

    def test_a_turn_that_commits_nothing_leaves_the_head(self):
        fake = self.trimmed(WORK, Idle("nothing worth trimming"), APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "nothing")
        self.assertEqual(result["reply"], "nothing worth trimming")

    def assert_skipped(self, fake, named):
        self.assert_merged()
        self.assertFalse(any(t.goal.startswith("Trim the change")
                             for t in fake.turns))
        [result] = self.results()
        self.assertEqual(result["outcome"], "skipped")
        self.assertIn(named, result["reason"])
        self.assertEqual(self.read(
            "SELECT count(*) FROM runEvents WHERE kind = 'run_cap'"), [(0,)])

    def test_a_small_diff_is_not_trimmed(self):
        fake = self.trimmed(Commit("work", "work.txt", lines(49)), APPROVE)
        self.assert_skipped(fake, "49 changed lines is under 50")

    def test_red_verify_at_the_implementers_head_is_not_trimmed(self):
        task = dict(a_task(), verify="test ! -e red.txt")
        fake = self.trimmed(
            Commits(WORK, Commit("red", "red.txt", "breaks verify\n")),
            REQUEST_CHANGES, Remove("fix", "red.txt"), APPROVE, task=task)
        self.assert_skipped(fake, "verify is red")

    def test_a_turn_the_run_cap_would_refuse_is_not_trimmed(self):
        fake = self.trimmed(WORK, APPROVE, config="[trim]\nbudget_min = 16\n")
        self.assert_skipped(fake, "run cap would refuse a 16 min turn")

    def test_disabled_trim_runs_no_turn_and_records_nothing(self):
        fake = self.trimmed(WORK, APPROVE, config="[trim]\nenabled = false\n")
        self.assert_merged()
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(self.read(
            "SELECT count(*) FROM runEvents WHERE kind IN ('trim', 'trim_result')"),
            [(0,)])


class TrimStepTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "main"],
                     ["config", "user.name", "Test"],
                     ["config", "user.email", "test@example.invalid"],
                     ["commit", "--allow-empty", "-qm", "base"],
                     ["checkout", "-qb", "task"]):
            self.git(*args)
        (self.repo / "work.txt").write_text(lines(60))
        self.git("add", "work.txt")
        self.git("commit", "-qm", "work")
        holo = root / "holo"
        holo.mkdir()
        (holo / "config.toml").write_text(CONFIG + TRIMMED)
        self.target = holophyte.config.project.Project(
            path=self.repo, holo_dir=holo, store_path=holo / "store.db",
            config_path=holo / "config.toml", worktrees=root / "repo.worktrees")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
        fake.chmod(0o755)
        env = patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_HARNESS_CALLS": str(root / "calls.jsonl")})
        env.start()
        self.addCleanup(env.stop)
        self.conn = store.open(self.target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "test", self.repo)
        ticket = store.mirror_ticket(self.conn, project, "KO-1", "KO-1", "trim",
                                     acceptance_criteria=["trimmed"],
                                     verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def payloads(self, kind):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = ? ORDER BY seq", (kind,))]

    def test_the_trim_turn_records_no_session_and_names_its_role(self):
        agent(self.target, "implement", "implement the thing", self.repo,
              timeout=60, conn=self.conn, run_id=self.run)
        [implemented] = self.payloads("agent_session")
        head = self.git("rev-parse", "HEAD")
        trim(self.target, self.conn, self.run, 60, self.repo,
             self.git("rev-parse", "main"), head, "true", [])
        self.assertEqual(self.payloads("agent_session"), [implemented])
        self.assertEqual(implemented["role"], "implement")
        self.assertEqual([p["role"] for p in self.payloads("agent_turn")],
                         ["implement", "trim"])
        [result] = self.payloads("trim_result")
        self.assertEqual(result["outcome"], "nothing")

    def test_an_unresolved_merge_is_left_for_the_conflict_guard(self):
        self.git("checkout", "-q", "main")
        (self.repo / "work.txt").write_text("main's own work\n")
        self.git("add", "work.txt")
        self.git("commit", "-qm", "main moved on")
        self.git("checkout", "-q", "task")
        head = self.git("rev-parse", "HEAD")
        subprocess.run(["git", "merge", "-q", "main"], cwd=self.repo,
                       capture_output=True)
        trim(self.target, self.conn, self.run, 60, self.repo,
             self.git("rev-parse", "main"), head, "true", [])
        self.assertEqual(self.git("diff", "--name-only", "--diff-filter=U"),
                         "work.txt")
        self.assertEqual(self.payloads("agent_turn"), [])
        [result] = self.payloads("trim_result")
        self.assertEqual((result["outcome"], result["reason"]),
                         ("skipped", "the worktree is mid-merge with main"))


if __name__ == "__main__":
    unittest.main()

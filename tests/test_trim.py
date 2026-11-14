"""The trim step between the implement turn and the first review round.

Run: python3 -m unittest discover -s tests -p 'test_trim.py' -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
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
    FakeAgent,
    Idle,
    Reply,
    block_until_killed,
)
from loop_fixture import VALID_BODY, LoopFixture, StubProvider, a_task  # noqa: E402

import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.pipeline  # noqa: E402
import store  # noqa: E402 - after the sys.path insert above
import store.board  # noqa: E402
import store.criteria  # noqa: E402
import store.steer_notes  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.agents.agent_output import ImplementerOutput  # noqa: E402
from holophyte.agents.roles import agent  # noqa: E402
from holophyte.loop.retire import SUBJECT, retire  # noqa: E402
from holophyte.loop.test_removal import removed_outside_scope  # noqa: E402
from holophyte.loop.trim import trim  # noqa: E402
from tests.test_harness import CONFIG, FAKE_CLAUDE  # noqa: E402
from tests.test_ticket_criteria_rows import COMMANDS, ticket_body  # noqa: E402


def lines(count):
    return "".join(f"line {n}\n" for n in range(count))


TRIMMED = "[trim]\nbudget_min = 1\n"
WORK = Commit("work", path="work.txt", body=lines(60))
PAIR = ("def test_a():\n    assert {a}\n\n\ndef test_b():\n    value = {b}\n"
        "    assert value\n")
SHORTENED_PAIR = "def test_a():\n    assert 3\n\n\ndef test_b():\n    pass\n"
JS_PAIR = ('test("test_a", () => expect({a}));\n\ntest(\n  "test_b",\n'
           '  () => expect({b}),\n);\n')
REGEX_PAIR = ('test("test_a", () => expect({a})); const quote = /"/; '
              'test("test_b", () => expect({b}));\n')
SPACED_PAIR = ('test ("test_a", () => expect({a}));\n'
               'test ("test_b", () => expect({b}));\n')
CONDITION_PAIR = ('test("test_a", () => {{ if (true) /"/.test("text"); '
                  'expect({a}); }}); test("test_b", () => expect({b}));\n')
GO_PAIR = ("package pair\n\nfunc TestFooBar(t *testing.T) {{ _ = {a} }}\n\n"
           "func TestFoo(t *testing.T) {{ _ = {b} }}\n")
ASYNC_PAIR = ("async def test_a(): assert {a}\n\n\n"
              "async def test_b(): assert {b}\n")
DOC_PAIR = PAIR.replace("def test_b():\n", 'def test_b():\n    """Not a test:\n\n'
                        '    def test_example():\n    """\n')
JS_BODY_SPEC = 'test("checks", () => {{ {a}expect({b}); }});\n'
NAMES_PAIR = "Given a pair, then both hold. (`test_pair.py` witnesses it.)"


class Commits:
    """One turn that plays several scripted commits in order."""

    role = "implement"

    def __init__(self, *commits):
        self.commits = commits

    def play(self, cwd, turn):
        for commit in self.commits:
            commit.play(cwd, turn)
        return "trimmed"


class BoardEdit:
    """A board edit that replaces the ticket's criteria mid-turn."""

    def __init__(self, db, criteria):
        self.db, self.criteria = db, criteria

    def play(self, cwd, turn):
        conn = store.open(self.db)
        try:
            (ticket_id,) = conn.execute("SELECT id FROM tickets").fetchone()
            with conn:
                store.criteria.record_criteria(conn, ticket_id, self.criteria,
                                               [], 0)
        finally:
            conn.close()


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


class CommitThenLeaveEdit(Commits):
    """A trim turn that commits a pass, then leaves an edit uncommitted."""

    def play(self, cwd, turn):
        reply = super().play(cwd, turn)
        (cwd / "work.txt").write_text("an unverified trim edit\n")
        return reply


class CommitThenExit(Commits):
    """A trim turn that commits a pass, then exits unsuccessfully."""

    def play(self, cwd, turn):
        super().play(cwd, turn)
        return ImplementerOutput("gave up", 1, "fake")


class WorkWithIgnored(Commit):
    """The implement turn, leaving an ignored file behind."""

    def play(self, cwd, turn):
        (cwd / ".gitignore").write_text("*.log\n")
        reply = super().play(cwd, turn)
        (cwd / "carried.log").write_text("carried before the trim\n")
        return reply


class CommitInDir(Commit):
    def play(self, cwd, turn):
        (cwd / self.path).parent.mkdir(parents=True, exist_ok=True)
        return super().play(cwd, turn)


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


class MakeExecutable(Commit):
    def play(self, cwd, turn):
        (cwd / self.path).chmod(0o755)
        return super().play(cwd, turn)


class MoveGitlink:
    """A trim pass that repoints a submodule's gitlink and commits it."""

    role = "implement"

    def __init__(self, path, sha):
        self.path, self.sha = path, sha

    def play(self, cwd, turn):
        subprocess.run(["git", "update-index", "--cacheinfo",
                        f"160000,{self.sha},{self.path}"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "trim: merge"], cwd=cwd,
                       check=True)
        return "trimmed"


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


def in_scope(path):
    return VALID_BODY.replace("* The thing.", f"* `{path}`")


class TrimFixture(LoopFixture):
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

    def on_main(self, path, body):
        (self.target / path).parent.mkdir(parents=True, exist_ok=True)
        (self.target / path).write_text(body)
        self.git("add", "--", path)
        self.git("commit", "-q", "-m", f"add {path}")


class TrimLoopTests(TrimFixture):
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

    def test_an_import_swap_that_also_changes_the_files_mode_is_reverted(self):
        self.on_main("helpers.py", "from a import x\n")
        fake = self.trimmed(
            WORK, Commits(MakeExecutable("trim: merge", "helpers.py",
                                         "from b import x\n")),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn("helpers.py outside the run's diff", result["reason"])

    def test_a_textconv_driver_does_not_hide_an_outside_edit(self):
        self.git("config", "diff.imports.textconv", "grep -E '^(import|from) '")
        self.on_main(".gitattributes", "helpers.py diff=imports\n")
        self.on_main("helpers.py", "from a import x\n\n\ndef helper():\n"
                                   "    return 1\n")
        fake = self.trimmed(
            WORK, Commits(Commit("trim: merge", "helpers.py",
                                 "from b import x\n\n\ndef helper():\n"
                                 "    return 2\n")),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work"]])
        [result] = self.results()
        self.assertIn("helpers.py outside the run's diff", result["reason"])

    def test_an_ignored_submodule_does_not_hide_an_outside_gitlink_move(self):
        self.git("config", "diff.ignoreSubmodules", "all")
        self.git("update-index", "--add", "--cacheinfo",
                 f"160000,{'1' * 40},vendor")
        self.git("commit", "-q", "-m", "add vendor")
        fake = self.trimmed(WORK, MoveGitlink("vendor", "2" * 40), APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work"]])
        [result] = self.results()
        self.assertIn("vendor outside the run's diff", result["reason"])

    def trimmed_pair(self, criterion, trimmed_pair, path="test_pair.py",
                     pair=PAIR, **extra):
        self.on_main(path, pair.format(a="1", b="2"))
        task = dict(a_task(), criteria=[criterion], **extra)
        fake = self.trimmed(
            Commits(WORK, Commit("work tests", path, pair.format(a="3", b="4"))),
            Commits(Commit("trim: tests", path, trimmed_pair)),
            APPROVE, task=task)
        self.assert_merged()
        return fake

    def test_a_pass_deleting_a_test_from_a_criterion_named_file_is_reverted(self):
        fake = self.trimmed_pair(NAMES_PAIR, "def test_a():\n    assert 3\n")
        self.assertEqual(self.review_candidates(fake), [self.shas()["work tests"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn("criterion witness", result["reason"])
        self.assertIn("def test_b(", self.git("show", "main:test_pair.py"))

    def assert_witness_reverted(self, path, pair, trimmed_pair):
        fake = self.trimmed_pair(f"Given a pair, then both hold. (`{path}` "
                                 "witnesses it.)", trimmed_pair, path, pair)
        self.assertEqual(self.review_candidates(fake), [self.shas()["work tests"]])
        [result] = self.results()
        self.assertIn("criterion witness", result["reason"])

    def test_deleting_a_multiline_js_test_from_a_named_file_is_reverted(self):
        self.assert_witness_reverted(
            "pair.test.js", JS_PAIR, 'test("test_a", () => expect(3));\n')

    def test_deleting_a_js_test_past_a_quote_in_a_regex_literal_is_reverted(self):
        self.assert_witness_reverted(
            "pair.test.js", REGEX_PAIR,
            'test("test_a", () => expect(3)); const quote = /"/;\n')

    def test_deleting_a_js_test_called_with_a_space_before_its_paren_is_reverted(self):
        self.assert_witness_reverted(
            "pair.test.js", SPACED_PAIR, 'test ("test_a", () => expect(3));\n')

    def test_deleting_a_js_test_past_a_regex_after_an_if_condition_is_reverted(self):
        self.assert_witness_reverted(
            "pair.test.js", CONDITION_PAIR,
            'test("test_a", () => { if (true) /"/.test("text"); expect(3); });\n')

    def test_deleting_a_test_from_a_named_file_with_glob_characters_is_reverted(self):
        path = "app/[id]/route.test.ts"
        self.on_main(path, JS_PAIR.format(a="1", b="2"))
        self.trimmed(
            Commits(WORK, Commit("work tests", path, JS_PAIR.format(a="3", b="4"))),
            Commits(Commit("trim: tests", path, 'test("test_a", () => expect(3));\n')),
            Idle(), APPROVE, task=dict(a_task(), criteria=[
                f"Given a route, then both hold. (`{path}` witnesses it.)"]))
        [result] = self.results()
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn("criterion witness", result["reason"])
        self.assertIn('"test_b"', self.git("show", f"main:{path}"))

    def test_a_named_path_with_glob_characters_protects_only_itself(self):
        named, sibling = "app/[id]/route.test.ts", "app/i/route.test.ts"
        self.on_main(named, JS_PAIR.format(a="1", b="2"))
        self.on_main(sibling, JS_PAIR.format(a="1", b="2"))
        self.trimmed(
            Commits(WORK, Commit("work tests", sibling, JS_PAIR.format(a="3", b="4"))),
            Commits(Commit("trim: tests", sibling,
                           'test("test_a", () => expect(3));\n')),
            APPROVE, task=dict(a_task(), body=in_scope(sibling), criteria=[
                f"Given a route, then both hold. (`{named}` witnesses it.)"]))
        [result] = self.results()
        self.assertEqual(result["outcome"], "kept")
        self.assertNotIn('"test_b"', self.git("show", f"main:{sibling}"))

    def test_a_deleted_go_test_whose_name_prefixes_a_kept_one_is_reverted(self):
        self.assert_witness_reverted(
            "pair_test.go", GO_PAIR,
            "package pair\n\nfunc TestFooBar(t *testing.T) { _ = 3 }\n")

    def test_shortening_a_one_line_async_witness_test_is_kept(self):
        fake = self.trimmed_pair(
            "Given a pair, then both hold. (`test_async.py` witnesses it.)",
            "async def test_a(): assert 3\n\n\nasync def test_b(): pass\n",
            "test_async.py", ASYNC_PAIR)
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: tests"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "kept")

    def test_a_claimed_criterion_keeps_its_witness_after_a_board_edit(self):
        self.on_main("test_pair.py", PAIR.format(a="1", b="2"))
        fake = self.trimmed(
            Commits(WORK, Commit("work tests", "test_pair.py",
                                 PAIR.format(a="3", b="4")),
                    BoardEdit(self.db, a_task()["criteria"])),
            Commits(Commit("trim: tests", "test_pair.py",
                           "def test_a():\n    assert 3\n")),
            APPROVE, task=dict(a_task(), criteria=[NAMES_PAIR]))
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work tests"]])
        [result] = self.results()
        self.assertIn("criterion witness", result["reason"])

    def test_dropping_a_js_example_from_an_added_doc_is_kept(self):
        fake = self.trimmed(
            Commits(WORK, Commit("work docs", "usage.md",
                                 'Run:\n\n    test("sample", () => {});\n')),
            Commits(Commit("trim: comments", "usage.md", "Run the suite.\n")),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: comments"]])

    def test_deleting_a_test_from_a_module_the_run_added_is_reverted(self):
        fake = self.trimmed(
            Commits(WORK, Commit("work tests", "checks.py",
                                 PAIR.format(a="3", b="4"))),
            Commits(Commit("trim: tests", "checks.py",
                           "def test_a():\n    assert 3\n")),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["work tests"]])
        [result] = self.results()
        self.assertIn("criterion witness", result["reason"])

    def test_dropping_test_like_calls_and_comments_from_a_js_body_is_kept(self):
        fake = self.trimmed_pair(
            "Given a spec, then it holds. (`body.test.js` witnesses it.)",
            JS_BODY_SPEC.format(a="", b="1"), "body.test.js",
            JS_BODY_SPEC.replace("{a}", '/x/.test("abc");\n'
                                      '  // test("example", () => {{}});\n  '))
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: tests"]])

    def test_dropping_a_docstring_naming_a_test_from_a_witness_is_kept(self):
        fake = self.trimmed_pair(
            NAMES_PAIR, PAIR.format(a="3", b="4"), pair=DOC_PAIR)
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: tests"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "kept")

    def test_the_same_deletion_in_a_file_no_criterion_names_is_kept(self):
        fake = self.trimmed_pair(a_task()["criteria"][0],
                                 "def test_a():\n    assert 3\n",
                                 body=in_scope("test_pair.py"))
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: tests"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "kept")

    def test_the_brief_lists_a_criterion_named_file_as_protected(self):
        fake = self.trimmed_pair(NAMES_PAIR, SHORTENED_PAIR)
        self.assertIn("witness the ticket's acceptance criteria", fake.turns[1].goal)
        self.assertIn("- `test_pair.py`", fake.turns[1].goal)

    def test_a_pass_that_only_shortens_a_witness_tests_body_is_kept(self):
        fake = self.trimmed_pair(NAMES_PAIR, SHORTENED_PAIR)
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: tests"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "kept")

    def assert_failed_turn_reverted(self, failure, named):
        review = ObservedApproval()
        fake = self.trimmed(
            WorkWithIgnored("work", "work.txt", lines(60)),
            LeaveDirtyThen(failure, Commit("trim: delete", "work.txt", lines(40))),
            review, task=dict(a_task(), verify="echo verified > keep.txt"))
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

    def test_a_trim_turn_s_swept_leftover_is_dropped_and_its_pass_kept(self):
        fake = self.trimmed(
            WORK, CommitThenLeaveEdit(Commit("trim: delete", "work.txt", lines(40))),
            APPROVE)
        self.assert_merged()
        self.assertEqual(self.review_candidates(fake), [self.shas()["trim: delete"]])
        [result] = self.results()
        self.assertEqual(result["outcome"], "kept")

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

    def test_a_diff_whose_tests_outweigh_its_code_asks_for_the_tests_pass(self):
        fake = self.trimmed(
            Commits(Commit("fix", "fix.ts", lines(16)),
                    Commit("fix tests", "fix.test.ts", lines(157))),
            Idle("nothing worth trimming"), APPROVE)
        goal = fake.turns[1].goal
        self.assertIn("adds 157 test lines against 16 non-test lines", goal)
        self.assertIn("run pass 5 (tests) in earnest", goal)

    def test_a_diff_with_tests_under_three_times_its_code_gets_the_plain_brief(self):
        fake = self.trimmed(
            Commits(Commit("fix", "fix.py", lines(40)),
                    CommitInDir("fix tests", "tests/test_fix.py", lines(60))),
            Idle("nothing worth trimming"), APPROVE)
        goal = fake.turns[1].goal
        self.assertTrue(goal.startswith("Trim the change"))
        self.assertNotIn("in earnest", goal)
        self.assertNotIn("non-test lines against", goal)

    def test_a_test_heavy_diff_under_60_test_lines_gets_the_plain_brief(self):
        fake = self.trimmed(
            Commits(Commit("fix", "fix.py", lines(10)),
                    CommitInDir("fix tests", "tests/test_fix.py", lines(50))),
            Idle("nothing worth trimming"), APPROVE)
        self.assertNotIn("in earnest", fake.turns[1].goal)

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


def unittest_module(cls, *names, helper=False):
    head = "import unittest\n\n\n" + ("def value():\n    return 1\n\n\n"
                                       if helper else "")
    body = "".join(f"    def {name}(self):\n        self.assertTrue("
                   f"{'value()' if helper else 'True'})\n\n" for name in names)
    return f"{head}class {cls}(unittest.TestCase):\n{body}"


class Files:
    """One turn that writes, or with a None body removes, files in one commit."""

    role = "implement"

    def __init__(self, message, files):
        self.message, self.files = message, files

    def play(self, cwd, turn):
        for path, body in self.files.items():
            path = cwd / path
            if body is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        subprocess.run(["git", "add", "--", *self.files], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-q", "-m", self.message], cwd=cwd,
                       check=True)
        return "committed"


RETIRE_VERIFY = "python3 -m unittest discover -s tests -p 'test_*.py'"
WITNESS = "Given the thing, then it lands. (`tests/test_new.py` witnesses it.)"
OLD = unittest_module("OldTests", "test_one", "test_two")
NEW = ("import unittest\nfrom pathlib import Path\n\n\n"
       "class NewTests(unittest.TestCase):\n    def test_it(self):\n"
       "        self.assertTrue(Path('thing.txt').exists())\n")
ADOPTED_V1 = "python3 -m unittest discover -s tests -p 'test_a.py'"
ADOPTED_V2 = "python3 -m unittest discover -s tests -p 'test_b.py'"
COPY = unittest_module("CopyTests", "test_kept", "test_dropped")
RETIRE_BODY = ("RETIRED tests/test_old.py::test_three: pins a one-off label\n"
               "RETIRED tests/test_copy.py::test_dropped: protects nothing")


class NotReproduced(Commit):
    """An implement turn that commits a test and declares the report not
    reproduced."""

    def play(self, cwd, turn):
        return f"{super().play(cwd, turn)}\nOUTCOME: NOT_REPRODUCED"


def approving(*cited):
    return Reply("Reviewed the diff; no blockers.\nCRITERION 1: met \u2014 "
                 + ", ".join(("tests/test_new.py::NewTests::test_it", *cited))
                 + "\nVERDICT: APPROVE")


class RetireLoopTests(TrimFixture):
    def retired(self, *turns, config=TRIMMED, cited=(), work=None,
                body=VALID_BODY):
        self.on_main("tests/test_old.py", OLD)
        self.base = self.git("rev-parse", "main").strip()
        self.task = dict(a_task(), verify=RETIRE_VERIFY, criteria=[WITNESS],
                         body=body)
        self.gated, real = [], holophyte.loop.pipeline._merge_gate

        def merge_gate(*args, **kwargs):
            self.gated.append(args[9])
            return real(*args, **kwargs)

        with patch.object(holophyte.loop.pipeline, "_merge_gate", merge_gate):
            fake = self.trimmed(
                *(work or (Files("work", {
                    "thing.txt": "the thing\n", "tests/test_new.py": NEW,
                    "tests/test_old.py": unittest_module(
                        "OldTests", "test_one", "test_two", "test_three"),
                    "tests/test_copy.py": COPY}),)),
                approving(*cited), *turns, config=config, task=self.task)
        self.assert_merged()
        [self.approved] = self.review_candidates(fake)
        return fake

    def retire_results(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'retire_result'"
            " ORDER BY seq")]

    def assert_reverted(self, named):
        self.assertEqual(self.gated, [self.approved])
        self.assertNotIn(SUBJECT, self.shas())
        [result] = self.retire_results()
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn(named, result["reason"])

    def test_a_sound_retire_commit_is_what_the_merge_gate_lands(self):
        fake = self.retired(Files(f"{SUBJECT}\n\n{RETIRE_BODY}", {
            "tests/test_old.py": OLD,
            "tests/test_copy.py": unittest_module("CopyTests", "test_kept")}))
        retire_sha = self.shas()[SUBJECT]
        self.assertEqual(self.gated, [retire_sha])
        self.assertEqual(self.git("rev-parse", f"{retire_sha}^").strip(), self.approved)
        [result] = self.retire_results()
        self.assertEqual(result["outcome"], "kept")
        self.assertEqual(result["retired"], [
            {"test": "tests/test_copy.py::test_dropped",
             "reason": "protects nothing"},
            {"test": "tests/test_old.py::test_three",
             "reason": "pins a one-off label"}])
        self.assertEqual(removed_outside_scope(
            self.target, self.base, retire_sha,
            f"{self.task['title']}\n\n{VALID_BODY}"), [])
        self.assertEqual(fake.roles.count("review"), 1)
        self.assertEqual(self.read("SELECT count(*) FROM reviewRounds"), [(1,)])
        [(summary,)] = self.read(
            "SELECT summary FROM runEvents WHERE kind = 'retire'")
        self.assertNotIn("test_dropped", summary)

    def test_a_retire_commit_with_no_retired_reason_lines_is_reverted(self):
        self.retired(Files(SUBJECT, {
            "tests/test_old.py": OLD,
            "tests/test_copy.py": unittest_module("CopyTests", "test_kept")}))
        self.assert_reverted("no RETIRED line for: tests/test_copy.py::"
                             "test_dropped, tests/test_old.py::test_three")

    def test_a_retired_js_title_holding_a_colon_keeps_its_reason(self):
        labels = ('test("parse: old label", () => {});\n'
                  'test("parse: new label", () => {});\n')
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/labels.test.js::parse: old label:"
                  " pins a one-off label",
                  {"tests/labels.test.js": labels.splitlines(True)[1]}),
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/labels.test.js": labels}),))
        [result] = self.retire_results()
        self.assertEqual((result["outcome"], result["retired"]), ("kept", [
            {"test": "tests/labels.test.js::parse: old label",
             "reason": "pins a one-off label"}]))

    def test_a_not_reproduced_run_records_the_skip(self):
        self.configure(TRIMMED)
        self.loop(NotReproduced("test the thing"),
                  Reply("The test never runs the thing.\nVERDICT: FAIL"),
                  Commit("fix the thing"), APPROVE)
        self.assert_merged()
        [result] = self.retire_results()
        self.assertEqual((result["outcome"], result["reason"]),
                         ("skipped", "the run took the not-reproduced path"))

    def test_a_reproduction_test_the_run_renamed_stays_protected(self):
        regress = unittest_module("RegressTests", "test_bug").replace(
            "self.assertTrue(True)", "self.assertTrue(Path('thing.txt').exists())"
        ).replace("import unittest\n", "import unittest\nfrom pathlib import Path\n")
        self.retired(
            Idle("every candidate passes the gate"),
            body=VALID_BODY.replace("## In scope", "## Reproduce\n\n"
                                    "Run the thing.\n\n## In scope"),
            work=(Files("reproduce", {"tests/test_regress.py": regress}),
                  Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/test_regress.py": None,
                                 "tests/test_bug.py": regress,
                                 "tests/test_copy.py": COPY})))
        [result] = self.retire_results()
        self.assertIn({"test": "tests/test_bug.py::test_bug",
                       "why": "held at the reproduction commit"},
                      result["protected"])
        self.assertNotIn("tests/test_bug.py::test_bug", result["candidates"])

    def test_a_retire_commit_deleting_a_criterion_witness_is_reverted(self):
        self.retired(Files(
            f"{SUBJECT}\n\nRETIRED tests/test_old.py::test_three: pins a label\n"
            "RETIRED tests/test_new.py::test_it: pins a path",
            {"tests/test_old.py": OLD, "tests/test_new.py": "import unittest\n"}))
        self.assert_reverted("not candidates: tests/test_new.py::test_it")

    def test_a_retire_commit_cutting_a_protected_tests_assertion_is_reverted(self):
        self.cut_from_test_kept("        self.assertEqual(1, 1)\n")

    def test_a_retire_commit_cutting_an_assigned_assertion_is_reverted(self):
        self.cut_from_test_kept("        unused = self.assertEqual(1, 1)\n")

    def cut_from_test_kept(self, line):
        self.cut_from_copy(COPY.replace("(True)\n\n    def test_dropped",
                                        f"(True)\n{line}\n    def test_dropped"),
                           unittest_module("CopyTests", "test_kept"), "::test_kept")

    def cut_from_copy(self, approved, retired, named, files=()):
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/test_copy.py::test_dropped: "
                  "protects nothing", {"tests/test_copy.py": retired}),
            cited=["tests/test_copy.py::CopyTests::test_kept"],
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/test_copy.py": approved, **dict(files)}),))
        self.assert_reverted("deleted lines outside retired tests and unused imports: "
                             f"tests/test_copy.py{named}")

    def test_a_retire_cutting_a_hash_line_from_a_kept_docstring_is_reverted(self):
        def documented(source, marker):
            return source.replace("test_kept(self):\n", "test_kept(self):\n"
                                  f'        """Checks:\n{marker}        """\n')

        self.cut_from_copy(documented(COPY, "# the marker\n"), documented(
            unittest_module("CopyTests", "test_kept"), ""), "::test_kept")

    def test_a_retire_commit_cutting_a_load_tests_adding_a_check_is_reverted(self):
        hook = ("def load_tests(loader, tests, pattern):\n"
                "    tests.addTest(unittest.FunctionTestCase(lambda: None))\n"
                "    return tests\n\n\n")
        hooked = unittest_module("CopyTests", "test_dropped").replace(
            "class CopyTests", hook + "class CopyTests")
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/test_copy.py::test_dropped: "
                  "protects nothing", {"tests/test_copy.py": "import unittest\n"}),
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/test_copy.py": hooked}),))
        self.assert_reverted("deleted lines outside retired tests and unused imports: "
                             "tests/test_copy.py::CopyTests, "
                             "tests/test_copy.py::load_tests")

    def test_a_retire_commit_cutting_an_import_that_supplies_tests_is_reverted(self):
        shared = COPY.replace("import unittest\n", "import unittest\n"
                              "from checks import SharedChecks\n").replace(
            "test_dropped(self):\n        self.assertTrue(True)",
            "test_dropped(self):\n        self.assertTrue(SharedChecks)")
        self.cut_from_copy(shared, unittest_module("CopyTests", "test_kept"),
                           ":2", files={"tests/checks.py": unittest_module(
                               "SharedChecks", "test_shared")})

    def test_a_retire_commit_cutting_an_imported_module_hook_is_reverted(self):
        hooked = COPY.replace("import unittest\n", "import unittest\n"
                              "from hooks import setUpModule\n")
        self.cut_from_copy(hooked, unittest_module("CopyTests", "test_kept"),
                           ":2", files={"tests/hooks.py": "def setUpModule():\n"
                                        "    pass\n"})

    def test_a_retire_commit_cutting_an_import_sharing_a_line_is_reverted(self):
        shared = COPY.replace("import unittest\n", "import unittest\n"
                              "import os; os.environ.setdefault('KEPT', '1')\n")
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/test_copy.py::test_dropped: "
                  "protects nothing", {"tests/test_copy.py": unittest_module(
                      "CopyTests", "test_kept")}),
            cited=["tests/test_copy.py::CopyTests::test_kept"],
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/test_copy.py": shared}),))
        self.assert_reverted("deleted a line holding more than one statement: "
                             "tests/test_copy.py:2")

    def test_a_retire_commit_cutting_an_assigned_load_tests_is_reverted(self):
        hooked = COPY.replace("import unittest\n", "import unittest\n"
                              "load_tests = lambda loader, tests, pattern: (\n"
                              "    tests.addTest(unittest.FunctionTestCase("
                              "lambda: None)), tests)[1]\n")
        self.cut_from_copy(hooked, unittest_module("CopyTests", "test_kept"),
                           ":2")

    def test_a_retire_commit_cutting_a_kept_tests_framework_attribute_is_reverted(self):
        def unskipped(source):
            return source.replace(
                "class CopyTests(unittest.TestCase):\n",
                '@unittest.skip("base")\nclass Base(unittest.TestCase):\n'
                "    pass\n\n\nclass CopyTests(Base):\n"
                "    __unittest_skip__ = False\n\n")

        self.cut_from_copy(unskipped(COPY), unskipped(unittest_module(
            "CopyTests", "test_kept")).replace("    __unittest_skip__ = False\n\n", ""),
            "::CopyTests")

    def test_a_retire_commit_cutting_a_hook_kept_tests_use_is_reverted(self):
        hooked = COPY.replace("    def test_kept", "    def tearDown(self):\n"
                              "        self.assertTrue(True)\n\n    def test_kept")
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/test_copy.py::test_dropped: "
                  "protects nothing", {"tests/test_copy.py": unittest_module(
                      "CopyTests", "test_kept")}),
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/test_copy.py": hooked}),))
        self.assert_reverted("deleted lines outside retired tests and unused imports: "
                             "tests/test_copy.py::tearDown")

    def test_a_retire_commit_cutting_a_candidate_with_its_tear_down_is_reverted(self):
        def documented(*names):
            return unittest_module("CopyTests", *names).replace(
                "(unittest.TestCase):\n", '(unittest.TestCase):\n    """Shared."""\n\n')

        hooked = documented("test_dropped").replace(
            "    def test_dropped", "    def tearDown(self):\n"
            "        self.assertTrue(True)\n\n    def test_dropped")
        heir = ("from test_copy import CopyTests\n\n\nclass HeirTests(CopyTests):\n"
                "    def test_heir(self):\n        self.assertTrue(True)\n")
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/test_copy.py::test_dropped: "
                  "protects nothing", {"tests/test_copy.py": documented().rstrip()
                                       + "\n"}),
            cited=["tests/test_heir.py::HeirTests::test_heir"],
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/test_heir.py": heir,
                                 "tests/test_copy.py": hooked}),))
        self.assert_reverted("deleted lines outside retired tests and unused "
                             "imports: tests/test_copy.py::tearDown")

    def test_a_retire_commit_deleting_a_class_inheriting_a_base_test_is_reverted(self):
        self.on_main("tests/mixins.py", "class SharedMixin:\n"
                     "    def test_shared(self):\n        self.assertTrue(True)\n")
        kept = unittest_module("CopyTests", "test_kept").replace(
            "import unittest\n", "import unittest\nfrom mixins import SharedMixin\n")
        dropped = ("\nclass Dropped(SharedMixin, unittest.TestCase):\n"
                   "    def test_dropped(self):\n        self.assertTrue(True)\n")
        self.cut_from_copy(kept + dropped, kept, "::Dropped")

    def test_a_js_retire_cutting_a_kept_assertion_naming_the_retired_is_reverted(self):
        flags = ('test("dropped", () => { assert.ok(true); });\n'
                 'test("kept", () => {\n  assert.equal(state(), "dropped");\n'
                 '});\n')
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/flags.test.js::dropped: pins a label",
                  {"tests/flags.test.js": 'test("kept", () => {\n});\n'}),
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/flags.test.js": flags}),))
        self.assert_reverted("deleted lines outside retired tests and unused imports: "
                             "tests/flags.test.js:3")

    def test_a_retire_cutting_a_same_named_surviving_tests_assertion_is_reverted(self):
        twins = unittest_module("CopyTests", "test_common") + "\n\n" + (
            unittest_module("TwinTests", "test_common").split("\n\n\n", 1)[1]
            .replace("(True)\n", "(True)\n        self.assertEqual(1, 1)\n"))
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/test_copy.py::test_common: "
                  "protects nothing", {"tests/test_copy.py": twins.replace(
                      "        self.assertEqual(1, 1)\n", "").replace(
                      unittest_module("CopyTests", "test_common").split(
                          "\n\n\n", 1)[1] + "\n\n", "")}),
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/test_copy.py": twins}),))
        self.assert_reverted("deleted lines outside retired tests and unused imports: "
                             "tests/test_copy.py::CopyTests, "
                             "tests/test_copy.py::test_common")

    def test_a_js_retire_cutting_under_a_commented_candidate_title_is_reverted(self):
        flags = ('test("dropped", () => { assert.ok(true); });\n'
                 'test("kept", () => {\n  // test("dropped", () => {\n'
                 '  assert.ok(true);\n});\n')
        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/flags.test.js::dropped: pins a label",
                  {"tests/flags.test.js": 'test("kept", () => {\n'
                   '  // test("dropped", () => {\n});\n'}),
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/flags.test.js": flags}),))
        self.assert_reverted("deleted lines outside retired tests and unused imports: "
                             "tests/flags.test.js:4")

    def test_a_retire_commit_adding_a_statement_after_an_import_is_reverted(self):
        self.retired(Files(
            f"{SUBJECT}\n\nRETIRED tests/test_copy.py::test_dropped: "
            "protects nothing", {"tests/test_copy.py": "import os; "
                                 "os.environ['RETIRED'] = '1'\n"
                                 + unittest_module("CopyTests", "test_kept")}))
        self.assert_reverted("not an import in tests/test_copy.py")

    def test_a_test_the_approving_review_cited_is_protected(self):
        fake = self.retired(
            Idle("every candidate passes the gate"),
            cited=["tests/test_copy.py::CopyTests::test_kept"])
        [result] = self.retire_results()
        kept = "tests/test_copy.py::test_kept"
        self.assertIn(kept, [entry["test"] for entry in result["protected"]])
        self.assertNotIn(kept, result["candidates"])
        self.assertIn("tests/test_copy.py::test_dropped", result["candidates"])
        self.assertIn("tests/test_copy.py::test_dropped", fake.turns[-1].goal)
        self.assertNotIn("test_copy.py::test_kept", fake.turns[-1].goal)

    def test_a_retire_commit_that_turns_verify_red_is_reverted(self):
        def leaning(source):
            return source.replace(
                "def test_kept(self):\n        self.assertTrue(True)",
                "def test_kept(self):\n        self.assertTrue("
                "hasattr(self, 'test_dropped'))")

        self.retired(
            Files(f"{SUBJECT}\n\nRETIRED tests/test_copy.py::test_dropped: "
                  "protects nothing", {"tests/test_copy.py": leaning(
                      unittest_module("CopyTests", "test_kept"))}),
            work=(Files("work", {"thing.txt": "the thing\n",
                                 "tests/test_new.py": NEW,
                                 "tests/test_copy.py": leaning(COPY)}),))
        self.assert_reverted("verify was red at the retire commit")

    def test_a_retire_commit_that_edits_a_factory_module_is_reverted(self):
        self.on_main("holophyte/helper.py", "VALUE = 1\n")
        self.retired(Files(SUBJECT, {
            "tests/test_old.py": OLD,
            "holophyte/helper.py": "VALUE = 1\n\n\ndef unused():\n    pass\n"}))
        self.assert_reverted("holophyte/helper.py, not a test file")

    def test_deleting_a_base_held_test_outside_in_scope_is_reverted(self):
        self.retired(Files(SUBJECT, {"tests/test_old.py": unittest_module(
            "OldTests", "test_two")}))
        self.assert_reverted("tests removed outside the ticket's scope: "
                             "tests/test_old.py")

    def test_only_criterion_file_tests_added_skips_with_no_turn(self):
        fake = self.retired(work=(Files("work", {"thing.txt": "the thing\n",
                                                 "tests/test_new.py": NEW}),))
        self.assertEqual(fake.roles, ["implement", "review"])
        [result] = self.retire_results()
        self.assertEqual((result["outcome"], result["reason"]),
                         ("skipped", "no run-added test outside the protected set"))

    def test_disabled_trim_records_no_retire_and_lands_the_approved_sha(self):
        fake = self.retired(config="[trim]\nenabled = false\n")
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(self.gated, [self.approved])
        self.assertEqual(self.read(
            "SELECT count(*) FROM runEvents WHERE kind IN ('retire',"
            " 'retire_result')"), [(0,)])


def documented_b(*names):
    return unittest_module("BTests", *names).replace(
        "(unittest.TestCase):\n", '(unittest.TestCase):\n    """B."""\n\n')


class RetireStepTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests/test_a.py").write_text(
            unittest_module("ATests", "test_a"))
        for args in (["init", "-q", "-b", "main"],
                     ["config", "user.name", "Test"],
                     ["config", "user.email", "test@example.invalid"],
                     ["add", "tests"], ["commit", "-qm", "base"],
                     ["checkout", "-qb", "task"]):
            self.git(*args)
        (self.repo / "tests/test_b.py").write_text(documented_b("test_b"))
        self.git("add", "tests")
        self.git("commit", "-qm", "work")
        holo = root / "holo"
        holo.mkdir()
        (holo / "config.toml").write_text(CONFIG + TRIMMED)
        self.target = holophyte.config.project.Project(
            path=self.repo, holo_dir=holo, store_path=holo / "store.db",
            config_path=holo / "config.toml", worktrees=root / "repo.worktrees")
        self.conn = store.open(self.target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.tickets.ensure_project(self.conn, "team", str(self.repo))
        key = store.board.file_ticket(self.conn, project, "NAT",
                                      self.body(ADOPTED_V1))
        (self.ticket,) = self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?", (key,)
        ).fetchone()
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, project, self.ticket)
        edited = int(time.time() * 1000) + 1000
        store.board.edit_ticket(self.conn, project, key, self.body(ADOPTED_V2),
                                expected_revision=1, now=edited)
        store.steer_notes.steer(self.conn, self.ticket, "Meet revision 2",
                                "maintainer", now=edited + 1000)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True).stdout.strip()

    def body(self, verify):
        return ticket_body(["the list shows every entry"]).replace(
            COMMANDS, verify)

    def test_a_deletion_the_adopted_verify_fails_is_reverted(self):
        approved = self.git("rev-parse", "HEAD")
        fake = FakeAgent(Files(
            f"{SUBJECT}\n\nRETIRED tests/test_b.py::test_b: pins nothing",
            {"tests/test_b.py": documented_b().rstrip() + "\n"}))
        with patch.object(holophyte.loop.implement, "agent", fake):
            head = retire(self.target, self.conn, self.run, 60, self.repo,
                          self.git("rev-parse", "main"), approved,
                          self.body(ADOPTED_V1), ADOPTED_V1, [],
                          ["the list shows every entry"])
        self.assertEqual((head, self.git("rev-parse", "HEAD")),
                         (approved, approved))
        [(payload,)] = self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'retire_result'")
        result = json.loads(payload)
        self.assertEqual(result["outcome"], "reverted")
        self.assertIn("verify was red at the retire commit", result["reason"])


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

"""`holophyte.babysit.babysitter`: the pass's texts, read and written without GitHub.

The verdict parser is what decides which thread gets fixed, which gets a
decline, and which parks the run for a person; the acceptance tests in
`test_babysit_pass.py` witness the pass end to end, and this holds the
parser's edges: a thread with no line is `HUMAN`, a verdict is read whatever
separator the model reached for, and a number outside the listing is
ignored rather than filed against a thread that does not exist.

Run: python3 -m unittest discover -s tests -p 'test_babysit*' -v
"""
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
# The repo root for `holophyte`, and `tests/` itself for the loop harness
# (`loop_fixture`) and its scripted agent (`fake_agent`).
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import babysit_fixture as cases  # noqa: E402
from fake_agent import Commit, Idle  # noqa: E402
from loop_fixture import (  # noqa: E402
    BRANCH,
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.config.project  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import holophyte.pr.pullrequest  # noqa: E402
import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.babysit import babysitter  # noqa: E402
from holophyte.loop.gates import RunFailure  # noqa: E402
from holophyte.loop.runs import open_store, set_phase  # noqa: E402
from holophyte.pr import github, pr_media, pr_status  # noqa: E402
from holophyte.pr.github import PullRequest, Thread  # noqa: E402

PULL = PullRequest(host="github.com", owner="o", name="r", number=3,
                   url="https://github.com/o/r/pull/3")


def thread(n, body="a thread", author="bot", path="a.py"):
    line = n
    return Thread(id=f"T{n}", path=path, line=line, author=author, body=body,
                  url=f"{PULL.url}#discussion_r{n}")


def run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


class FoldChecksTests(unittest.TestCase):
    """`pr_status.fold_checks()`: the rollup beside the head's check runs and the
    branch's required contexts. Regression: REL-120 was parked "ready to
    merge" 19 seconds after its PR opened, on a rollup that said success
    while vitest, the build and three review bots were still queued."""

    def test_a_run_still_in_progress_is_pending_whatever_the_rollup_says(self):
        runs = [run("lint"), run("vitest", status="in_progress",
                                 conclusion=None)]
        self.assertEqual(pr_status.fold_checks("SUCCESS", runs, []), "pending")

    def test_every_run_completed_without_failure_is_green(self):
        runs = [run("lint"), run("vitest"), run("docs", conclusion="skipped")]
        self.assertEqual(pr_status.fold_checks("SUCCESS", runs, []), "success")

    def test_a_completed_run_that_failed_is_red(self):
        runs = [run("lint"), run("vitest", conclusion="failure")]
        self.assertEqual(pr_status.fold_checks("SUCCESS", runs, []), "failure")

    def test_a_required_context_with_no_run_yet_is_pending(self):
        runs = [run("lint")]
        self.assertEqual(pr_status.fold_checks("SUCCESS", runs, ["vitest"]),
                         "pending")
        self.assertEqual(pr_status.fold_checks("SUCCESS", runs + [run("vitest")],
                                        ["vitest"]), "success")

    def test_no_rules_and_no_runs_is_green_as_the_rollup_alone_said(self):
        self.assertEqual(pr_status.fold_checks(None, [], []), "success")

    def test_a_read_that_did_not_come_back_is_pending_never_green(self):
        self.assertEqual(pr_status.fold_checks("SUCCESS", None, []), "pending")
        self.assertEqual(pr_status.fold_checks("SUCCESS", [run("lint")], None),
                         "pending")
        # Red still wins: the rollup is the cheapest red signal.
        self.assertEqual(pr_status.fold_checks("FAILURE", None, None), "failure")

    def test_check_data_the_babysitter_cannot_read_is_pending_never_green(self):
        # Review finding: a `check_runs` that is not a list, or an entry
        # that is not a run, was skipped and the rest read as green.
        self.assertEqual(pr_status.fold_checks("SUCCESS", "unreadable", []),
                         "pending")
        self.assertEqual(pr_status.fold_checks("SUCCESS", [run("lint"), "garbage"],
                                        []), "pending")
        self.assertEqual(pr_status.fold_checks("SUCCESS", [run("lint"), None], []),
                         "pending")


class AdjudicationBriefTests(unittest.TestCase):
    """A thread naming an existing function the diff re-implements is a
    change request: the brief says so, and quotes the repository's
    conventions when it has a file of them."""
    THREAD = thread(1, "This duplicates `getTxSide` in lib/tx.py; reuse it.")

    def brief(self, wt):
        return babysitter.adjudication_brief(
            PULL, (self.THREAD,), "the ticket", "c" * 40,
            babysitter.conventions(wt))

    def test_the_rule_and_the_conventions_excerpt_with_an_agents_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            (wt / "AGENTS.md").write_text("# Guide\n\nKeep it KISS and DRY.\n")

            text = self.brief(wt)

        self.assertIn("getTxSide", text)
        self.assertIn("which the diff duplicates is a concrete change "
                      "request", text)
        self.assertIn("The repository's AGENTS.md:\n\n# Guide\n\nKeep it KISS "
                      "and DRY.", text)
        self.assertIn("DECLINE is for a thread that asks for nothing "
                      "specific, or asks for what the ticket puts out of "
                      "scope.", text)
        self.assertNotIn("style preference", text)

    def test_the_rule_and_no_excerpt_without_a_conventions_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(babysitter.conventions(Path(tmp)), ())
            text = self.brief(Path(tmp))

        self.assertIn("which the diff duplicates is a concrete change "
                      "request", text)
        self.assertNotIn("The repository's AGENTS.md", text)
        self.assertNotIn("The repository's CLAUDE.md", text)


class VerdictTests(unittest.TestCase):
    def test_a_thread_without_a_verdict_line_is_a_human_question(self):
        reply = ("Looked at both.\n"
                 "- THREAD 1: address — the null check is missing\n"
                 "THREAD 3: DECLINE: out of scope\n"
                 "THREAD 9: ADDRESS -- no such thread\n")

        verdicts = babysitter.parse_verdicts(reply, 3)

        self.assertEqual(verdicts[1], ("ADDRESS", "the null check is missing"))
        self.assertEqual(verdicts[2][0], "HUMAN")
        self.assertEqual(verdicts[3], ("DECLINE", "out of scope"))
        self.assertNotIn(9, verdicts)

    def test_the_round_text_reads_as_a_review_round(self):
        threads = (thread(1, "crash on None"), thread(2, "rename n",
                                                       author="style"))
        verdicts = {1: ("ADDRESS", "real"), 2: ("DECLINE", "taste")}

        text = babysitter.round_reply(PULL, 1, threads, verdicts, "success",
                                    "a" * 40)

        self.assertTrue(text.startswith("Babysit pass 1 over " + PULL.url))
        self.assertTrue(text.endswith("VERDICT: REQUEST_CHANGES"))
        self.assertIn("- a.py:1 @bot: crash on None -- ADDRESS: real", text)
        self.assertEqual(babysitter.route_of(threads), "github:bot+style")
        self.assertTrue(babysitter.round_reply(PULL, 2, (), {}, "success",
                                             "a" * 40)
                        .endswith("VERDICT: APPROVE"))
        self.assertEqual(babysitter.route_of(()), "github:ci")

    def test_replies_open_with_the_model_header(self):
        addressed = babysitter.addressed_reply("codex-sol-medium", "added the"
                                             " check", "b" * 40)
        declined = babysitter.declined_reply("codex-sol-medium", "taste")

        for text in (addressed, declined):
            self.assertTrue(text.startswith(
                "---- Comment by codex-sol-medium ----\n"), text)
        self.assertIn(f"Addressed in {'b' * 40}: added the check", addressed)
        self.assertIn("Declined: taste", declined)
        self.assertEqual(babysitter.parse_summaries(
            "did things\nTHREAD 2: guarded the load\nTHREAD 1: renamed"),
            {2: "guarded the load", 1: "renamed"})


class WrittenPrTextTests(unittest.TestCase):
    """`github.parse_pr_text()`: the `TITLE:` line and the body after it, or
    None for a reply the loop cannot open a PR from; `github.pr_body_written()`
    ends the body with the Linear line (KO-336)."""

    def test_the_title_line_and_the_body_after_it_are_read(self):
        reply = ("TITLE: [Contacts] Put Contact Name first\n\n"
                 "The two forms now \u2026")

        self.assertEqual(github.parse_pr_text(reply),
                         ("[Contacts] Put Contact Name first",
                          "The two forms now \u2026"))

    def test_a_reply_without_a_title_line_is_none(self):
        self.assertIsNone(github.parse_pr_text(
            "Here is the description.\n\nThe two forms now \u2026"))

    def test_an_empty_or_overlong_title_is_none(self):
        self.assertIsNone(github.parse_pr_text("TITLE:\n\nA body."))
        self.assertIsNone(github.parse_pr_text(f"TITLE: {'x' * 121}\n\nA body."))
        self.assertIsNotNone(
            github.parse_pr_text(f"TITLE: {'x' * 120}\n\nA body."))

    def test_the_written_body_ends_with_the_linear_line(self):
        body = github.pr_body_written("What changed.\n", "KO-336",
                                  "https://linear.app/example/issue/KO-336")

        self.assertEqual(body.splitlines()[-1],
                         "Linear: KO-336 (https://linear.app/example/issue/"
                         "KO-336)")
        self.assertTrue(body.startswith("What changed.\n\n"))
        self.assertEqual(github.pr_body_written("Text", "KO-1", None).splitlines()[-1],
                         "Linear: KO-1")


class PullStatusTests(unittest.TestCase):
    """`pr_status.pull_status()` reads the facts `/attention` shows on a parked
    pull request (KO-368) from the same answer the reconcile already
    makes: the head's `statusCheckRollup` and `reviewDecision`."""

    OPEN = {"state": "OPEN", "merged": False, "mergeCommit": None,
            "mergedBy": None, "updatedAt": "2026-09-10T10:00:00Z",
            "reviewThreads": {"totalCount": 2}}

    def read(self, node):
        with patch.object(pr_status, "graphql",
                          return_value={"repository": {"pullRequest": node}}):
            return pr_status.pull_status(None, PULL)

    def test_checks_and_review_are_read_from_the_head_rollup_and_decision(
            self):
        status = self.read(dict(
            self.OPEN, reviewDecision="CHANGES_REQUESTED",
            commits={"nodes": [{"commit": {"statusCheckRollup":
                                            {"state": "FAILURE"}}}]}))

        self.assertEqual((status.checks, status.review, status.threads),
                         ("failure", "changes_requested", 2))

    def test_a_pending_rollup_and_an_approval_read_as_such(self):
        status = self.read(dict(
            self.OPEN, reviewDecision="APPROVED",
            commits={"nodes": [{"commit": {"statusCheckRollup":
                                            {"state": "PENDING"}}}]}))

        self.assertEqual((status.checks, status.review),
                         ("pending", "approved"))

    def test_an_answer_without_them_reads_none_for_both(self):
        """A pull request with no checks has no rollup (`null`), and a
        repository requiring no review has no decision: neither is
        "pending" -- there is nothing to wait for -- so both are None, as
        is an answer that predates the fields."""
        no_rollup = self.read(dict(
            self.OPEN, reviewDecision=None,
            commits={"nodes": [{"commit": {"statusCheckRollup": None}}]}))
        older = self.read(self.OPEN)

        self.assertEqual((no_rollup.checks, no_rollup.review), (None, None))
        self.assertEqual((older.checks, older.review), (None, None))
        self.assertEqual(older.threads, 2)


class AuthorKindTests(unittest.TestCase):
    def test_an_author_is_read_as_bot_user_or_unknown_by_github_type(self):
        page = {"nodes": [
            {"author": {"login": "devin-ai-integration", "__typename": "Bot"},
             "body": "a finding"},
            {"author": {"login": "wevial", "__typename": "User"},
             "body": "a person's word"},
            {"author": None, "body": "a deleted account's word"}]}

        comments = pr_status._comment_nodes(page)

        self.assertEqual([c.author_kind for c in comments],
                         ["bot", "user", "unknown"])
        self.assertEqual([c.author for c in comments],
                         ["devin-ai-integration", "wevial", "unknown"])


class MergeableReadTests(unittest.TestCase):
    """`pr_status.pr_state()` carries GitHub's `mergeable` answer through to the
    babysit pass; a read whose page predates the field, and the `null`
    GitHub answers while it computes mergeability lazily, both read
    UNKNOWN -- never a conflict and never a clearance."""

    def read(self, mergeable="absent"):
        node = {"state": "OPEN", "merged": False, "headRefOid": "h",
                "mergeCommit": None, "reviewThreads": {"nodes": []},
                "commits": {"nodes": []}}
        if mergeable != "absent":
            node["mergeable"] = mergeable
        with patch.object(
                pr_status, "graphql",
                return_value={"repository": {"pullRequest": node}}), \
                patch.object(pr_status, "rest", return_value=[]):
            return pr_status.pr_state(None, PULL)

    def test_the_mergeable_answer_is_carried(self):
        self.assertEqual(self.read("CONFLICTING").mergeable, "CONFLICTING")
        self.assertEqual(self.read("MERGEABLE").mergeable, "MERGEABLE")

    def test_an_absent_or_null_answer_reads_unknown(self):
        self.assertEqual(self.read().mergeable, "UNKNOWN")
        self.assertEqual(self.read(None).mergeable, "UNKNOWN")


class ConflictingPullRequestTests(cases.ConflictingMainHelpers,
                                  MergeModeFixture):
    """A babysit pass over a pull request GitHub reports CONFLICTING
    merges `origin/main` -- the remote's `main`, not the checkout's
    possibly stale local one -- into the branch, pushes, and goes back
    to waiting on checks; nothing is rebased or force-pushed, so review
    threads keep their lines. A merge that stops in the tree is the
    implementer's to resolve, and one left unresolved parks the run
    naming the conflicting paths. MERGEABLE and UNKNOWN answers trigger
    none of it (KO-377)."""

    def test_a_conflicting_pull_request_merges_origin_main_in(self):
        """KO-377: GitHub says CONFLICTING and `origin/main` merges
        cleanly. The pass fetches `origin`, merges the remote's `main`
        into the branch in the resumed worktree -- the merge commit's
        second parent is the remote's `main`, not the checkout's local
        `main`, which the test leaves behind -- pushes it, records the
        merge in the ledger, and goes back to waiting on checks. No
        agent turns and no history rewrite: the candidate stays the
        merge's first parent and the checkout's `main` never moved."""
        approved = self.parked_on_a_nit(
            Commit("the scripted work", path="THING.md",
                   body="the branch's line\n"))
        moved = self.remote_main("MOVED.md", "main moved on\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())

        fake, _ = self.resume()

        wt = self.worktrees / "ko-131-add-a-thing"
        head = self.git("rev-parse", "HEAD", cwd=wt).strip()
        self.assertEqual(fake.roles, [])
        self.assertEqual(self.git("rev-parse", "HEAD^1", cwd=wt).strip(),
                         approved)
        self.assertEqual(self.git("rev-parse", "HEAD^2", cwd=wt).strip(),
                         moved)
        self.assertNotEqual(self.git("rev-parse", "main").strip(), moved)
        self.assertEqual([c for c in self.recorded()
                          if c.startswith("git")],
                         [f"git push origin {BRANCH}"] * 2)
        # What the pushes delivered: the candidate at open, then the
        # merge commit -- resolved at push time, so a push ordered
        # before the merge would record the pre-merge tip here.
        self.assertEqual(self.pushed(),
                         [(BRANCH, approved), (BRANCH, head)])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE kind = 'note' AND"
                      " text LIKE 'Merged main into%'"),
            [(f"Merged main into {BRANCH} at {head} (GitHub reported a"
              " conflict)",)])
        self.assertEqual(
            self.read("SELECT phase, candidateSha FROM runs WHERE id = 2"),
            [("awaiting_merge_approval", head)])
        self.assertIn("waiting for a human to say merge", self.question())

    def merge_verify(self, main_red):
        calls = []

        def verify(command, cwd, *args, **kwargs):
            sha = self.git("rev-parse", "HEAD", cwd=cwd).strip()
            calls.append((command, Path(cwd), sha))
            ok = (Path(cwd) / "FIXED.md").exists() or (
                not main_red and not (Path(cwd) / "THING.md").exists())
            return ok, "ok" if ok else "[verify] FAILED: full command: echo ok"

        return calls, patch("holophyte.loop.gates._run_verify", side_effect=verify)

    def test_red_merged_tree_and_red_main_park_before_review(self):
        self.parked_on_a_nit(Commit("candidate", path="THING.md"))
        moved = self.remote_main("MOVED.md", "main moved\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        calls, verify = self.merge_verify(main_red=True)
        with verify:
            fake, _ = self.resume()
        self.assertEqual(fake.roles, [])
        self.assertIn("main is red at " + moved, self.question())
        self.assertIn("echo ok", self.question())
        results = [result for (raw,) in self.read(
            "SELECT verificationResults FROM reviewRounds WHERE runId = 2")
            for result in json.loads(raw)]
        self.assertEqual(len(results), 2)
        self.assertEqual([r["exitCode"] for r in results], [1, 1])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][2], moved)
        self.assertNotEqual(calls[0][1], calls[1][1])
        self.assertFalse(calls[1][1].exists())
        for result, (_, _, sha) in zip(results, calls):
            self.assertIn(sha, result["output"])

    def test_red_merged_tree_and_green_main_get_one_fix_and_verify(self):
        self.parked_on_a_nit(Commit("candidate", path="THING.md"))
        moved = self.remote_main("MOVED.md", "main moved\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        calls, verify = self.merge_verify(main_red=False)
        with verify:
            fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertEqual(fake.roles, ["implement", "implement"])
        self.assertIn("echo ok", fake.turns[0].goal)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(calls[1][2], moved)
        self.assertEqual(calls[2][1], calls[0][1])
        self.assertNotEqual(calls[2][2], calls[0][2])
        self.assertEqual(self.pushed()[-1][1], calls[2][2])

    def module_verify(self, main_red):
        """A verify that imports what its command names, as unittest does:
        a `tests.name` without `tests/name.py` in the tree fails. The
        candidate's own module makes the merged tree fail until FIXED.md,
        and `main_red` makes main fail with every module present."""
        calls = []

        def verify(command, cwd, *args, **kwargs):
            cwd = Path(cwd)
            calls.append((command, cwd))
            if not command:
                return True, "(no verify command)"
            missing = [n for n in re.findall(r"tests\.(\w+)", command)
                       if not (cwd / "tests" / f"{n}.py").exists()]
            if missing:
                return False, f"No module named 'tests.{missing[0]}'"
            ok = (cwd / "FIXED.md").exists() or not (
                main_red or (cwd / "tests" / "test_branch_only.py").exists())
            return ok, "ok" if ok else "FAILED (failures=1)"

        return calls, patch("holophyte.loop.gates._run_verify", side_effect=verify)

    def module_candidate(self, command):
        """KO-597: a candidate adding `tests/test_branch_only.py`, a main
        moved on by `tests/test_shared.py`, and a ticket verify `command`
        the resumed pass reads."""
        self.parked_on_a_nit(Commit("candidate", path="tests/test_branch_only.py"))
        self.provider = lambda: StubProvider(
            dict(a_task(), body=self.BODY, verify=command))
        moved = self.remote_main("tests/test_shared.py", "main's test\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        return moved

    def test_a_module_only_the_candidate_adds_is_not_run_on_main(self):
        command = "python3 -m unittest tests.test_branch_only tests.test_shared"
        moved = self.module_candidate(command)
        calls, verify = self.module_verify(main_red=False)
        with verify:
            fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertEqual(fake.roles, ["implement", "implement"])
        self.assertNotIn("main is red", fake.turns[0].goal)
        self.assertIn(f"main at {moved} passes", fake.turns[0].goal)
        self.assertEqual(calls[0][0], command)
        self.assertEqual(calls[1][0], "python3 -m unittest tests.test_shared")
        self.assertEqual(calls[2][0], command)
        commands = [row["command"] for (raw,) in self.read(
            "SELECT verificationResults FROM reviewRounds WHERE runId = 2"
            " ORDER BY round") for row in json.loads(raw)]
        self.assertEqual(commands[:2],
                         [command, "python3 -m unittest tests.test_shared"])
        ((event,),) = self.read(
            "SELECT summary FROM runEvents WHERE runId = 2"
            " AND summary LIKE 'main-side verify%'")
        self.assertIn("tests.test_branch_only", event)
        self.assertIn("only on the candidate", event)
        self.assertNotIn("main is red", self.question())
        self.assertIn("the fix rounds moved the candidate", self.question())

    def test_a_red_main_with_every_module_present_still_parks(self):
        command = "python3 -m unittest tests.test_shared"
        moved = self.module_candidate(command)
        calls, verify = self.module_verify(main_red=True)
        with verify:
            fake, _ = self.resume()
        self.assertEqual(fake.roles, [])
        self.assertIn(f"main is red at {moved}; verify command: {command}",
                      self.question())
        self.assertEqual([c for c, _ in calls], [command, command])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE runId = 2"
            " AND summary LIKE 'main-side verify%'"), [(0,)])

    def test_a_clause_left_after_a_dropped_one_still_runs_on_main(self):
        shared = "python3 -m unittest tests.test_shared"
        moved = self.module_candidate(
            "python3 -m unittest tests.test_branch_only && " + shared)
        calls, verify = self.module_verify(main_red=True)
        with verify:
            fake, _ = self.resume()
        self.assertEqual(fake.roles, [])
        self.assertIn(f"main is red at {moved}", self.question())
        self.assertEqual(calls[1][0], shared)

    def carry_candidate(self, command, setup=None, installed=True,
                        carry=("deps",)):
        """KO-643: a candidate adding THING.md, a target carrying the
        ignored `deps` directory, the task worktree holding it when
        `installed`, and a main moved on by MOVED.md. Returns the task
        worktree and the moved main."""
        self.parked_on_a_nit(Commit("candidate", path="THING.md"))
        common = Path(self.git("rev-parse", "--path-format=absolute",
                               "--git-common-dir").strip())
        (common / "info").mkdir(exist_ok=True)
        with open(common / "info" / "exclude", "a") as exclude:
            exclude.write("deps\n")
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       f"[worktree]\ncarry = {json.dumps(list(carry))}\n"
                       + (f"setup = {json.dumps(setup)}\n" if setup else ""))
        self.provider = lambda: StubProvider(
            dict(a_task(), body=self.BODY, verify=command))
        wt = self.worktrees / "ko-131-add-a-thing"
        if installed:
            (wt / "deps").mkdir()
            (wt / "deps" / "package.txt").write_text("installed\n")
        moved = self.remote_main("MOVED.md", "main moved\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        return wt, moved

    def prepared(self):
        return [event for (event,) in self.read(
            "SELECT summary FROM runEvents WHERE runId = 2"
            " AND summary LIKE 'main-side verify%prepared%'")]

    def test_a_carried_directory_makes_a_green_main_baseline(self):
        command = "test -d deps && test -e FIXED.md -o ! -e THING.md"
        wt, moved = self.carry_candidate(command)
        fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertEqual(fake.roles[0], "implement")
        self.assertIn(f"main at {moved} passes", fake.turns[0].goal)
        self.assertIn("carried deps", " ".join(self.prepared()))
        # The link is gone with the checkout; the worktree's copy is not.
        self.assertEqual((wt / "deps" / "package.txt").read_text(), "installed\n")

    def test_a_carry_the_worktree_lacks_runs_setup_on_main_first(self):
        command = "test -d deps && test -e FIXED.md -o ! -e THING.md"
        _, moved = self.carry_candidate(command, setup=["mkdir deps"],
                                        installed=False)
        fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertEqual(fake.roles[0], "implement")
        self.assertIn(f"main at {moved} passes", fake.turns[0].goal)
        (event,) = self.prepared()
        self.assertIn("ran setup for deps: mkdir deps", event)
        self.assertNotIn("FAILED", event)

    def test_setup_replacing_a_carried_link_still_cleans_up(self):
        """Review finding: setup that swaps a carried link for a directory
        must not turn the checkout's cleanup into a crash."""
        command = "test -d deps && test -e FIXED.md -o ! -e THING.md"
        wt, moved = self.carry_candidate(
            command, setup=["rm -rf deps && mkdir -p deps other-deps"],
            carry=("deps", "other-deps"))
        fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertIn(f"main at {moved} passes", fake.turns[0].goal)
        self.assertNotIn("--detach", self.git("worktree", "list", "--porcelain"))
        self.assertEqual((wt / "deps" / "package.txt").read_text(), "installed\n")

    def test_a_true_red_main_with_the_directory_carried_still_parks(self):
        command = "test -d deps && test ! -e MOVED.md"
        _, moved = self.carry_candidate(command)
        fake, _ = self.resume()
        self.assertEqual(fake.roles, [])
        self.assertIn(f"main is red at {moved}; verify command: {command}",
                      self.question())
        self.assertIn("carried deps", " ".join(self.prepared()))

    def test_a_tree_conflict_goes_to_the_implementer_then_parks(self):
        """KO-377: `origin/main` conflicts with the branch in the tree.
        The pass invokes one implementer turn with the conflicting paths
        the way the local gate does (KO-355); a turn that leaves the
        conflict unresolved parks the run, its question naming the paths,
        and the aborted merge leaves the branch at the candidate."""
        approved = self.parked_on_a_nit(
            Commit("the scripted work", path="README.md",
                   body="the branch's line\n"))
        self.remote_main("README.md", "the remote's line\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())

        fake, _ = self.resume(Idle("I cannot reconcile these."))

        self.assertEqual(fake.roles, ["implement"])
        self.assertIn("README.md", fake.turns[0].goal)
        question = self.question()
        self.assertIn("README.md", question)
        self.assertIn("conflict", question)
        wt = self.worktrees / "ko-131-add-a-thing"
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(),
                         approved)
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), approved)
        self.assertEqual(
            self.read("SELECT phase FROM runs WHERE id = 2"),
            [("awaiting_merge_approval",)])
        # The only push ever was the candidate's, at open; the aborted
        # merge pushed nothing.
        self.assertEqual(self.pushed(), [(BRANCH, approved)])

    def test_a_resolved_conflict_survives_a_fetch_during_the_turn(self):
        """KO-765: the turn resolves and commits the merge it was given,
        and before it returns `origin/main` moves, as a concurrent fetch
        by another worker would. The pass judges the turn against the
        `main` it fetched, not the moved ref, so it pushes the merge."""
        approved = self.parked_on_a_nit(
            Commit("the scripted work", path="README.md",
                   body="the branch's line\n"))
        fetched = self.remote_main("README.md", "the remote's line\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        remote_main = self.remote_main

        class ResolveThenFetch(Commit):
            def play(self, cwd, turn):
                said = super().play(cwd, turn)
                remote_main("LATER.md", "main moved again\n")
                return said

        fake, _ = self.resume(ResolveThenFetch(
            "resolve the merge", path="README.md", body="both lines\n"),
            Idle(""))

        self.assertEqual(fake.roles[0], "implement")
        wt = self.worktrees / "ko-131-add-a-thing"
        head = self.git("rev-parse", "HEAD", cwd=wt).strip()
        self.assertEqual(self.git("rev-parse", "HEAD^1", cwd=wt).strip(),
                         approved)
        self.assertEqual(self.git("rev-parse", "HEAD^2", cwd=wt).strip(),
                         fetched)
        self.assertNotIn("left it unresolved", self.question())
        self.assertEqual(self.pushed(), [(BRANCH, approved), (BRANCH, head)])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE kind = 'note' AND"
                      " text LIKE 'Merged main into%'"),
            [(f"Merged main into {BRANCH} at {head} (GitHub reported a"
              " conflict)",)])

    def test_mergeable_and_unknown_pull_requests_are_not_merged(self):
        """KO-377: MERGEABLE is left alone -- a PR that is merely behind
        is not merged into -- and UNKNOWN is treated as not conflicting:
        GitHub computes it lazily and the next pass sees it. Neither
        merges `main` in nor pushes."""
        approved = self.parked_on_a_nit(Commit("the scripted work"))
        self.remote_main("MOVED.md", "main moved on\n")
        wt = self.worktrees / "ko-131-add-a-thing"

        self.serve(self.pr_state(mergeable="MERGEABLE"))
        self.resume()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(),
                         approved)
        self.assertEqual(self.pushed(), [(BRANCH, approved)])

        self.serve(self.pr_state(mergeable=None))
        self.resume()
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(),
                         approved)
        self.assertEqual(self.pushed(), [(BRANCH, approved)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM ledger WHERE kind = 'note'"
                      " AND text LIKE 'Merged main into%'"), [(0,)])


SPEC = Path("e2e/capture/KO-1.capture.ts")
CAPTURE = """\
import pathlib, sys
spec = pathlib.Path("e2e/capture/KO-1.capture.ts")
if spec.read_text() == "BROKEN":
    print("page redirected into the index")
    sys.exit(1)
pathlib.Path(sys.argv[-1], "01-state.png").write_bytes(b"png")
"""


class CaptureOnlyBabysitFixTests(unittest.TestCase):
    """A babysit fix turn whose only change is the ticket's capture spec, in
    a capture directory that ignores itself, makes no commit; the edited
    spec is still progress, and only a turn that changes neither the head
    nor the spec fails the run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name, "repo")
        self.root.mkdir()
        self.enterContext(patch.dict(
            os.environ, {"HOLOPHYTE_HOME": str(Path(tmp.name, "home"))}))
        script = Path(tmp.name, "capture.py")
        script.write_text(CAPTURE)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test implementer"),
                     ("config", "user.email", "implementer@example.test"),
                     ("commit", "--allow-empty", "-qm", "base"),
                     ("checkout", "-qb", "task")):
            self.git(*args)
        (self.root / "ui").mkdir()
        (self.root / "ui/page.html").write_text("<p>load</p>\n")
        self.git("add", ".")
        self.git("commit", "-qm", "candidate")
        self.head = self.git("rev-parse", "HEAD")
        (self.root / SPEC).parent.mkdir(parents=True)
        (self.root / SPEC.parent / ".gitignore").write_text("*\n")
        (self.root / SPEC).write_text("BROKEN")
        self.project = holophyte.config.project.Project.locate(self.root)
        self.project.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.project.config_path.write_text(
            '[merge]\nmode = "pr"\napprove = "auto"\nui_paths = ["ui/**"]\n'
            f"ui_capture = '{shlex.join([sys.executable, str(script)])}'\n"
            "ui_capture_local = true\n")
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project_id = store.tickets.ensure_project(self.conn, "team-1", self.root)
        ticket = store.tickets.mirror_ticket(
            self.conn, project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1",
            acceptance_criteria=["Given the page, then it shows load"],
            verification_commands=["true"], time_box_ms=60 * 60 * 1000)
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run_id = store.claim(self.conn, project_id, ticket)
        set_phase(self.conn, self.run_id, "merge_gate")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True, stderr=subprocess.PIPE
        ).strip()

    def review_fix(self, fix):
        replies = ["CRITERION 1: unwitnessed — the capture failed\n"
                   "VERDICT: REQUEST_CHANGES",
                   "CRITERION 1: met — ui/page.html shows load\n"
                   "VERDICT: APPROVE"]
        self.prompts, self.fix_turns = [], 0

        def reviewer(target, role, goal, *args, **kwargs):
            self.prompts.append(goal)
            return replies.pop(0)

        def fixer(*args, **kwargs):
            self.fix_turns += 1
            fix()
            return "Pointed the capture spec at the index.", False

        def publish(project, wt, output, files, task_id, note, media_repo):
            return "Media lives here.", {
                file: f"https://example.test/{file.name}" for file in files}

        with (patch.object(holophyte.loop.review_round, "agent", reviewer),
              patch.object(holophyte.loop.implement, "_transport_timed", fixer),
              patch.object(pr_media, "_publish_git", publish),
              patch.object(github, "push_branch"),
              patch.object(github, "rest", return_value={"body": "Load."}),
              patch.object(holophyte.pr.pullrequest, "refresh_pr_text")):
            return babysitter._review_fix(
                self.project, self.conn, self.run_id, None, "KO-1", "task",
                self.root, self.head, None, 30,
                PullRequest("example.test", "o", "n", 1,
                            "https://example.test/pull/1"),
                "Show load in `ui/page.html`.\n", "true", (),
                ["Given the page, then it shows load"],
                fix_note="repair the capture", budget_min=10)

    def test_a_fix_that_only_edits_the_ignored_spec_is_reviewed_again(self):
        approved = self.review_fix(lambda: (self.root / SPEC).write_text("FIXED"))

        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(approved, self.head)
        self.assertEqual(self.fix_turns, 1)
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("failed (exit 1)", self.prompts[0])
        self.assertNotIn("failed (exit 1)", self.prompts[1])
        self.assertIn("https://example.test/01-state.png", self.prompts[1])

    def test_a_fix_that_changes_neither_head_nor_spec_still_fails(self):
        with self.assertRaisesRegex(RunFailure, "fix round made no progress"):
            self.review_fix(lambda: None)

        self.assertEqual(self.fix_turns, 1)
        self.assertEqual(len(self.prompts), 1)


if __name__ == "__main__":
    unittest.main()

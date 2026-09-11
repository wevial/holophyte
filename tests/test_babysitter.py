"""`holophyte.babysitter`: the pass's texts, read and written without GitHub.

The verdict parser is what decides which thread gets fixed, which gets a
decline, and which parks the run for a person; the acceptance tests in
`test_factory_loop.py` witness the pass end to end, and this holds the
parser's edges: a thread with no line is `HUMAN`, a verdict is read whatever
separator the model reached for, and a number outside the listing is
ignored rather than filed against a thread that does not exist.

Run: python3 -m unittest discover -s tests -p 'test_babysit*' -v
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
# The repo root for `holophyte`, and `tests/` itself for the loop harness
# (`test_factory_loop`) and its scripted agent (`fake_agent`).
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fake_agent import APPROVE, Commit, Idle, Reply  # noqa: E402
from test_factory_loop import (  # noqa: E402
    BRANCH,
    MergeModeFixture,
)

import holophyte.loop  # noqa: E402
from holophyte import babysitter, pr  # noqa: E402
from holophyte.pr import PullRequest, Thread  # noqa: E402

PULL = PullRequest(host="github.com", owner="o", name="r", number=3,
                   url="https://github.com/o/r/pull/3")


def thread(n, body="a thread", author="bot", path="a.py"):
    line = n
    return Thread(id=f"T{n}", path=path, line=line, author=author, body=body,
                  url=f"{PULL.url}#discussion_r{n}")


def run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


class FoldChecksTests(unittest.TestCase):
    """`pr.fold_checks()`: the rollup beside the head's check runs and the
    branch's required contexts. Regression: REL-120 was parked "ready to
    merge" 19 seconds after its PR opened, on a rollup that said success
    while vitest, the build and three review bots were still queued."""

    def test_a_run_still_in_progress_is_pending_whatever_the_rollup_says(self):
        runs = [run("lint"), run("vitest", status="in_progress",
                                 conclusion=None)]
        self.assertEqual(pr.fold_checks("SUCCESS", runs, []), "pending")

    def test_every_run_completed_without_failure_is_green(self):
        runs = [run("lint"), run("vitest"), run("docs", conclusion="skipped")]
        self.assertEqual(pr.fold_checks("SUCCESS", runs, []), "success")

    def test_a_completed_run_that_failed_is_red(self):
        runs = [run("lint"), run("vitest", conclusion="failure")]
        self.assertEqual(pr.fold_checks("SUCCESS", runs, []), "failure")

    def test_a_required_context_with_no_run_yet_is_pending(self):
        runs = [run("lint")]
        self.assertEqual(pr.fold_checks("SUCCESS", runs, ["vitest"]),
                         "pending")
        self.assertEqual(pr.fold_checks("SUCCESS", runs + [run("vitest")],
                                        ["vitest"]), "success")

    def test_no_rules_and_no_runs_is_green_as_the_rollup_alone_said(self):
        self.assertEqual(pr.fold_checks(None, [], []), "success")

    def test_a_read_that_did_not_come_back_is_pending_never_green(self):
        self.assertEqual(pr.fold_checks("SUCCESS", None, []), "pending")
        self.assertEqual(pr.fold_checks("SUCCESS", [run("lint")], None),
                         "pending")
        # Red still wins: the rollup is the cheapest red signal.
        self.assertEqual(pr.fold_checks("FAILURE", None, None), "failure")

    def test_check_data_the_babysitter_cannot_read_is_pending_never_green(self):
        # Review finding: a `check_runs` that is not a list, or an entry
        # that is not a run, was skipped and the rest read as green.
        self.assertEqual(pr.fold_checks("SUCCESS", "unreadable", []),
                         "pending")
        self.assertEqual(pr.fold_checks("SUCCESS", [run("lint"), "garbage"],
                                        []), "pending")
        self.assertEqual(pr.fold_checks("SUCCESS", [run("lint"), None], []),
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
    """`pr.parse_pr_text()`: the `TITLE:` line and the body after it, or
    None for a reply the loop cannot open a PR from; `pr.pr_body_written()`
    ends the body with the Linear line (KO-336)."""

    def test_the_title_line_and_the_body_after_it_are_read(self):
        reply = ("TITLE: [Contacts] Put Contact Name first\n\n"
                 "The two forms now \u2026")

        self.assertEqual(pr.parse_pr_text(reply),
                         ("[Contacts] Put Contact Name first",
                          "The two forms now \u2026"))

    def test_a_reply_without_a_title_line_is_none(self):
        self.assertIsNone(pr.parse_pr_text(
            "Here is the description.\n\nThe two forms now \u2026"))

    def test_an_empty_or_overlong_title_is_none(self):
        self.assertIsNone(pr.parse_pr_text("TITLE:\n\nA body."))
        self.assertIsNone(pr.parse_pr_text(f"TITLE: {'x' * 121}\n\nA body."))
        self.assertIsNotNone(
            pr.parse_pr_text(f"TITLE: {'x' * 120}\n\nA body."))

    def test_the_written_body_ends_with_the_linear_line(self):
        body = pr.pr_body_written("What changed.\n", "KO-336",
                                  "https://linear.app/example/issue/KO-336")

        self.assertEqual(body.splitlines()[-1],
                         "Linear: KO-336 (https://linear.app/example/issue/"
                         "KO-336)")
        self.assertTrue(body.startswith("What changed.\n\n"))
        self.assertEqual(pr.pr_body_written("Text", "KO-1", None).splitlines()[-1],
                         "Linear: KO-1")


class PullStatusTests(unittest.TestCase):
    """`pr.pull_status()` reads the facts `/attention` shows on a parked
    pull request (KO-368) from the same answer the reconcile already
    makes: the head's `statusCheckRollup` and `reviewDecision`."""

    OPEN = {"state": "OPEN", "merged": False, "mergeCommit": None,
            "mergedBy": None, "updatedAt": "2026-09-10T10:00:00Z",
            "reviewThreads": {"totalCount": 2}}

    def read(self, node):
        with patch.object(pr, "graphql",
                          return_value={"repository": {"pullRequest": node}}):
            return pr.pull_status(None, PULL)

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

        comments = pr._comment_nodes(page)

        self.assertEqual([c.author_kind for c in comments],
                         ["bot", "user", "unknown"])
        self.assertEqual([c.author for c in comments],
                         ["devin-ai-integration", "wevial", "unknown"])


class MergeableReadTests(unittest.TestCase):
    """`pr.pr_state()` carries GitHub's `mergeable` answer through to the
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
                pr, "graphql",
                return_value={"repository": {"pullRequest": node}}), \
                patch.object(pr, "rest", return_value=[]):
            return pr.pr_state(None, PULL)

    def test_the_mergeable_answer_is_carried(self):
        self.assertEqual(self.read("CONFLICTING").mergeable, "CONFLICTING")
        self.assertEqual(self.read("MERGEABLE").mergeable, "MERGEABLE")

    def test_an_absent_or_null_answer_reads_unknown(self):
        self.assertEqual(self.read().mergeable, "UNKNOWN")
        self.assertEqual(self.read(None).mergeable, "UNKNOWN")


class ConflictingPullRequestTests(MergeModeFixture):
    """A babysit pass over a pull request GitHub reports CONFLICTING
    merges `origin/main` -- the remote's `main`, not the checkout's
    possibly stale local one -- into the branch, pushes, and goes back
    to waiting on checks; nothing is rebased or force-pushed, so review
    threads keep their lines. A merge that stops in the tree is the
    implementer's to resolve, and one left unresolved parks the run
    naming the conflicting paths. MERGEABLE and UNKNOWN answers trigger
    none of it (KO-377)."""

    def parked_on_a_nit(self, work):
        """A run parked on its pull request by a declined nit thread,
        under `approve = "human"`; `work` is the implementer step that
        made the candidate. Returns the approved candidate's sha."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state([self.NIT])])
        self.loop(work, APPROVE,
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        for path in self.api_dir.iterdir():
            path.unlink()
        return approved

    def remote_main(self, path, body):
        """A commit on top of `main` as the remote would hold it: built in
        the target's object store without moving the checkout's `main`
        (which stays behind, as a writer host's does while the remote
        moved), then `refs/remotes/origin/main` pointed at it -- the
        state the fake route's swallowed `git fetch origin` would leave.
        Returns the new `main` sha."""
        index = self.worktrees.parent / "remote-main-index"
        env = dict(os.environ, GIT_INDEX_FILE=str(index))

        def plumb(*args, **kw):
            return subprocess.run(
                ["git", *args], cwd=self.target, env=env, check=True,
                capture_output=True, text=True, **kw).stdout.strip()

        plumb("read-tree", "main")
        blob = plumb("hash-object", "-w", "--stdin", input=body)
        plumb("update-index", "--add", "--cacheinfo",
              f"100644,{blob},{path}")
        moved = self.git("commit-tree", plumb("write-tree"), "-p", "main",
                         "-m", "main moved on").strip()
        index.unlink(missing_ok=True)
        self.git("update-ref", "refs/remotes/origin/main", moved)
        return moved

    def resume(self, *script):
        """`--babysit` the parked run and drive it through the harness,
        faked GitHub serving whatever `serve()` last laid down."""
        holophyte.loop.babysit_ticket(self.tgt, "KO-131", "look again",
                                      out=io.StringIO())
        return self.loop(*script, provider=self.provider())

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
        self.assertIn("a human says merge", self.question())

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


if __name__ == "__main__":
    unittest.main()

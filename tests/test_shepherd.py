"""`holophyte.shepherd`: the pass's texts, read and written without GitHub.

The verdict parser is what decides which thread gets fixed, which gets a
decline, and which parks the run for a person; the acceptance tests in
`test_factory_loop.py` witness the pass end to end, and this holds the
parser's edges: a thread with no line is `HUMAN`, a verdict is read whatever
separator the model reached for, and a number outside the listing is
ignored rather than filed against a thread that does not exist.

Run: python3 -m unittest discover -s tests -p 'test_shepherd*' -v
"""
import tempfile
import unittest
from pathlib import Path

from holophyte import pr, shepherd
from holophyte.pr import PullRequest, Thread

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

    def test_check_data_the_shepherd_cannot_read_is_pending_never_green(self):
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
        return shepherd.adjudication_brief(
            PULL, (self.THREAD,), "the ticket", "c" * 40,
            shepherd.conventions(wt))

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
            self.assertEqual(shepherd.conventions(Path(tmp)), ())
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

        verdicts = shepherd.parse_verdicts(reply, 3)

        self.assertEqual(verdicts[1], ("ADDRESS", "the null check is missing"))
        self.assertEqual(verdicts[2][0], "HUMAN")
        self.assertEqual(verdicts[3], ("DECLINE", "out of scope"))
        self.assertNotIn(9, verdicts)

    def test_the_round_text_reads_as_a_review_round(self):
        threads = (thread(1, "crash on None"), thread(2, "rename n",
                                                       author="style"))
        verdicts = {1: ("ADDRESS", "real"), 2: ("DECLINE", "taste")}

        text = shepherd.round_reply(PULL, 1, threads, verdicts, "success",
                                    "a" * 40)

        self.assertTrue(text.endswith("VERDICT: REQUEST_CHANGES"))
        self.assertIn("- a.py:1 @bot: crash on None -- ADDRESS: real", text)
        self.assertEqual(shepherd.route_of(threads), "github:bot+style")
        self.assertTrue(shepherd.round_reply(PULL, 2, (), {}, "success",
                                             "a" * 40)
                        .endswith("VERDICT: APPROVE"))
        self.assertEqual(shepherd.route_of(()), "github:ci")

    def test_replies_open_with_the_model_header(self):
        addressed = shepherd.addressed_reply("codex-sol-medium", "added the"
                                             " check", "b" * 40)
        declined = shepherd.declined_reply("codex-sol-medium", "taste")

        for text in (addressed, declined):
            self.assertTrue(text.startswith(
                "---- Comment by codex-sol-medium ----\n"), text)
        self.assertIn(f"Addressed in {'b' * 40}: added the check", addressed)
        self.assertIn("Declined: taste", declined)
        self.assertEqual(shepherd.parse_summaries(
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


if __name__ == "__main__":
    unittest.main()

"""`holophyte.shepherd`: the pass's texts, read and written without GitHub.

The verdict parser is what decides which thread gets fixed, which gets a
decline, and which parks the run for a person; the acceptance tests in
`test_factory_loop.py` witness the pass end to end, and this holds the
parser's edges: a thread with no line is `HUMAN`, a verdict is read whatever
separator the model reached for, and a number outside the listing is
ignored rather than filed against a thread that does not exist.

Run: python3 -m unittest discover -s tests -p 'test_shepherd*' -v
"""
import unittest

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

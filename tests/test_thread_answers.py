"""Standalone imports must not depend on the factory's import order."""
import subprocess
import tempfile
import unittest
from pathlib import Path

import store
import store.read
import store.tickets
from holophyte.babysit import thread_answers
from holophyte.pr import github

REPO = "https://github.com/OWNER/NAME"
PULL = github.PullRequest("github.com", "OWNER", "NAME", 7,
                          "https://github.com/OWNER/NAME/pull/7")


class ThreadAnswersImportTests(unittest.TestCase):
    def test_standalone_import_in_fresh_interpreter(self):
        result = subprocess.run(
            ["python3", "-c", "import holophyte.babysit.thread_answers"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class GithubLinksTests(unittest.TestCase):
    def test_reviewer_mount_links_point_at_the_candidate_on_github(self):
        answer = ("See [stats.ts:92–106]"
                  "(/home/reviewer/candidate/lib/utils/stats.ts:92) and"
                  " [route](/workspace/app/x/route.ts), also"
                  " [range](/workspace/a.py:3–7).")
        self.assertEqual(
            thread_answers.github_links(answer, REPO, "S"),
            "See [stats.ts:92–106](https://github.com/OWNER/NAME/blob/S/"
            "lib/utils/stats.ts#L92) and [route](https://github.com/OWNER/"
            "NAME/blob/S/app/x/route.ts), also [range](https://github.com/"
            "OWNER/NAME/blob/S/a.py#L3-L7).")

    def test_other_links_are_unchanged(self):
        answer = ("Per [the docs](https://example.com/guide:12) and"
                  " [local](src/app.py:30).")
        self.assertEqual(thread_answers.github_links(answer, REPO, "S"), answer)


class AskPromptTests(unittest.TestCase):
    def test_prompt_says_thread_and_ticket_are_included_and_github_unreachable(self):
        pull = github.PullRequest("github.com", "OWNER", "NAME", 7,
                              "https://github.com/OWNER/NAME/pull/7")
        thread = github.Thread("1", "app.py", 1, "maintainer",
                           "@holophyte ask: Why?", "url")
        prompt = thread_answers.ask_prompt(pull, "Ticket body", thread)
        self.assertIn("thread and ticket are included", prompt)
        self.assertIn("GitHub is not reachable", prompt)
        self.assertIn("do not fetch it or report on access", prompt)


class AskPromptRelatedTicketsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(str(Path(tmp.name) / "store.db"))
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1", tmp.name)

    def mirror(self, number, title, depends_on=(), criteria=("works",)):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, f"issue-{number}", f"KO-{number}", title,
            acceptance_criteria=list(criteria), verification_commands=["true"],
            depends_on=[f"issue-{n}" for n in depends_on])

    def prompt_for(self, ticket_id, author="asker"):
        run = store.claim(self.conn, self.project_id, ticket_id)
        thread = github.Thread("1", "app.py", 1, author,
                               "@holophyte ask: Is the rest filed?", "url")
        return thread_answers.ask_prompt(
            PULL, "Ticket body", thread,
            store.read.related_tickets(self.conn, run))

    def test_prompt_lists_the_tickets_it_depends_on_and_that_follow_it(self):
        self.mirror(1, "Part one", criteria=())
        middle = self.mirror(2, "Part two", depends_on=[1])
        self.mirror(3, "Part three", depends_on=[2])
        prompt = self.prompt_for(middle)
        self.assertIn("Depends on: KO-1 (needs_spec): Part one\n", prompt)
        self.assertIn("Followed by: KO-3 (ready): Part three\n", prompt)

    def test_prompt_says_none_without_related_tickets(self):
        self.mirror(1, "Unrelated")
        alone = self.mirror(4, "Alone")
        prompt = self.prompt_for(alone)
        self.assertIn("Depends on: none\n", prompt)
        self.assertIn("Followed by: none\n", prompt)

    def test_prompt_replies_to_the_asker_rather_than_advising_a_third_party(self):
        prompt = self.prompt_for(self.mirror(4, "Alone"))
        self.assertIn("Your answer is the reply to @asker", prompt)
        self.assertNotIn("End with whether a change seems warranted, as advice"
                         " only.", prompt)

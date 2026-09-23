"""Standalone imports must not depend on the factory's import order."""
import subprocess
import unittest
from pathlib import Path

from holophyte import pr, thread_answers

REPO = "https://github.com/OWNER/NAME"


class ThreadAnswersImportTests(unittest.TestCase):
    def test_standalone_import_in_fresh_interpreter(self):
        result = subprocess.run(
            ["python3", "-c", "import holophyte.thread_answers"],
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
        pull = pr.PullRequest("github.com", "OWNER", "NAME", 7,
                              "https://github.com/OWNER/NAME/pull/7")
        thread = pr.Thread("1", "app.py", 1, "maintainer",
                           "@holophyte ask: Why?", "url")
        prompt = thread_answers.ask_prompt(pull, "Ticket body", thread)
        self.assertIn("thread and ticket are included", prompt)
        self.assertIn("GitHub is not reachable", prompt)
        self.assertIn("do not fetch it or report on access", prompt)

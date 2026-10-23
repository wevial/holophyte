"""The review base follows main forward once the candidate has merged it."""

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from holophyte.loop import adjudicate, review_round
from tests.host_fixture import git

TICKET = "Change `src/app.py` so it greets."
OWN = {"src/app.py"}
MAINS = ("ci.yml", "test_other.py")


class Captured(Exception):
    pass


class ReviewBaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.git("init", "-q", "-b", "main")
        self.commit({"README.md": "a\n"}, "A")
        self.a = self.git("rev-parse", "HEAD")
        self.commit({"README.md": "b\n"}, "B")
        self.b = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "task")
        self.commit({"src/app.py": "print('hi')\n"}, "own change")
        self.git("checkout", "-q", "main")
        self.commit({".github/workflows/ci.yml": "on: push\n",
                     "tests/test_other.py": "pass\n"}, "unrelated")
        self.m = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "task")

    def git(self, *args):
        return git(self.repo, *args)

    def commit(self, files, message):
        for name, text in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def merge_main(self):
        self.git("merge", "-q", "--no-edit", "main")
        self.commit({"src/app.py": "print('hello')\n"}, "own follow-up")

    def drive(self, run):
        calls = []

        def capture(target, role, goal, *args, **kwargs):
            calls.append((kwargs["base_sha"], goal))
            raise Captured

        common = dict(
            project=Mock(config=Mock(return_value={})),
            conn=None, run_id=1, provider=None, task_id=1, branch="task",
            wt=self.repo, beat_s=1, base_sha=self.b,
            sha=self.git("rev-parse", "HEAD"), ticket=TICKET,
            verify_cmd="true", contracts=(),
        )
        with contextlib.ExitStack() as stack:
            for module in (review_round, adjudicate):
                stack.enter_context(patch.object(module, "agent", side_effect=capture))
                stack.enter_context(patch.object(module, "set_phase"))
                stack.enter_context(
                    patch.object(module, "run_verify", return_value=(True, "ok")))
            stack.enter_context(
                patch.object(review_round, "merge_conflicts", return_value=[]))
            with self.assertRaises(Captured):
                run(common)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def review(self):
        return self.drive(lambda common: review_round._review_rounds(
            **common, criteria=(), budget_min=10, cap=1))

    def changed(self, base):
        return set(self.git("diff", "--name-only", f"{base}..HEAD").split())

    def test_review_of_merged_candidate_measures_from_merged_main(self):
        self.merge_main()
        base, prompt = self.review()
        self.assertEqual(base, self.m)
        self.assertEqual(self.changed(base), OWN)
        for name in MAINS:
            self.assertNotIn(name, prompt)

    def test_adjudication_of_merged_candidate_measures_from_merged_main(self):
        self.merge_main()
        base, _ = self.drive(lambda common: adjudicate._terminal_adjudication(
            **common, task={}, cap=1))
        self.assertEqual(base, self.m)

    def test_base_stays_frozen_unless_main_moved_forward_into_candidate(self):
        with self.subTest("main moved on but not merged"):
            base, prompt = self.review()
            self.assertEqual(base, self.b)
            self.assertEqual(self.changed(base), OWN)
            for name in MAINS:
                self.assertNotIn(name, prompt)
        with self.subTest("main moved back behind the frozen base"):
            self.git("branch", "-f", "main", self.a)
            base, _ = self.review()
            self.assertEqual(base, self.b)


if __name__ == "__main__":
    unittest.main()

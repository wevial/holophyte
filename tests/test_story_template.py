"""`ticket_template.py --story DIR` validates a story body and its witnesses."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.story_fixture import witness_path, write_story

ROOT = Path(__file__).resolve().parent.parent
ADVISORY = "  - advisory: "


class StoryCliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / ".gitignore").write_text("build/\n")

    def story(self, **kwargs):
        return write_story(Path(self.tmp.name) / "plans", **kwargs)

    def edit(self, directory, old, new):
        body = directory / "story.md"
        text = body.read_text()
        self.assertIn(old, text)
        body.write_text(text.replace(old, new))

    def retarget(self, directory, old, new):
        self.edit(directory, old, new)
        for child in (directory / "children").glob("*.md"):
            child.write_text(child.read_text().replace(old, new))

    def run_cli(self, directory, repo=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "ticket_template.py"),
             "--repo", str(repo or self.repo), "--story", str(directory)],
            capture_output=True, text=True)

    def blockers(self, result):
        return [line for line in result.stdout.splitlines()
                if line.startswith("  - ") and not line.startswith(ADVISORY)]

    def assert_refused(self, directory, expected):
        result = self.run_cli(directory)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], f"{directory}: INVALID")
        problems = self.blockers(result)
        self.assertEqual(len(problems), 1, result.stdout)
        self.assertIn(expected, problems[0])

    def assert_advised(self, directory, expected, repo=None):
        result = self.run_cli(directory, repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines()[0], f"{directory}: OK")
        advisories = [line for line in result.stdout.splitlines()
                      if line.startswith(ADVISORY) and expected in line]
        self.assertEqual(len(advisories), 1, result.stdout)


class ValidStoryTests(StoryCliCase):
    def test_fixture_story_with_two_witnesses_is_ok(self):
        directory = self.story()
        result = self.run_cli(directory)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"{directory}: OK"])


class StoryBodyTests(StoryCliCase):
    def test_goal_section_missing_is_refused(self):
        directory = self.story()
        self.edit(directory, "## Goal\n", "")
        self.edit(directory, "An operator downloads every order as one CSV "
                  "file.\n\n", "")
        self.assert_refused(directory, "missing section '## Goal'")

    def test_no_witness_lines_is_refused(self):
        self.assert_refused(self.story(witnesses=0), "no witness lines")

    def test_eleven_witnesses_are_refused(self):
        self.assert_refused(self.story(witnesses=11),
                            "has 11 witnesses; the cap is 10")

    def test_witness_line_without_its_test_is_refused(self):
        directory = self.story()
        self.edit(directory, f" (a test in {witness_path(2)} witnesses "
                  "outcome 2)", "")
        self.assert_refused(directory, "not in the criterion form")

    def test_witness_without_command_is_refused(self):
        directory = self.story()
        self.edit(directory, "W2: python3 -m unittest tests.test_story_w2\n", "")
        self.assert_refused(directory, "witness W2 has no line in "
                            "'Witness commands'")

    def test_two_witnesses_on_one_file_are_refused(self):
        directory = self.story()
        self.edit(directory, f"(a test in {witness_path(2)}",
                  f"(a test in {witness_path(1)}")
        self.assert_refused(directory, "witnesses W1 and W2 name one file: "
                            f"{witness_path(1)}")

    def test_two_spellings_of_one_file_are_refused(self):
        directory = self.story()
        self.edit(directory, f"(a test in {witness_path(2)}",
                  f"(a test in ./{witness_path(1)}")
        self.assert_refused(directory, "witnesses W1 and W2 name one file: "
                            f"./{witness_path(1)}")

    def test_six_standing_orders_are_refused(self):
        self.assert_refused(self.story(standing_orders=6),
                            "'Standing orders' has 6 lines; the limit is 5")

    def test_open_question_is_refused(self):
        directory = self.story()
        self.edit(directory, "- None", "- Which delimiter?")
        self.assert_refused(directory, "'Open questions' must read exactly")


class WitnessFileTests(StoryCliCase):
    def move_witness(self, directory, path):
        self.retarget(directory, witness_path(2), path)
        source = directory / "witnesses" / witness_path(2)
        target = directory / "witnesses" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, target)

    def test_gitignored_witness_file_is_refused(self):
        directory = self.story()
        self.move_witness(directory, "build/test_story_w2.py")
        self.assert_refused(directory, "gitignored witness file in W2: "
                            "build/test_story_w2.py")

    def test_witness_file_outside_the_repository_is_refused(self):
        directory = self.story()
        self.retarget(directory, witness_path(2),
                      "../outside/test_story_w2.py")
        self.assert_refused(directory, "outside the repository in W2: "
                            "../outside/test_story_w2.py")

    def test_witness_file_absent_from_the_witnesses_tree_is_refused(self):
        directory = self.story()
        (directory / "witnesses" / witness_path(2)).unlink()
        self.assert_refused(directory, f"W2: {witness_path(2)}")
        self.assertIn("missing", self.blockers(self.run_cli(directory))[0])

    def test_repository_git_cannot_read_is_advised(self):
        unreadable = Path(self.tmp.name) / "no-such-repo"
        self.assert_advised(self.story(), f"could not check paths against "
                            f"{unreadable}", repo=unreadable)


class StoryAdvisoryTests(StoryCliCase):
    def test_six_witnesses_are_advised(self):
        self.assert_advised(self.story(witnesses=6), "'Witnesses' has 6")

    def test_skip_call_in_a_witness_file_is_advised(self):
        directory = self.story()
        path = directory / "witnesses" / witness_path(1)
        path.write_text(path.read_text().replace(
            "self.assertEqual", "self.skipTest('later')\n        self.assertEqual"))
        self.assert_advised(directory, "W1 calls skipTest(")

    def test_one_pass_wording_in_a_criterion_is_advised(self):
        directory = self.story()
        self.edit(directory, "outcome 1 holds (", "outcome 1 holds after one pass (")
        self.assert_advised(directory, "W1's criterion says 'one pass'")

    def test_one_pass_wording_in_what_a_criterion_witnesses_is_advised(self):
        directory = self.story()
        self.edit(directory, "witnesses outcome 1)",
                  "witnesses outcome 1 after one pass)")
        self.assert_advised(directory, "W1's criterion says 'one pass'")


if __name__ == "__main__":
    unittest.main()

"""The babysit pass's main-side verify after a merge of main turns it red.

Main runs only what exists on main: a verify command naming a file only
the branch adds is left out on main's side, and when the merged tree's
failing command is one left out, main is not compared and the implementer
gets the fix turn instead of a "main is red" park. Real git, the real
verify runner.

Run: HOLOPHYTE_HOME=$(mktemp -d) python3 -m unittest discover -s tests \
    -p 'test_main_side_comparable.py' -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import babysit_fixture as cases  # noqa: E402
from fake_agent import IMPLEMENT, Commit, Idle  # noqa: E402
from loop_fixture import MergeModeFixture, StubProvider, a_task  # noqa: E402

from holophyte.loop.gates import drop_candidate_modules  # noqa: E402

BRANCH_ONLY = "notes/branch_only.txt"


@dataclass
class TwoFiles:
    """An implementer turn that commits `notes/branch_only.txt` holding
    `marker` and THING.md together."""
    role = IMPLEMENT

    def play(self, cwd, turn):
        (cwd / "notes").mkdir(exist_ok=True)
        (cwd / BRANCH_ONLY).write_text("marker\n")
        (cwd / "THING.md").write_text("the thing\n")
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "candidate"], cwd=cwd,
                       check=True)
        return "committed: candidate"


class BranchOnlyPathTests(cases.ConflictingMainHelpers, MergeModeFixture):
    def candidate(self, command):
        self.parked_on_a_nit(TwoFiles())
        self.provider = lambda: StubProvider(
            dict(a_task(), body=self.BODY, verify=command))
        moved = self.remote_main("MOVED.md", "main moved\n")
        self.serve(self.pr_state(mergeable="CONFLICTING"), self.pr_state())
        return moved

    def round_commands(self):
        return [row for (raw,) in self.read(
            "SELECT verificationResults FROM reviewRounds WHERE runId = 2"
            " ORDER BY round") for row in json.loads(raw)]

    def main_side_events(self):
        return [event for (event,) in self.read(
            "SELECT summary FROM runEvents WHERE runId = 2"
            " AND summary LIKE 'main-side verify%'")]

    def test_a_command_naming_a_branch_only_file_is_left_out_on_main(self):
        kept = "test -e FIXED.md -o ! -e THING.md"
        moved = self.candidate(f"grep -q marker {BRANCH_ONLY}\n{kept}")
        fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertNotIn("main is red", self.question())
        self.assertEqual(fake.roles, ["implement", "implement"])
        goal = fake.turns[0].goal
        self.assertNotIn("main is red", goal)
        self.assertIn(f"main at {moved} passes", goal)
        main_side = [row["command"] for row in self.round_commands()
                     if moved in row["output"]]
        self.assertEqual(main_side, [kept])
        skipped = [e for e in self.main_side_events() if "skipped" in e]
        self.assertTrue(skipped, self.main_side_events())
        self.assertIn(BRANCH_ONLY, skipped[0])

    def assert_positive_probe_left_out(self, probe):
        kept = "test -e FIXED.md -o ! -e THING.md"
        moved = self.candidate(f"{probe}\n{kept}")
        fake, _ = self.resume(Commit("fix merge", path="FIXED.md"), Idle(""))
        self.assertNotIn("main is red", self.question())
        self.assertEqual(fake.roles, ["implement", "implement"])
        main_side = [row["command"] for row in self.round_commands()
                     if moved in row["output"]]
        self.assertEqual(main_side, [kept])
        self.assertTrue([e for e in self.main_side_events()
                         if "skipped" in e and BRANCH_ONLY in e])

    def test_a_positive_probe_of_a_branch_only_file_is_left_out_on_main(self):
        self.assert_positive_probe_left_out(f"test -f {BRANCH_ONLY}")

    def test_a_doubly_negated_probe_of_a_branch_only_file_is_left_out(self):
        self.assert_positive_probe_left_out(f"! test ! -e {BRANCH_ONLY}")

    def test_a_failing_command_left_out_on_main_is_not_compared(self):
        moved = self.candidate(f"grep -q fixed {BRANCH_ONLY}")
        fake, _ = self.resume(
            Commit("fix", path=BRANCH_ONLY, body="marker\nfixed\n"), Idle(""))
        self.assertNotIn("main is red", self.question())
        self.assertEqual(fake.roles, ["implement", "implement"])
        goal = fake.turns[0].goal
        self.assertNotIn("main is red", goal)
        self.assertIn("was not compared", goal)
        rows = self.round_commands()
        self.assertFalse([r for r in rows if moved in r["output"]])
        (event,) = [e for e in self.main_side_events()
                    if "not comparable" in e]
        self.assertTrue(event.startswith(f"main-side verify at {moved[:12]}"),
                        event)
        self.assertIn(BRANCH_ONLY, event)
        self.assertIn("the fix rounds moved the candidate", self.question())

    def test_a_red_main_naming_only_its_own_files_still_parks(self):
        command = "grep -q absent MOVED.md"
        moved = self.candidate(command)
        fake, _ = self.resume()
        self.assertEqual(fake.roles, [])
        self.assertIn(f"main is red at {moved}; verify command: {command}",
                      self.question())
        self.assertEqual(self.main_side_events(), [])


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


class BranchOnlyPathRuleTests(unittest.TestCase):
    """The path rule over a real repository: main has `old.spec.ts`, the
    candidate adds `new.spec.ts` and `src/new.js`, and both carry an
    untracked `.venv`."""

    def setUp(self):
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        self.write("e2e/checks/old.spec.ts", "old\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.main_sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-q", "-b", "candidate")
        self.write("e2e/checks/new.spec.ts", "new\n")
        self.write("src/new.js", "new.ts\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "candidate")
        self.write(".venv/bin/python", "")

    def write(self, path, body):
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_text(body)

    def test_branch_only_files_are_trimmed_from_tests_or_left_out(self):
        command = "\n".join((
            "npx playwright test --config e2e/checks.config.ts"
            " e2e/checks/new.spec.ts e2e/checks/old.spec.ts --reporter=line",
            'grep -q "new[.]ts" src/new.js',
            ".venv/bin/python -m tests.capture_site",
            "npx playwright test e2e/checks/new.spec.ts"))
        side = drop_candidate_modules(command, self.repo, self.repo,
                                      self.main_sha)
        self.assertEqual(side.command.splitlines(), [
            "npx playwright test --config e2e/checks.config.ts"
            " e2e/checks/old.spec.ts --reporter=line",
            ".venv/bin/python -m tests.capture_site"])
        self.assertEqual(side.paths, ["e2e/checks/new.spec.ts", "src/new.js"])

    def test_a_quoted_branch_only_test_file_is_trimmed_not_left_out(self):
        side = drop_candidate_modules(
            'npx playwright test "e2e/checks/new.spec.ts" e2e/checks/old.spec.ts',
            self.repo, self.repo, self.main_sha)
        self.assertEqual(side.command,
                         "npx playwright test e2e/checks/old.spec.ts")
        self.assertEqual(side.dropped, {})

    def test_a_block_line_that_loses_a_clause_counts_as_left_out(self):
        line = "grep -q x src/new.js && test -e e2e/checks/old.spec.ts"
        side = drop_candidate_modules(f"{line}\ntrue", self.repo, self.repo,
                                      self.main_sha)
        self.assertEqual(side.command.splitlines(),
                         ["test -e e2e/checks/old.spec.ts", "true"])
        self.assertEqual(side.dropped[line], ["src/new.js"])


if __name__ == "__main__":
    unittest.main()

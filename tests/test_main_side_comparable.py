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
import unittest
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import babysit_fixture as cases  # noqa: E402
from fake_agent import IMPLEMENT, Commit, Idle  # noqa: E402
from loop_fixture import MergeModeFixture, StubProvider, a_task  # noqa: E402

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
        self.assertEqual(rows[-1]["exitCode"], 0)


if __name__ == "__main__":
    unittest.main()

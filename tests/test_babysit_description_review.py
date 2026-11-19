"""Which covering-review replies count as faulting only the PR description."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import holophyte.config.project  # noqa: E402 - after the sys.path insert
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import holophyte.pr.pullrequest  # noqa: E402
import store  # noqa: E402
import store.read  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.babysit import babysitter  # noqa: E402
from holophyte.babysit.description_review import (  # noqa: E402
    description_findings,
)
from holophyte.loop.gates import MergeParked  # noqa: E402
from holophyte.loop.runs import open_store, set_phase  # noqa: E402
from holophyte.pr import github, pr_activity, pr_status  # noqa: E402
from holophyte.pr.github import PullRequest  # noqa: E402

TAGGED = '- Blocker (path: "PR description" -- it promises a handshake).'
CODE = "- Blocker: src/app.py:3 drops the tab target."
TAIL = ("\nCRITERION 1: met — tests/test_thing.py::test_it_works"
        "\nVERDICT: REQUEST_CHANGES")


class DescriptionFindingsTests(unittest.TestCase):
    def test_a_heading_above_a_tagged_blocker_is_not_a_finding(self):
        for heading in ("Findings:", "**Blockers**", "## Findings", "### Blockers:"):
            with self.subTest(heading=heading):
                self.assertEqual(description_findings(
                    f"{heading}\n{TAGGED}{TAIL}", "REQUEST_CHANGES", []), [TAGGED])
                self.assertEqual(description_findings(
                    f"{heading}\n\n{TAGGED}{TAIL}", "REQUEST_CHANGES", []), [TAGGED])

    def test_a_heading_does_not_hide_an_untagged_code_blocker(self):
        self.assertEqual(description_findings(
            f"Findings:\n{TAGGED}\n{CODE}{TAIL}", "REQUEST_CHANGES", []), [])

    def test_an_unlisted_code_blocker_after_a_tagged_item_is_not_hidden(self):
        for code in ("**P1 — load() crashes on empty input**",
                     "P1: load() crashes on empty input"):
            with self.subTest(code=code):
                self.assertEqual(description_findings(
                    f"{TAGGED}\n{code}{TAIL}", "REQUEST_CHANGES", []), [])

    def test_a_code_blocker_mentioning_the_tag_is_not_a_description_finding(self):
        mention = CODE[:-1] + ' (also noted under path: "PR description").'
        self.assertEqual(description_findings(
            f"{mention}{TAIL}", "REQUEST_CHANGES", []), [])

    def test_a_tag_after_a_bracketed_severity_is_a_description_finding(self):
        bracketed = ('- [P2] path: "PR description" — The description promises'
                     ' a handshake absent from the diff.')
        self.assertEqual(description_findings(
            f"{bracketed}{TAIL}", "REQUEST_CHANGES", []), [bracketed])

    def test_a_bracketed_severity_without_the_tag_is_a_code_finding(self):
        bracketed = '- [P2] path: "PR description" — it names a handshake.'
        self.assertEqual(description_findings(
            f"{bracketed}\n- [P2] src/app.py:3 drops the tab target.{TAIL}",
            "REQUEST_CHANGES", []), [])

    def test_a_code_path_beginning_with_the_tag_subject_is_a_code_finding(self):
        for code in ('- [P2] path: "PR description.py":3 — load() crashes.',
                     '- [P2] path: "PR descriptions/app.py":3 — it crashes.'):
            with self.subTest(code=code):
                self.assertEqual(description_findings(
                    f"{code}{TAIL}", "REQUEST_CHANGES", []), [])

    def test_ten_code_line_shapes_beside_a_tagged_finding_are_not_hidden(self):
        for code in ("  - Blocker: src/app.py:3 drops the tab target.",
                     "  Blocker: src/app.py:3 drops the tab target.",
                     "    src/app.py:3 drops the tab target.",
                     "    `src/app.py:3` drops the tab target.",
                     "    `src/app.py` drops the tab target.",
                     "    [app.py](src/app.py#L3) drops the tab target.",
                     "    **src/app.py:3** drops the tab target.",
                     '    path: "src/app.py" drops the tab target.',
                     '    "src/app.py" drops the tab target.',
                     "**P1 Empty input crashes the parser**"):
            with self.subTest(code=code):
                self.assertEqual(description_findings(
                    f"{TAGGED}\n{code}{TAIL}", "REQUEST_CHANGES", []), [])

    def test_a_backticked_or_bold_severity_tag_is_a_description_finding(self):
        for line in ('- `path: "PR description"` — the Changes table misstates'
                     ' the menu',
                     '- **[p2]** path: "PR description" — the Changes table'
                     ' misstates the menu'):
            with self.subTest(line=line):
                self.assertEqual(description_findings(
                    f"{line}{TAIL}", "REQUEST_CHANGES", []), [line])

    def test_a_bracketed_then_worded_severity_before_the_tag_is_one(self):
        for line in ('- [P1] Blocker: path: "PR description" — it names a menu.',
                     '- [P1] Blocker (path: "PR description" — it names a menu.)',
                     '- **[P2] Blocker** — path: "PR description" names a menu.'):
            with self.subTest(line=line):
                self.assertEqual(description_findings(
                    f"{line}{TAIL}", "REQUEST_CHANGES", []), [line])

    def test_a_backticked_or_bold_verdict_line_is_not_a_blocker(self):
        for verdict in ("**VERDICT: REQUEST_CHANGES**", "`VERDICT: REQUEST_CHANGES`"):
            with self.subTest(verdict=verdict):
                self.assertEqual(description_findings(
                    f"{TAGGED}\nCRITERION 1: met — tests/test_thing.py::test_it_works"
                    f"\n{verdict}", "REQUEST_CHANGES", []), [TAGGED])


DESCRIBED = '- **[p2]** path: "PR description" — the Changes table misstates the menu'
TABLE_FAULT = "the row menu drops the revert item"
LINKED = ('P2: path: "PR description" — it links [the guide](docs/guide.md:25) for'
          ' the Changes table')
TABLE_PATH = "console/src/app/components/TicketTable.tsx"
TABLE = ('- [P1] path: "console/src/app/components/TicketTable.tsx" — the row'
         ' menu drops the revert item')
PULL = PullRequest("example.test", "o", "n", 1, "https://example.test/pull/1")
TICKET = "Fix the row menu in `menu.txt`.\n"
CRITERION = "Given the table, then its row menu offers revert"
MET = "\nCRITERION 1: met — menu.txt offers revert"
APPROVE = f"{MET}\nVERDICT: APPROVE"
REJECT = f"{MET}\nVERDICT: REQUEST_CHANGES"


class MixedRoundTests(unittest.TestCase):
    """A real covering review round and PR-text rewrite on a real store and
    worktree; only the agents and GitHub are faked."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name, "repo")
        repo.mkdir()
        self.enterContext(patch.dict(
            os.environ, {"HOLOPHYTE_HOME": str(Path(tmp.name, "home"))}))
        self.project = holophyte.config.project.Project.locate(repo)
        self.root = holophyte.config.project.worktree_path(self.project, "task")
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test implementer"),
                     ("config", "user.email", "implementer@example.test"),
                     ("commit", "--allow-empty", "-qm", "base"),
                     ("worktree", "add", "-qb", "task", str(self.root))):
            self.git(*args, cwd=repo)
        self.head = self.commit("candidate")
        self.project.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.project.config_path.write_text(
            '[merge]\nmode = "pr"\napprove = "auto"\ncovering_fix_rounds = 1\n')
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1", repo)
        self.ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1",
            acceptance_criteria=[CRITERION], verification_commands=["true"],
            time_box_ms=60 * 60 * 1000)
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.claim()

    def claim(self):
        self.run_id = store.claim(self.conn, self.project_id, self.ticket)
        store.set_branch(self.conn, self.run_id, "task")
        set_phase(self.conn, self.run_id, "merge_gate")

    def git(self, *args, cwd=None):
        return subprocess.check_output(
            ["git", *args], cwd=cwd or self.root, text=True,
            stderr=subprocess.PIPE).strip()

    def commit(self, message):
        Path(self.root, "menu.txt").write_text(message)
        self.git("add", ".")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def review_fix(self, *rejections, refreshed=True):
        replies, self.goals, self.writes = [*rejections, APPROVE], [], []

        def reviewer(target, role, goal, *args, **kwargs):
            return replies.pop(0)

        def fixer(project, conn, run_id, beat_s, wt, budget_min, goal, *args,
                  **kwargs):
            self.goals.append(goal)
            self.commit(f"fix {len(self.goals)}")
            return "Restored the revert item.", False

        def writer(project, conn, run_id, beat_s, wt, budget_min, goal, *,
                   role, **kwargs):
            self.assertEqual(role, "write")
            self.writes.append(goal)
            return ("Restores the revert item." if refreshed else ""), False

        with (patch.object(holophyte.loop.review_round, "agent", reviewer),
              patch.object(holophyte.loop.implement, "_transport_timed", fixer),
              patch.object(holophyte.loop.implement, "_timed", writer),
              patch.object(github, "push_branch"),
              patch.object(github, "rest", return_value={"body": "Menu."}),
              patch.object(github, "edit_pr_body"),
              patch.object(pr_status, "pull_status",
                           side_effect=OSError("no GitHub in tests")),
              patch.object(babysitter, "_post")):
            babysitter._review_fix(
                self.project, self.conn, self.run_id, None, "KO-1", "task",
                self.root, self.head, None, 30, PULL, TICKET, "true", (),
                [CRITERION], fix_note=None, budget_min=10)

    def reviewer_findings(self, goal):
        findings = goal.partition("\n\nReviewer findings:\n")[2]
        return re.split(r"\n\n(?:Fix note from|Operator babysit note)", findings)[0]

    def fix_note(self, goal):
        return goal.partition("\n\nFix note from")[2]

    def fix_note_line(self, goal, path):
        (line,) = [line for line in self.fix_note(goal).splitlines()
                   if line.startswith(f"- Fix {path}")]
        return line

    def refreshed_reviews(self):
        return [goal.partition("the review says the description is wrong:\n\n")[2]
                .partition("\n\nRewrite the description")[0]
                for goal in self.writes]

    def answered(self, write):
        return write.partition("what this fix answered.")[2].partition(
            "\n\nthe review says the description is wrong")[0]

    def assert_split(self, described):
        self.review_fix(f"{TABLE}\n{described}{REJECT}")

        (goal,) = self.goals
        self.assertIn(TABLE, self.reviewer_findings(goal))
        self.assertIn(TABLE_FAULT, self.fix_note_line(goal, TABLE_PATH))
        self.assertNotIn("PR description", goal)
        self.assertNotIn("Changes table", goal)
        self.assertEqual(self.refreshed_reviews(), [described])

    def test_a_mixed_round_sends_the_description_finding_to_the_pr_text(self):
        self.assert_split(DESCRIBED)

        (write,) = self.writes
        self.assertEqual(self.answered(write).count("Changes table"), 0)
        self.assertEqual(pr_activity.latest(self.conn, self.run_id, "pr_text_sha"),
                         self.git("rev-parse", "HEAD"))

    def test_a_linked_description_finding_leaves_the_code_path_and_line(self):
        self.assert_split(LINKED)
        self.assertNotIn("docs/guide.md", self.goals[0])
        self.assertIn(f"- Fix {TABLE_PATH} \u2014", self.fix_note(self.goals[0]))

    def test_a_repeated_description_finding_does_not_make_a_round_a_repeat(self):
        self.project.config_path.write_text(
            '[merge]\nmode = "pr"\napprove = "auto"\ncovering_fix_rounds = 2\n')
        again = DESCRIBED.replace("Changes table", "Summary")
        menu = '- [P1] path: "holophyte/menu.py" — revert is unguarded'
        self.review_fix(f"{TABLE}\n{DESCRIBED}\n{again}{REJECT}",
                        f"{menu}\n{DESCRIBED}\n{again}{REJECT}")

        self.assertEqual(len(self.goals), 2)
        self.assertIn("holophyte/menu.py", self.reviewer_findings(self.goals[1]))

    def test_a_description_finding_ending_the_reply_leaves_no_fix_note_item(self):
        self.review_fix(f"{MET}\n{TABLE}\n{DESCRIBED}\nVERDICT: REQUEST_CHANGES")

        (goal,) = self.goals
        self.assertEqual(self.fix_note(goal).count("\n- Fix "), 1)
        self.assertIn(TABLE_FAULT, self.fix_note(goal))
        self.assertEqual(self.refreshed_reviews(), [DESCRIBED])

    def test_an_evidence_reply_is_not_turned_into_fix_note_items(self):
        unanswered = ('- [P1] path: "holophyte/old.py" — the old loop never exits'
                      f"\n{DESCRIBED}")
        self.review_fix(unanswered, f"{TABLE}\n{DESCRIBED}{REJECT}")

        (goal,) = self.goals
        self.assertIn(TABLE_FAULT, self.fix_note_line(goal, TABLE_PATH))
        self.assertNotIn("holophyte/old.py", self.fix_note(goal))

    def test_an_all_code_round_sends_every_blocker_to_the_implementer(self):
        code = f"{TABLE}\n- [P2] path: \"holophyte/menu.py\" — revert is unguarded"
        self.assertEqual(description_findings(
            f"{code}{REJECT}", "REQUEST_CHANGES", []), [])

        self.review_fix(f"{code}{REJECT}")

        (goal,) = self.goals
        self.assertIn(code, self.reviewer_findings(goal))
        self.assertIn(TABLE_FAULT, self.fix_note_line(goal, TABLE_PATH))
        self.assertIn("revert is unguarded",
                      self.fix_note_line(goal, "holophyte/menu.py"))
        self.assertEqual(self.refreshed_reviews(), [""])

    def phase(self):
        return store.read.run_snapshot(self.conn, self.run_id).phase

    def test_a_refused_rewrite_with_pending_findings_holds_the_approved_fix(self):
        with self.assertRaises(MergeParked) as parked:
            self.review_fix(f"{TABLE}\n{DESCRIBED}{REJECT}", refreshed=False)

        self.assertIn("refused to rewrite the description", str(parked.exception))
        self.assertIn(DESCRIBED[:40], str(parked.exception))
        self.assertEqual(self.phase(), "awaiting_merge_approval")
        self.assertEqual(self.refreshed_reviews(), [DESCRIBED])

    def test_a_refused_description_only_rewrite_parks_naming_every_finding_sent(self):
        again = DESCRIBED.replace("Changes table", "Summary")
        with self.assertRaises(MergeParked):
            self.review_fix(f"{TABLE}\n{DESCRIBED}{REJECT}", f"{again}{REJECT}",
                            refreshed=False)

        sent = f"{DESCRIBED}\n{again}"
        self.assertEqual(self.refreshed_reviews()[-1], sent)
        (park,) = [entry.text for entry in store.read.ledger(self.conn, self.run_id)
                   if entry.text.startswith("PR OPEN")]
        self.assertIn(sent, park)

    def test_a_refused_rewrite_with_nothing_pending_still_reaches_the_gate(self):
        self.review_fix(f"{TABLE}{REJECT}", refreshed=False)

        self.assertEqual(self.phase(), "merge_gate")


if __name__ == "__main__":
    unittest.main()

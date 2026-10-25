"""`holo scout page ID`: a done scout's report printed as Lotuspod markdown
from `GET /scouts/ID` over the local transport, its decisions table and its
fenced blocks shaped for Lotuspod's converter, and a scout not done refused.

Run: python3 -m unittest discover -s tests -p 'test_scout_page.py' -v
"""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.tickets
from holophyte.config.project import Project
from holophyte.holo.cli import main as holo_main
from store.scouts import (
    create_scout,
    end_scout,
    file_scout_action,
    move_scout,
    record_scout_actions,
    record_scout_report,
)

ASK = "Private tasks notify stakeholders on save"
COMMAND = "python3 -m unittest tests.test_notify"
OUTPUT = "FAIL: test_private_task_is_silent\n```\nnot a fence"
LINK = "https://github.com/acme/demo/issues/3"
VERIFY = ["```", "python3 -m unittest tests.test_notify", "```"]
DRAFT = "\n".join(["# Fix the save path", "", "## Verify command(s)", "",
                   *VERIFY])
DECISIONS = "## Decisions for the maintainer"
HEADER = "| # | Question | Context | Options |"


def finding(title, *evidence):
    return {"title": title, "status": "confirmed", "evidence": list(evidence)}


def report(summary, findings, reproduction=None):
    return {"summary": summary, "findings": findings, "actions": [],
            "reproduction": reproduction}


PASS_1 = report("Save notifies everyone.", [
    finding("The save hook notifies",
            {"file": "app/notify.py", "line": 88, "verified": True}),
    finding("The notify test fails",
            {"command": COMMAND, "output": OUTPUT})],
    reproduction={"level": "test", "hypotheses": [
        {"hypothesis": "privacy | ignored", "separates": "a private task",
         "verdict": "holds"}]})
PASS_2 = report("The tracker agrees.", [
    finding("Reported upstream", {"url": LINK})])
ITEMS = [
    {"title": "Fix | the save path", "why": "Private tasks leak.\nOn save.",
     "kind": "fix", "body": DRAFT},
    {"title": "Cover private saves", "why": "No test pins it.",
     "kind": "test", "body": "# Cover private saves"},
    {"title": "Revise the notify ticket", "why": "Its scope moved.",
     "kind": "revise", "body": "# Notify on save",
     "revises": {"ticket": "DEMO-7", "revision": 2}},
]


def table_lines(lines):
    start = lines.index(DECISIONS)
    rest = lines[start + 1:]
    while rest and not rest[0].startswith("|"):
        rest = rest[1:]
    end = next((i for i, line in enumerate(rest) if not line.startswith("|")),
               len(rest))
    return rest[:end]


def cells(line):
    return [part.strip() for part in line.strip().strip("|").split("|")]


def fenced_block(lines, opening_index):
    closing = next(i for i in range(opening_index + 1, len(lines))
                   if lines[i] == "```")
    return lines[opening_index + 1:closing]


class ScoutPageCase(unittest.TestCase):
    """A project whose store holds 13 queued scouts, then done bug scout 14
    with two passes and items A1-A3, done scout 15 whose items are all filed
    or `revise`, and scout 16 in `swarming`."""

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.repo = root / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.enterContext(patch.dict(os.environ, {
            "HOLOPHYTE_HOME": str(root / "home"), "HOLO_TRANSPORT": "local"}))
        project = Project.locate(self.repo)
        project.store_path.parent.mkdir(parents=True, exist_ok=True)
        conn = store.open(str(project.store_path))
        self.addCleanup(conn.close)
        store.init(conn)
        project_id = store.tickets.ensure_project(conn, "team-1", self.repo)
        for _ in range(13):
            create_scout(conn, project_id, "an earlier ask", "research", 30)
        self.bug = self.done(conn, project_id, "bug", [(1, PASS_1, ITEMS),
                                                       (2, PASS_2, [])])
        file_scout_action(conn, self.bug, 2, "DEMO-9")
        self.decided = self.done(conn, project_id, "recon",
                                 [(1, report("Mapped.", []), ITEMS[1:])])
        file_scout_action(conn, self.decided, 1, "DEMO-10")
        self.swarming = create_scout(conn, project_id, "still looking",
                                     "research", 30)
        move_scout(conn, self.swarming, "planning", base_sha="b" * 40)
        move_scout(conn, self.swarming, "swarming")

    def done(self, conn, project_id, kind, passes):
        scout_id = create_scout(conn, project_id, ASK, kind, 30)
        move_scout(conn, scout_id, "planning", base_sha="a" * 40)
        move_scout(conn, scout_id, "swarming")
        for number, written, items in passes:
            record_scout_report(conn, scout_id, number, written)
            record_scout_actions(conn, scout_id, number, items)
        end_scout(conn, scout_id, "done", "answered")
        return scout_id

    def page(self, scout_id):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = holo_main(["scout", "page", str(scout_id),
                                  "-p", str(self.repo)])
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def printed(self, scout_id):
        code, out, err = self.page(scout_id)
        self.assertEqual(code, 0, err)
        return out.splitlines()


class ScoutPageTests(ScoutPageCase):
    def test_the_page_numbers_findings_across_passes_with_their_evidence(self):
        self.assertEqual(self.bug, 14)
        lines = self.printed(self.bug)
        self.assertEqual(lines[0], f"# Scout 14: {ASK}")
        headings = [line for line in lines if line.startswith("### F")]
        self.assertEqual(len(headings), 3, headings)
        self.assertTrue(headings[2].startswith("### F3 "), headings)
        self.assertIn("Reported upstream", headings[2])
        self.assertIn("- `app/notify.py:88`", lines)
        command = lines.index(f"- `{COMMAND}`")
        opening = next(i for i in range(command + 1, len(lines))
                       if lines[i].startswith("```"))
        self.assertEqual(fenced_block(lines, opening),
                         ["FAIL: test_private_task_is_silent", " ```",
                          "not a fence"])

    def test_only_the_unfiled_item_that_is_no_revise_is_a_decision(self):
        lines = self.printed(self.bug)
        self.assertEqual(lines.count(DECISIONS), 1)
        table = table_lines(lines)
        self.assertEqual(table[0], HEADER)
        rows = [line for line in table[1:]
                if not set(line) <= set("|- ")]
        self.assertEqual(len(rows), 1, table)
        self.assertTrue(rows[0].startswith("| A1 |"), rows)
        self.assertTrue(rows[0].endswith("| File / Look further / Drop |"),
                        rows)
        text = "\n".join(lines)
        a2 = text.split("### A2 ", 1)[1].split("\n### ", 1)[0]
        a3 = text.split("### A3 ", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Filed as DEMO-9", a2)
        self.assertIn("Revises DEMO-7 at revision 2", a3)

    def test_a_pipe_or_line_break_in_a_cell_keeps_every_row_four_cells(self):
        table = table_lines(self.printed(self.bug))
        self.assertEqual([len(cells(line)) for line in table],
                         [4] * len(table))
        self.assertEqual(cells(table[-1])[1], "Fix / the save path")

    def test_a_fence_inside_a_draft_does_not_close_the_drafts_block(self):
        lines = self.printed(self.bug)
        heading = next(i for i, line in enumerate(lines)
                       if line.startswith("### A1 "))
        opening = lines.index("```markdown", heading)
        block = fenced_block(lines, opening)
        self.assertEqual([line for line in block if line.startswith("```")],
                         [])
        self.assertEqual(block[-3:], [" ```",
                                      "python3 -m unittest tests.test_notify",
                                      " ```"])

    def test_no_decisions_heading_when_every_item_is_filed_or_a_revise(self):
        lines = self.printed(self.decided)
        self.assertTrue(any(line.startswith("### A2 ") for line in lines))
        self.assertNotIn(DECISIONS, lines)

    def test_a_scout_not_done_is_refused_with_nothing_on_stdout(self):
        code, out, err = self.page(self.swarming)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err.strip(), f"[holo2] scout {self.swarming} is"
                         " swarming; only a done scout's report makes a page")


if __name__ == "__main__":
    unittest.main()

"""`--file-story SLUG` on a Linear board in store mode: the parent and each
child are created on the board first, the children as sub-issues of the
parent, then the store rows follow; a board failure part way writes nothing
to the store. The command line runs against a real store and a real git
repository under a throwaway home, the board answering as Linear does.

Run: python3 -m unittest discover -s tests -p 'test_story_linear_filing.py' -v
"""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from story_fixture import write_children, write_story  # noqa: E402

import holophyte.cli  # noqa: E402
import linear_provider  # noqa: E402
import provider  # noqa: E402

SLUG = "orders-csv"
STORE_LINEAR = ('[board]\nproject_id = "p-1"\nteam = "T"\nmode = "store"\n')
TICKETS = ("SELECT linearIdentifier, linearIssueId, status, boardColumn,"
           " dependsOn, parentTicketId FROM tickets ORDER BY id")


class RecordingBoard(provider.LinearBoard):
    """A Linear board whose writes are recorded and whose issues are served
    back through the real `fetch_task()` from what was filed."""

    def __init__(self, *args, fail_at=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls, self.issues, self.fail_at = [], {}, fail_at

    def file(self, title, body, estimate, state, priority=None, blockers=(),
             parent=None):
        self.calls.append(("file", title, state, parent, list(blockers)))
        if len(self.issues) + 1 == self.fail_at:
            raise RuntimeError("Linear is unreachable")
        number = len(self.issues) + 1
        blocking = [{"type": "blocks", "issue": {
            "id": self.issues[b]["id"], "state": {"type": "backlog"}}}
            for b in blockers]
        self.issues[f"REL-{number}"] = {
            "identifier": f"REL-{number}", "id": f"issue-{number}",
            "url": None, "title": title, "description": body,
            "estimate": estimate, "priority": priority, "createdAt": None,
            "updatedAt": None, "archivedAt": None,
            "state": {"name": state, "type": state.lower()},
            "labels": {"nodes": []},
            "inverseRelations": {"nodes": blocking}}
        return f"REL-{number}"

    def label_issue(self, issue_id, name):
        self.calls.append(("label", issue_id, name))
        self.issues[issue_id]["labels"]["nodes"].append({"name": name})

    def answer(self, query, variables=None):
        if query != linear_provider.ISSUE_QUERY:
            raise AssertionError("the filing asked Linear more than an issue")
        return {"issue": self.issues.get(variables["id"])}


class LinearFileStoryTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_story(self, config=STORE_LINEAR, fail_at=None):
        """File SLUG's children a, b and c (c depending on a and b) on a
        recording board; the exit status, printed lines and the board."""
        self.locate(config)
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        self.directory = write_story(self.project.holo_dir / "stories",
                                     name=SLUG)
        write_children(self.directory, [
            ("c", "completes W1", ["a", "b"], []),
            ("a", "scaffolding", [], []),
            ("b", "completes W2", [], []),
        ])
        self.before = self.files()
        boards = []

        def build(*args, **kwargs):
            boards.append(RecordingBoard(*args, fail_at=fail_at, **kwargs))
            return boards[-1]

        out = io.StringIO()
        with patch.object(provider, "LinearBoard", build), \
                patch.object(linear_provider, "_gql",
                             lambda *a: boards[0].answer(*a)), \
                contextlib.redirect_stdout(out):
            try:
                status = holophyte.cli.cli(
                    [str(self.target), "--file-story", SLUG]) or 0
            except SystemExit as exited:
                status = exited.code
        return status, out.getvalue().splitlines(), boards[0]

    def files(self):
        return {path.relative_to(self.directory): path.read_text()
                for path in sorted(self.directory.rglob("*.md"))}

    def store(self, query):
        with contextlib.closing(
                sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(query).fetchall()

    def test_the_story_is_a_parent_issue_with_sub_issues_and_blockers(self):
        status, lines, board = self.run_story()

        self.assertEqual(status, 0, lines)
        self.assertEqual(board.calls, [
            ("file", "Orders export as CSV", "Backlog", None, []),
            ("file", "Orders export step a", "Backlog", "issue-1", []),
            ("file", "Orders export step b", "Backlog", "issue-1", []),
            ("file", "Orders export step c", "Backlog", "issue-1",
             ["REL-2", "REL-3"]),
        ])
        rows = self.store(TICKETS)
        parent_id = self.store("SELECT id FROM tickets"
                               " WHERE linearIdentifier = 'REL-1'")[0][0]
        self.assertEqual(rows[0][:4],
                         ("REL-1", "issue-1", "needs_spec", "backlog"))
        self.assertEqual([(row[0], row[3], row[5]) for row in rows[1:]],
                         [(f"REL-{n}", "backlog", parent_id)
                          for n in (2, 3, 4)])
        self.assertEqual(json.loads(rows[3][4]), ["issue-2", "issue-3"])
        self.assertEqual(self.store("SELECT ticketId, state FROM stories"),
                         [(parent_id, "planned")])
        after = self.files()
        self.assertTrue(after[Path("story.md")].startswith("Story: REL-1\n"))
        self.assertTrue(after[Path("children/01-c.md")].startswith(
            "Ticket: REL-4\n"))

    def test_each_child_carries_the_board_label_and_the_parent_does_not(self):
        status, lines, board = self.run_story(STORE_LINEAR + 'label = "holo"\n')

        self.assertEqual(status, 0, lines)
        self.assertEqual([call for call in board.calls if call[0] == "label"],
                         [("label", f"REL-{n}", "holo") for n in (2, 3, 4)])

    def test_a_board_failure_part_way_names_what_it_created_and_stores_nothing(
            self):
        status, lines, board = self.run_story(fail_at=3)

        self.assertEqual(status, 1)
        self.assertEqual(list(board.issues), ["REL-1", "REL-2"])
        self.assertIn("Linear is unreachable", lines[0])
        self.assertIn("REL-1, REL-2", lines[-1])
        self.assertEqual(self.store("SELECT * FROM tickets"), [])
        for table in ("stories", "storyWitnesses", "storyChildren"):
            self.assertEqual(self.store(f"SELECT * FROM {table}"), [], table)
        self.assertEqual(self.files(), self.before)

    def test_a_linear_board_in_mirror_mode_is_refused_naming_store_mode(self):
        status, lines, board = self.run_story(
            STORE_LINEAR.replace('mode = "store"', 'mode = "mirror"'))

        self.assertEqual(status, 1)
        self.assertIn('mode = "store"', lines[0])
        self.assertEqual(board.calls, [])


class CreateIssueParentTests(unittest.TestCase):
    def create(self, **kwargs):
        """`create_issue()` against a transport that records each request;
        the `issueCreate` input it sent."""
        sent = []

        def transport(query, variables=None):
            sent.append(variables)
            if "teams(" in query:
                return {"teams": {"nodes": [{"id": "team-1"}]}}
            if "workflowStates(" in query:
                return {"workflowStates": {"nodes": [
                    {"id": "state-1", "name": "Backlog", "type": "backlog"}]}}
            return {"issueCreate": {"success": True, "issue": {
                "id": "issue-9", "identifier": "REL-9"}}}

        with patch.object(linear_provider, "_gql", transport):
            linear_provider.create_issue("p-1", "T", "Step", "body", 20,
                                         "Backlog", **kwargs)
        return sent[-1]["input"]

    def test_a_parent_is_sent_as_parent_id(self):
        self.assertEqual(self.create(parent="issue-1")["parentId"], "issue-1")

    def test_without_a_parent_no_parent_id_is_sent(self):
        self.assertNotIn("parentId", self.create())

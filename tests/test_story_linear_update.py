"""`--file-story SLUG --update KEY --revision N` on a Linear board in store
mode: each changed issue is written on the board first, then one store
transaction mirrors it and rewrites the story rows; a board failure part way
writes nothing to the store. The command line runs against a real store and
a real git repository under a throwaway home, the board answering as Linear
does.

Run: python3 -m unittest discover -s tests -p 'test_story_linear_update.py' -v
"""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from story_fixture import child_body, write_children, write_story  # noqa: E402
from test_story_linear_filing import (  # noqa: E402
    STORE_LINEAR,
    FakeLinear,
    RecordingBoard,
)

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
import provider  # noqa: E402
import store  # noqa: E402
import store.stories  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402

SLUG = "orders-csv"
LABELLED = STORE_LINEAR + 'label = "holo"\n'
STORY_TABLES = ("tickets", "stories", "storyWitnesses", "storyChildren",
                "ticketRevisions")


class UpdatingLinear(FakeLinear):
    """`FakeLinear` with the edits `LinearBoard.update()` makes."""

    def update_issue(self, identifier, title, body, estimate):
        issue = self.board.issues[identifier]
        issue.update(title=title, description=body, estimate=estimate)
        return issue["id"]

    def blockers_of(self, identifier):
        if identifier == self.board.shared.blockers_fail_for:
            raise RuntimeError("Linear refused the relations read")
        names = {issue["id"]: name for name, issue in self.board.issues.items()}
        return [names[relation["issue"]["id"]] for relation in
                self.board.issues[identifier]["inverseRelations"]["nodes"]
                if relation["type"] == "blocks"]

    def fetch_description(self, identifier):
        return self.board.issues[identifier]["description"] or ""


class UpdatingBoard(RecordingBoard):
    """A recording board whose issues and calls outlive one command, and
    whose `update()` calls are recorded too."""

    def __init__(self, *args, shared, **kwargs):
        super().__init__(*args, **kwargs)
        self._module = UpdatingLinear(self)
        self.shared = shared
        self.calls, self.issues = shared.calls, shared.issues

    def update(self, identifier, title, body, estimate, blockers=(), **kwargs):
        self.calls.append(("update", identifier, body, list(blockers)))
        if (sum(call[0] == "update" for call in self.calls)
                == self.shared.update_fails_at):
            raise RuntimeError("Linear is unreachable")
        return super().update(identifier, title, body, estimate,
                              blockers=blockers, **kwargs)


class LinearStoryUpdateTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls, self.issues, self.update_fails_at = [], {}, None
        self.blockers_fail_for = None
        board = UpdatingBoard("p-1", "T", shared=self)
        for patcher in (
                patch.object(provider, "LinearBoard",
                             lambda *a, **kw: UpdatingBoard(*a, shared=self,
                                                            **kw)),
                patch.object(linear_provider, "_gql", board.answer)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locate(LABELLED)
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        self.directory = write_story(self.project.holo_dir / "stories",
                                     name=SLUG)
        write_children(self.directory, [
            ("c", "completes W1", ["a", "b"], []),
            ("a", "scaffolding", [], []),
            ("b", "completes W2", [], []),
        ])
        status, lines = self.cli("--file-story", SLUG)
        self.assertEqual(status, 0, lines)
        self.revision = self.approve()
        self.calls.clear()

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                status = holophyte.cli.entry.cli([str(self.target), *args]) or 0
            except SystemExit as exited:
                status = exited.code
        return status, out.getvalue().splitlines()

    def update(self, revision=None):
        return self.cli("--file-story", SLUG, "--update", "REL-1",
                        "--revision",
                        str(self.revision if revision is None else revision))

    def store(self, query, *params):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(query, params).fetchall()

    def ticket(self, identifier):
        """(id, revision, dependsOn, body) of ticket `identifier`."""
        ((ticket_id, revision, depends, text),) = self.store(
            "SELECT id, revision, dependsOn, body FROM tickets"
            " WHERE linearIdentifier = ?", identifier)
        return ticket_id, revision, json.loads(depends), text

    def story_state(self):
        return self.store("SELECT state FROM stories")[0][0]

    def approve(self):
        parent_id, revision, _, _ = self.ticket("REL-1")
        with contextlib.closing(open_store(self.project)) as conn:
            store.stories.approve_story(conn, parent_id, revision, "maintainer",
                                        "the plan reads right")
        self.assertEqual(self.story_state(), "approved")
        return revision

    def child(self, name):
        return self.directory / "children" / f"{name}.md"

    def edit(self, path, old, new):
        self.assertIn(old, path.read_text())
        path.write_text(path.read_text().replace(old, new))

    def board_body(self, path, *renames):
        """The body the board should hold for child file `path`: its text
        below the header with each (slug line, identifier line) renamed."""
        text = path.read_text().partition("\n")[2]
        for old, new in renames:
            text = text.replace(old, new)
        return text

    def snapshot(self):
        rows = {table: self.store(f"SELECT * FROM {table}")
                for table in STORY_TABLES}
        files = {path: path.read_text()
                 for path in sorted(self.directory.rglob("*"))
                 if path.is_file()}
        return rows, files

    def test_an_implementation_note_edit_updates_the_issue_and_keeps_approval(
            self):
        path = self.child("01-c")
        self.edit(path, "- Keep the CSV header.", "- Quote every field.")
        body = self.board_body(path, ("Depends on: a, b",
                                      "Depends on: REL-2, REL-3"))
        child_revision = self.ticket("REL-4")[1] + 1

        status, lines = self.update()

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.calls,
                         [("update", "REL-4", body, ["REL-2", "REL-3"])])
        _, revision, _, stored = self.ticket("REL-4")
        self.assertEqual((revision, stored), (child_revision, body))
        self.assertIn(f"[holo2] updated REL-4 (revision {child_revision})",
                      lines)
        self.assertEqual(
            lines[-1], f"[holo2] story REL-1 is approved at revision "
                       f"{self.revision}")
        self.assertEqual(self.story_state(), "approved")

    def test_a_fixed_witness_file_reaches_the_story_rows_and_replans(self):
        path = self.directory / "witnesses" / "tests" / "test_story_w1.py"
        fixed = path.read_text().replace("[2, 1]", "[1, 2]")
        path.write_text(fixed)

        status, lines = self.update()

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.store("SELECT source FROM storyWitnesses"
                                    " WHERE key = 'W1'"), [(fixed,)])
        self.assertEqual(self.story_state(), "planned")
        self.assertEqual(self.ticket("REL-1")[1], self.revision + 1)
        self.assertEqual(self.calls, [])

    def test_a_story_body_edit_replaces_the_parent_issue_and_replans(self):
        path = self.directory / "story.md"
        self.edit(path, "Orders can be exported as CSV.",
                  "Every order can be exported as CSV.")
        body = path.read_text().partition("\n")[2]

        status, lines = self.update()

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.calls, [("update", "REL-1", body, [])])
        self.assertEqual(self.issues["REL-1"]["description"], body)
        _, revision, _, stored = self.ticket("REL-1")
        self.assertEqual(stored, body)
        self.assertEqual(self.store(
            "SELECT acceptanceCriteria, verificationCommands FROM tickets"
            " WHERE linearIdentifier = 'REL-1'"), [("[]", "[]")])
        self.assertEqual(self.story_state(), "planned")
        self.assertEqual(revision, self.revision + 1)

    def test_a_new_child_file_is_filed_as_a_labelled_blocked_sub_issue(self):
        path = self.child("04-e")
        path.write_text(child_body("e", "scaffolding", ["a"]))

        status, lines = self.update()

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.calls, [
            ("file", "Orders export step e", "Backlog", "issue-1", ["REL-2"]),
            ("label", "issue-5", "holo")])
        self.assertEqual(self.issues["REL-5"]["state"]["name"], "Backlog")
        self.assertEqual(
            [relation["issue"]["id"] for relation in
             self.issues["REL-5"]["inverseRelations"]["nodes"]], ["issue-2"])
        ticket_id, _, depends, _ = self.ticket("REL-5")
        parent_id = self.ticket("REL-1")[0]
        self.assertEqual(depends, ["issue-2"])
        self.assertEqual(self.store("SELECT storyId, role FROM storyChildren"
                                    " WHERE ticketId = ?", ticket_id),
                         [(parent_id, "scaffolding")])
        self.assertEqual(path.read_text(), "Ticket: REL-5\n"
                         + child_body("e", "scaffolding", ["a"]))
        self.assertIn("[holo2] filed REL-5: Orders export step e "
                      "(scaffolding, Backlog)", lines)

    def test_a_dropped_dependency_replans_and_names_the_relation_left(self):
        self.edit(self.child("01-c"), "Depends on: a, b", "Depends on: a")

        status, lines = self.update()

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.ticket("REL-4")[2], ["issue-2"])
        self.assertEqual(self.story_state(), "planned")
        self.assertEqual([line for line in lines if "still blocks" in line],
                         ["[holo2] REL-3 still blocks REL-4 on the board; "
                          "remove that relation on Linear"])

    def test_a_removed_child_file_is_refused_before_any_board_write(self):
        self.child("03-b").unlink()
        self.edit(self.child("02-a"), "- Keep the CSV header.",
                  "- Quote every field.")
        before = self.snapshot()

        status, lines = self.update()

        self.assertEqual(status, 1)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("REL-3", lines[0])
        self.assertIn("cancel the child on Linear and keep its file", lines[0])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_a_parent_revision_not_current_reaches_neither_board_nor_store(self):
        self.edit(self.child("02-a"), "- Keep the CSV header.",
                  "- Quote every field.")
        before = self.snapshot()

        status, lines = self.update(self.revision + 1)

        self.assertEqual(status, 1)
        self.assertEqual(lines, [f"[holo2] REL-1 is at revision "
                                 f"{self.revision}, not {self.revision + 1}; "
                                 "nothing changed"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)

    def test_a_board_failure_part_way_stores_nothing_and_a_rerun_converges(
            self):
        for name in ("02-a", "03-b"):
            self.edit(self.child(name), "- Keep the CSV header.",
                      "- Quote every field.")
        before = self.snapshot()
        self.update_fails_at = 2

        status, lines = self.update()

        self.assertEqual(status, 1)
        self.assertEqual([call[1] for call in self.calls], ["REL-2", "REL-3"])
        self.assertIn("Linear is unreachable", lines[0])
        self.assertIn("[holo2] already updated on the board: REL-2", lines)
        self.assertEqual(self.snapshot(), before)

        self.update_fails_at = None
        status, lines = self.update()

        self.assertEqual(status, 0, lines)
        for identifier, name in (("REL-2", "02-a"), ("REL-3", "03-b")):
            self.assertEqual(self.ticket(identifier)[3],
                             self.board_body(self.child(name)))
            self.assertIn("- Quote every field.", self.ticket(identifier)[3])

    def test_an_edit_whose_blocker_read_fails_is_named_as_updated(self):
        for name in ("02-a", "03-b"):
            self.edit(self.child(name), "- Keep the CSV header.",
                      "- Quote every field.")
        before = self.snapshot()
        self.blockers_fail_for = "REL-3"

        status, lines = self.update()

        self.assertEqual(status, 1)
        self.assertIn("Linear refused the relations read", lines[0])
        self.assertEqual(self.issues["REL-3"]["description"],
                         self.board_body(self.child("03-b")))
        self.assertIn("[holo2] already updated on the board: REL-2, REL-3",
                      lines)
        self.assertEqual(self.snapshot(), before)

    def test_a_linear_board_in_mirror_mode_is_refused_naming_store_mode(self):
        self.write_config(LABELLED.replace('mode = "store"', 'mode = "mirror"'))
        self.edit(self.child("02-a"), "- Keep the CSV header.",
                  "- Quote every field.")

        status, lines = self.update()

        self.assertEqual(status, 1)
        self.assertIn('mode = "store"', lines[0])
        self.assertEqual(self.calls, [])

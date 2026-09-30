"""`--file-story SLUG --update KEY-n --revision N` on a native board: a
redrafted story directory is applied to the filed story in one transaction,
a change to the plan returns an approved story to planned, and a refused
update writes nothing. The command line runs against a real store and a real
git repository under a throwaway home.

Run: python3 -m unittest discover -s tests -p 'test_cli_story_update.py' -v
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

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.stories  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from tests.test_cli_native_update import NATIVE, no_linear  # noqa: E402

SLUG = "orders-csv"
STORY_TABLES = ("stories", "storyWitnesses", "storyChildren",
                "ticketRevisions")


class StoryUpdateCliTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locate(NATIVE)
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)
        self.directory = write_story(self.project.holo_dir / "stories",
                                     name=SLUG)
        write_children(self.directory, [
            ("c", "completes W1", ["a", "b"], []),
            ("d", "completes W2", ["c"], []),
            ("a", "scaffolding", [], []),
            ("b", "scaffolding", [], []),
        ])
        status, lines = self.cli("--file-story", SLUG)
        self.assertEqual(status, 0, lines)

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                status = holophyte.cli.entry.cli([str(self.target), *args]) or 0
            except SystemExit as exited:
                status = exited.code
        return status, out.getvalue().splitlines()

    def store(self, query, *params):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(query, params).fetchall()

    def ticket(self, identifier):
        """(id, status, revision, dependsOn, body) of ticket `identifier`."""
        ((ticket_id, status, revision, depends, text),) = self.store(
            "SELECT id, status, revision, dependsOn, body FROM tickets"
            " WHERE linearIdentifier = ?", identifier)
        return ticket_id, status, revision, json.loads(depends), text

    def story_state(self):
        return self.store("SELECT state FROM stories")[0][0]

    def approve(self):
        """Approve the filed story at its parent's revision; answer it."""
        parent_id, _, revision, _, _ = self.ticket("NAT-1")
        with contextlib.closing(open_store(self.project)) as conn:
            store.stories.approve_story(conn, parent_id, revision, "maintainer",
                                        "the plan reads right")
        self.assertEqual(self.story_state(), "approved")
        return revision

    def child(self, name):
        return self.directory / "children" / f"{name}.md"

    def update(self, revision):
        return self.cli("--file-story", SLUG, "--update", "NAT-1",
                        "--revision", str(revision))

    def snapshot(self):
        """Every ticket, the story's rows and every file of the directory."""
        rows = {table: self.store(f"SELECT * FROM {table}")
                for table in STORY_TABLES}
        files = {path: path.read_text()
                 for path in sorted(self.directory.rglob("*.md"))}
        return self.store("SELECT * FROM tickets ORDER BY id"), rows, files

    def test_a_dropped_edge_returns_an_approved_story_to_planned(self):
        revision = self.approve()
        path = self.child("01-c")
        self.assertIn("Depends on: a, b", path.read_text())
        path.write_text(path.read_text().replace("Depends on: a, b",
                                                 "Depends on: a"))

        status, lines = self.update(revision)

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.ticket("NAT-4")[3], ["NAT-2"])
        self.assertEqual(self.story_state(), "planned")
        parent_id, parent_status, parent_revision, _, _ = self.ticket("NAT-1")
        self.assertEqual(parent_revision, revision + 1)
        self.assertEqual(parent_status, "needs_spec")
        with contextlib.closing(open_store(self.project)) as conn:
            self.assertFalse(store.pickable(conn, parent_id))

    def test_a_parent_body_change_records_one_revision_and_reports_it(self):
        revision = self.approve()
        path = self.directory / "story.md"
        path.write_text(path.read_text().replace(
            "Orders can be exported as CSV.",
            "Every order can be exported as CSV."))

        status, lines = self.update(revision)

        self.assertEqual(status, 0, lines)
        _, _, parent_revision, _, text = self.ticket("NAT-1")
        self.assertIn("Every order can be exported as CSV.", text)
        self.assertEqual(parent_revision, revision + 1)
        self.assertIn(f"revision {revision + 1}", lines[-1])
        self.assertEqual(self.story_state(), "planned")
        self.assertEqual(self.update(parent_revision)[0], 0)

    def test_a_plan_change_in_the_same_millisecond_still_moves_the_revision(self):
        revision = self.approve()
        story = self.directory / "story.md"
        story.write_text(story.read_text().replace(
            "Orders can be exported as CSV.",
            "Every order can be exported as CSV."))
        path = self.child("01-c")
        with patch("time.time", return_value=1_700_000_000.0):
            self.assertEqual(self.update(revision)[0], 0)
            path.write_text(path.read_text().replace("Depends on: a, b",
                                                     "Depends on: a"))
            status, lines = self.update(revision + 1)

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.ticket("NAT-4")[3], ["NAT-2"])
        self.assertEqual(self.ticket("NAT-1")[2], revision + 2)
        self.assertEqual(self.update(revision + 1)[0], 1)

    def test_a_new_child_file_is_filed_with_its_header(self):
        revision = self.approve()
        path = self.child("05-e")
        path.write_text(child_body("e", "advances W2"))
        completer = self.child("02-d")
        completer.write_text(completer.read_text().replace(
            "Depends on: c", "Depends on: c, e"))

        status, lines = self.update(revision)

        self.assertEqual(status, 0, lines)
        ticket_id, _, _, _, text = self.ticket("NAT-6")
        self.assertEqual(text.splitlines()[0], "# Orders export step e")
        self.assertEqual(self.store("SELECT boardColumn FROM tickets"
                                    " WHERE id = ?", ticket_id), [("backlog",)])
        self.assertEqual(path.read_text(),
                         "Ticket: NAT-6\n" + child_body("e", "advances W2"))
        self.assertEqual(self.store("SELECT witnessKey, role FROM storyChildren"
                                    " WHERE ticketId = ?", ticket_id),
                         [("W2", "advances")])
        self.assertEqual(self.story_state(), "planned")

    def test_a_change_to_implementation_notes_alone_keeps_the_approval(self):
        revision = self.approve()
        _, _, child_revision, _, _ = self.ticket("NAT-3")
        path = self.child("04-b")
        path.write_text(path.read_text().replace(
            "- Keep the CSV header.", "- Quote every field."))

        status, lines = self.update(revision)

        self.assertEqual(status, 0, lines)
        _, _, new_revision, _, text = self.ticket("NAT-3")
        self.assertEqual(new_revision, child_revision + 1)
        self.assertIn("- Quote every field.", text)
        self.assertNotIn("Keep the CSV header", text)
        self.assertEqual(self.story_state(), "approved")
        self.assertEqual(self.ticket("NAT-1")[2], revision)

    def test_a_stale_parent_revision_changes_nothing(self):
        revision = self.approve()
        path = self.child("01-c")
        path.write_text(path.read_text().replace("Depends on: a, b",
                                                 "Depends on: a"))
        before = self.snapshot()

        status, lines = self.update(revision - 1)

        self.assertEqual(status, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn(f"revision {revision}, not {revision - 1}", lines[0])
        self.assertEqual(self.snapshot(), before)

    def test_a_removed_child_file_is_refused_and_changes_nothing(self):
        revision = self.approve()
        self.child("02-d").unlink()
        path = self.child("04-b")
        path.write_text(path.read_text().replace(
            "- Keep the CSV header.", "- Quote every field."))
        before = self.snapshot()

        status, lines = self.update(revision)

        self.assertEqual(status, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("NAT-5", lines[0])
        self.assertIn("--cancel", lines[0])
        self.assertEqual(self.snapshot(), before)

    def test_a_cycle_through_a_sibling_ticket_id_is_refused(self):
        revision = self.approve()
        path = self.child("03-a")
        path.write_text(path.read_text().replace("Depends on: none",
                                                 "Depends on: NAT-4"))
        before = self.snapshot()

        status, lines = self.update(revision)

        self.assertEqual(status, 1, lines)
        self.assertIn("cycle", " ".join(lines))
        self.assertEqual(self.snapshot(), before)

    def test_a_refused_witness_file_is_named_in_the_story_directory(self):
        revision = self.approve()
        (self.directory / "witnesses" / "tests" / "test_story_w2.py").unlink()

        status, lines = self.update(revision)

        self.assertEqual(status, 1, lines)
        self.assertIn(f"missing from {self.directory / 'witnesses'} in W2",
                      " ".join(lines))

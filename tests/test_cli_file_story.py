"""`--file-story SLUG` on a native board: a valid story directory is filed
as its parent, its children in dependency order and its story rows in one
transaction, and a refused one writes nothing. The command line runs against
a real store and a real git repository under a throwaway home.

Run: python3 -m unittest discover -s tests -p 'test_cli_file_story.py' -v
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
from story_fixture import write_children, write_story  # noqa: E402

import holophyte.cli  # noqa: E402
import linear_provider  # noqa: E402
from tests.test_cli_native_update import NATIVE, body, no_linear  # noqa: E402

SLUG = "orders-csv"
TICKETS = ("SELECT linearIdentifier, status, boardColumn, priority, dependsOn,"
           " parentTicketId, body FROM tickets ORDER BY id")
STORY_TABLES = ("stories", "storyWitnesses", "storyChildren")


class FileStoryCliTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locate(NATIVE)
        subprocess.run(["git", "init", "-q", str(self.target)], check=True)

    def story(self, depends_a=()):
        """The story SLUG: witnesses W1 and W2, children written in an order
        their dependencies do not follow."""
        directory = write_story(self.project.holo_dir / "stories", name=SLUG)
        write_children(directory, [
            ("c", "completes W1", ["a", "b"], []),
            ("d", "completes W2", ["c"], []),
            ("a", "scaffolding", list(depends_a), []),
            ("b", "advances W1", [], []),
        ])
        return directory

    def files(self, directory):
        return {path.relative_to(directory): path.read_text()
                for path in sorted(directory.rglob("*.md"))}

    def cli(self, *args):
        """Run the command line on the project; its exit status and the
        lines it printed."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                status = holophyte.cli.cli([str(self.target), *args]) or 0
            except SystemExit as exited:
                status = exited.code
        return status, out.getvalue().splitlines()

    def store(self, query):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(query).fetchall()

    def state(self):
        """Every ticket, story row and the ticket sequence."""
        rows = {table: self.store(f"SELECT * FROM {table}")
                for table in STORY_TABLES}
        return (self.store(TICKETS), rows,
                self.store("SELECT ticketSeq FROM projects"))

    def file_one_ticket(self):
        """NAT-1, an ordinary ready ticket filed before the story."""
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text("")
        ticket = self.root / "T1.md"
        ticket.write_text(body("First"))
        self.assertEqual(self.cli("--file-ticket", str(ticket))[0], 0)

    def test_a_valid_story_files_its_parent_children_and_rows(self):
        directory = self.story()
        before = self.files(directory)

        status, lines = self.cli("--file-story", SLUG, "--priority", "high")

        self.assertEqual(status, 0)
        self.assertEqual([line.split(":")[0] for line in lines],
                         [f"[holo2] filed NAT-{n}" for n in range(1, 6)])
        rows = self.store(TICKETS)
        parent = rows[0]
        self.assertEqual(parent[:3], ("NAT-1", "needs_spec", "backlog"))
        self.assertEqual(parent[6], before[Path("story.md")])
        children = {row[6].splitlines()[0]: row for row in rows[1:]}
        self.assertEqual(
            {title: row[0] for title, row in children.items()},
            {f"# Orders export step {slug}": f"NAT-{n}"
             for n, slug in enumerate("abcd", 2)})
        (parent_id,) = self.store("SELECT id FROM tickets"
                                  " WHERE linearIdentifier = 'NAT-1'")[0]
        for identifier, _status, column, priority, _deps, parent_ticket, _ \
                in rows[1:]:
            self.assertEqual((column, priority, parent_ticket),
                             ("backlog", 2, parent_id), identifier)
        depends = {row[0]: (json.loads(row[4]), row[6]) for row in rows}
        self.assertEqual(depends["NAT-4"][0], ["NAT-2", "NAT-3"])
        self.assertIn("Depends on: NAT-2, NAT-3", depends["NAT-4"][1])
        self.assertEqual(depends["NAT-5"][0], ["NAT-4"])
        self.assertIn("Depends on: NAT-4", depends["NAT-5"][1])
        self.assertEqual(self.store("SELECT ticketId, state FROM stories"),
                         [(parent_id, "planned")])
        self.assertEqual(
            self.store("SELECT key, file FROM storyWitnesses ORDER BY key"),
            [("W1", "tests/test_story_w1.py"), ("W2", "tests/test_story_w2.py")])
        self.assertEqual(len(self.store("SELECT * FROM storyChildren")), 4)
        after = self.files(directory)
        self.assertEqual(after[Path("story.md")],
                         "Story: NAT-1\n" + before[Path("story.md")])
        for name, identifier in (("01-c", "NAT-4"), ("02-d", "NAT-5"),
                                 ("03-a", "NAT-2"), ("04-b", "NAT-3")):
            path = Path("children") / f"{name}.md"
            self.assertEqual(after[path],
                             f"Ticket: {identifier}\n" + before[path])
            self.assertNotIn("Ticket:", children[before[path].splitlines()[0]][6])

    def test_a_story_the_validator_refuses_writes_nothing(self):
        self.file_one_ticket()
        directory = self.story()
        story = directory / "story.md"
        story.write_text(story.read_text().replace(
            "W2: python3 -m unittest tests.test_story_w2\n", ""))
        before, state = self.files(directory), self.state()

        status, lines = self.cli("--file-story", SLUG)

        self.assertEqual(status, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("witness W2 has no line in 'Witness commands'", lines[0])
        self.assertEqual(self.files(directory), before)
        self.assertEqual(self.state(), state)

    def test_an_unmerged_real_dependency_refuses_the_whole_story(self):
        self.file_one_ticket()
        directory = self.story(depends_a=["NAT-1"])
        before, state = self.files(directory), self.state()

        status, lines = self.cli("--file-story", SLUG)

        self.assertEqual(status, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("NAT-1", lines[0])
        self.assertEqual(self.files(directory), before)
        self.assertEqual(self.state(), state)
        self.assertEqual([row[0] for row in state[0]], ["NAT-1"])

    def test_a_story_already_filed_points_to_update_and_writes_nothing(self):
        self.file_one_ticket()
        directory = self.story()
        story = directory / "story.md"
        story.write_text("Story: NAT-1\n" + story.read_text())
        before, state = self.files(directory), self.state()

        status, lines = self.cli("--file-story", SLUG)

        self.assertEqual(status, 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("NAT-1", lines[0])
        self.assertIn("--update", lines[0])
        self.assertEqual(self.files(directory), before)
        self.assertEqual(self.state(), state)

    def test_a_sibling_slug_shaped_like_a_ticket_id_is_a_sibling(self):
        directory = write_story(self.project.holo_dir / "stories", name=SLUG)
        write_children(directory, [
            ("step-1", "scaffolding", [], []),
            ("all", "completes W1, W2", ["step-1"], []),
        ])

        status, lines = self.cli("--file-story", SLUG)

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.store("SELECT linearIdentifier, dependsOn"
                                    " FROM tickets ORDER BY id")[1:],
                         [("NAT-2", "[]"), ("NAT-3", '["NAT-2"]')])

    def test_a_linked_sibling_dependency_is_resolved(self):
        directory = self.story()
        child = directory / "children" / "01-c.md"
        child.write_text(child.read_text().replace(
            "Depends on: a, b", "Depends on: [a](03-a.md), b"))

        status, lines = self.cli("--file-story", SLUG)

        self.assertEqual(status, 0, lines)
        (row,) = self.store("SELECT dependsOn, body FROM tickets"
                            " WHERE linearIdentifier = 'NAT-4'")
        self.assertEqual(json.loads(row[0]), ["NAT-2", "NAT-3"])
        self.assertIn("Depends on: NAT-2, NAT-3", row[1])

    def test_an_estimate_line_outside_its_section_is_left_alone(self):
        directory = self.story()
        child = directory / "children" / "01-c.md"
        example = "```\nEstimate: 20 min · Depends on: none\n```"
        child.write_text(child.read_text().replace(
            "- Keep the CSV header.", f"- Keep the CSV header.\n\n{example}"))

        status, lines = self.cli("--file-story", SLUG)

        self.assertEqual(status, 0, lines)
        (row,) = self.store("SELECT dependsOn, body FROM tickets"
                            " WHERE linearIdentifier = 'NAT-4'")
        self.assertEqual(json.loads(row[0]), ["NAT-2", "NAT-3"])
        self.assertIn(example, row[1])

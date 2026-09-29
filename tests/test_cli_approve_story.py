"""`--approve-story KEY-n --revision N --note TEXT`: the baseline runs every
witness at main's tip, a red-by-assertion baseline freezes the plan and
releases the children, and a refused approval leaves the story planned. The
command line runs against a real store and a real git repository under a
throwaway home.

Run: python3 -m unittest discover -s tests -p 'test_cli_approve_story.py' -v
"""
import contextlib
import io
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from story_fixture import witness_path, write_children, write_story  # noqa: E402

import holophyte.cli  # noqa: E402
import linear_provider  # noqa: E402
import provider  # noqa: E402
from tests.test_cli_native_update import NATIVE, no_linear  # noqa: E402
from tests.test_story_linear_filing import STORE_LINEAR, RecordingBoard  # noqa: E402

SLUG = "orders-csv"
FAILS_AN_ASSERTION = """import unittest


class Witness{n}Tests(unittest.TestCase):
    def test_outcome_{n}_holds(self):
        self.assertEqual(sorted([2, 1]), [2, 1])
"""
IMPORTS_A_MISSING_MODULE = """import unittest

import orders_export_that_does_not_exist


class Witness{n}Tests(unittest.TestCase):
    def test_outcome_{n}_holds(self):
        orders_export_that_does_not_exist.export()
"""
LEDGER = ("SELECT witnessKey, mainSha, verdict, redKind, verifier"
          " FROM witnessResults ORDER BY id")
APPROVALS = "SELECT note FROM interventions WHERE action = 'approve_story'"


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class ApproveStoryFixture(ConfigTestCase):
    def repository(self, config):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.locate(config)
        git(self.target, "init", "-q", "-b", "main")
        (self.target / "README.md").write_text("orders\n")
        git(self.target, "add", "README.md")
        git(self.target, "commit", "-q", "-m", "initial")
        self.tip = git(self.target, "rev-parse", "main")

    def write(self, slug, sources):
        """Story `slug` whose witness n has source `sources[n - 1]` (None
        keeps the passing one), a scaffolding child and one completing
        every witness."""
        directory = write_story(self.project.holo_dir / "stories", name=slug)
        write_children(directory, [
            ("setup", "scaffolding", [], []),
            ("all", "completes W1, W2", ["setup"], []),
        ])
        for n, source in enumerate(sources, 1):
            if source is not None:
                (directory / "witnesses" / witness_path(n)).write_text(
                    source.format(n=n))

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                status = holophyte.cli.cli([str(self.target), *args]) or 0
            except SystemExit as exited:
                status = exited.code
        return status, out.getvalue().splitlines()

    def store(self, query, params=()):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(query, params).fetchall()

    def revision(self, identifier):
        return self.store("SELECT revision FROM tickets"
                          " WHERE linearIdentifier = ?", (identifier,))[0][0]

    def children(self, parent):
        return self.store(
            "SELECT linearIdentifier, boardColumn, pushState FROM tickets"
            " WHERE parentTicketId = (SELECT id FROM tickets"
            " WHERE linearIdentifier = ?) ORDER BY id", (parent,))

    def story_state(self, parent):
        return self.store("SELECT s.state, s.approvedPlan IS NOT NULL"
                          " FROM stories s JOIN tickets t ON t.id = s.ticketId"
                          " WHERE t.linearIdentifier = ?", (parent,))[0]


class NativeApproveStoryTests(ApproveStoryFixture):
    def setUp(self):
        patcher = patch.object(linear_provider, "_gql", no_linear)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repository(NATIVE)

    def filed(self, slug, sources):
        self.write(slug, sources)
        status, lines = self.cli("--file-story", slug)
        self.assertEqual(status, 0, lines)
        return lines[0].split()[2].rstrip(":")

    def approve(self, parent, *args, revision=None):
        return self.cli("--approve-story", parent, "--revision",
                        str(self.revision(parent) if revision is None
                            else revision), "--note", "plan ok", *args)

    def test_a_red_baseline_approves_freezes_and_releases_every_child(self):
        parent = self.filed(SLUG, [FAILS_AN_ASSERTION] * 2)

        status, lines = self.approve(parent)

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.store(LEDGER), [
            ("W1", self.tip, "red", "assert", "baseline"),
            ("W2", self.tip, "red", "assert", "baseline")])
        self.assertEqual(self.story_state(parent), ("approved", 1))
        self.assertEqual(self.store(APPROVALS), [("plan ok",)])
        self.assertEqual([row[:2] for row in self.children(parent)],
                         [("NAT-2", "ready"), ("NAT-3", "ready")])

    def test_a_green_witness_refuses_until_baseline_green_names_it(self):
        parent = self.filed(SLUG, [FAILS_AN_ASSERTION, None])

        status, lines = self.approve(parent)

        self.assertEqual(status, 1)
        self.assertIn("W2 is green", "\n".join(lines))
        self.assertEqual(self.story_state(parent), ("planned", 0))
        self.assertEqual({row[1] for row in self.children(parent)},
                         {"backlog"})
        self.assertEqual([row[2] for row in self.store(LEDGER)],
                         ["red", "green"])

        status, lines = self.approve(parent, "--baseline-green", "W2")

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.story_state(parent), ("approved", 1))
        (note,) = self.store(APPROVALS)
        self.assertIn("W2 green", note[0])
        self.assertEqual(len(self.store(LEDGER)), 4)

    def test_a_red_by_exception_refuses_until_its_override_names_it(self):
        parent = self.filed(SLUG, [FAILS_AN_ASSERTION,
                                   IMPORTS_A_MISSING_MODULE])

        status, lines = self.approve(parent)

        self.assertEqual(status, 1)
        self.assertIn("W2 is red by exception", "\n".join(lines))
        self.assertEqual(self.story_state(parent), ("planned", 0))
        self.assertEqual(self.store(LEDGER)[1][2:4], ("red", "exception"))

        status, lines = self.approve(
            parent, "--baseline-red-kind", "exception", "W2")

        self.assertEqual(status, 0, lines)
        (note,) = self.store(APPROVALS)
        self.assertIn("W2 red by exception", note[0])

    def test_another_approved_story_refuses_and_writes_nothing(self):
        first = self.filed("first", [FAILS_AN_ASSERTION] * 2)
        self.assertEqual(self.approve(first)[0], 0)
        second = self.filed(SLUG, [FAILS_AN_ASSERTION] * 2)
        before = (self.store(LEDGER), self.store(APPROVALS),
                  self.children(second))

        status, lines = self.approve(second)

        self.assertEqual(status, 1)
        self.assertIn(f"story {first}", lines[0])
        self.assertEqual((self.store(LEDGER), self.store(APPROVALS),
                          self.children(second)), before)
        self.assertEqual(self.story_state(second), ("planned", 0))

    def test_a_stale_revision_refuses_and_writes_nothing(self):
        parent = self.filed(SLUG, [FAILS_AN_ASSERTION] * 2)
        current = self.revision(parent)

        status, lines = self.approve(parent, revision=current + 1)

        self.assertEqual(status, 1)
        self.assertIn(f"revision {current}, not {current + 1}", lines[0])
        self.assertEqual(self.store(LEDGER), [])
        self.assertEqual(self.store(APPROVALS), [])
        self.assertEqual({row[1] for row in self.children(parent)},
                         {"backlog"})

    def test_an_override_naming_no_witness_of_the_story_exits_1(self):
        parent = self.filed(SLUG, [FAILS_AN_ASSERTION] * 2)

        status, lines = self.approve(parent, "--baseline-green", "W9")

        self.assertEqual(status, 1)
        self.assertIn("no witness W9", lines[0])
        self.assertEqual(self.store(LEDGER), [])

    def test_a_red_kind_other_than_exception_is_a_usage_error(self):
        with contextlib.redirect_stderr(io.StringIO()):
            status, _ = self.cli("--approve-story", "NAT-1", "--revision",
                                 "1", "--note", "ok", "--baseline-red-kind",
                                 "assert", "W1")

        self.assertEqual(status, 2)


class LinearApproveStoryTests(ApproveStoryFixture):
    def setUp(self):
        self.repository(STORE_LINEAR)

    def test_each_child_gets_a_queued_todo_push_and_no_board_call(self):
        self.write(SLUG, [FAILS_AN_ASSERTION] * 2)
        boards, asked = [], []

        def build(*args, **kwargs):
            boards.append(RecordingBoard(*args, **kwargs))
            return boards[-1]

        def gql(*args):
            asked.append(args)
            return boards[0].answer(*args)

        with patch.object(provider, "LinearBoard", build), \
                patch.object(linear_provider, "_gql", gql):
            status, lines = self.cli("--file-story", SLUG)
            self.assertEqual(status, 0, lines)
            filing = (list(boards[0].calls), len(asked))
            status, lines = self.cli(
                "--approve-story", "REL-1", "--revision",
                str(self.revision("REL-1")), "--note", "plan ok")

        self.assertEqual(status, 0, lines)
        self.assertEqual(self.story_state("REL-1"), ("approved", 1))
        self.assertEqual(self.children("REL-1"), [
            ("REL-2", "backlog", "Todo"), ("REL-3", "backlog", "Todo")])
        self.assertEqual(boards[-1].calls, [])
        self.assertEqual((list(boards[0].calls), len(asked)), filing)

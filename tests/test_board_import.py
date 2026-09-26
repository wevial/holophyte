"""`--board-import`: every open Linear issue copied into the store by board
id, restartable and with a dry run (KO-756).

Run: python3 -m unittest discover -s tests -p 'test_board_import.py' -v
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import holophyte.cli  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board import mirror_task  # noqa: E402
from holophyte.board_import import board_import  # noqa: E402

TEAM = "team-1"
CRITERIA = ["Given the ticket, then it is worked"]


def issue(identifier, column, blocked_by=()):
    """An `open_issues()` answer for one issue, in KO-751's shape."""
    return {"id": identifier, "issue_id": f"uuid-{identifier}",
            "title": f"ticket {identifier}", "criteria": CRITERIA,
            "verify": "echo ok", "budget_min": 25, "column": column,
            "blocked_by": list(blocked_by)}


class Board:
    """A Linear board answering `open_issues()` and nothing else."""

    team = TEAM

    def __init__(self, issues):
        self.issues = issues

    def open_issues(self):
        return [dict(i) for i in self.issues]


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class BoardImportTests(unittest.TestCase):
    """A Linear project's store holding KO-1 with its run 3, a ledger row
    and `dependsOn`, and a board answering KO-1 and a Backlog KO-2."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.target = self.locate("repo")
        conn = store.open(str(self.target.store_path))
        try:
            self.project_id = store.tickets.ensure_project(
                conn, TEAM, self.target.path)
            self.ticket = mirror_task(conn, self.project_id,
                                      issue("KO-1", "ready"))
            for attempt in range(3):
                store.tickets.transition(conn, self.ticket, "in_flight")
                self.run = store.claim(conn, self.project_id, self.ticket,
                                       now=1000 + attempt)
                store.release(conn, self.run, "failed", "verify failed",
                              now=2000 + attempt)
                store.tickets.walk_ticket(conn, self.ticket, "ready")
            store.record_ledger(conn, self.run, "note", "run 3 was here")
            mirror_task(conn, self.project_id, issue("KO-1", "ready"),
                        depends_on=["uuid-KO-9"])
        finally:
            conn.close()
        self.board = Board([issue("KO-1", "ready"),
                            issue("KO-2", "backlog", ["uuid-KO-1"])])

    def locate(self, name):
        repo = self.root / name
        repo.mkdir()
        target = holophyte.project.Project.locate(repo)
        target.store_path.parent.mkdir(parents=True, exist_ok=True)
        return target

    def run_import(self, target, dry_run=False):
        out = io.StringIO()
        self.assertEqual(board_import(target, self.board, dry_run=dry_run,
                                      out=out), 0)
        return out.getvalue().splitlines()

    def rows(self, target):
        conn = store.read.open_readonly(target.store_path)
        try:
            return {r[1]: r for r in conn.execute(
                "SELECT id, linearIdentifier, linearIssueId, lastRunId,"
                " dependsOn, boardColumn FROM tickets ORDER BY id")}, \
                conn.execute("SELECT runId, ticketId, text FROM ledger"
                             ).fetchall()
        finally:
            conn.close()

    def summary(self, lines):
        return [line for line in lines
                if line.startswith("[holo2] board import:")]

    def test_an_import_keeps_the_held_row_and_adds_the_backlog_one(self):
        self.assertEqual(self.run, 3)

        lines = self.run_import(self.target)

        self.assertIn("[holo2] KO-1: unchanged", lines)
        self.assertIn("[holo2] KO-2: new", lines)
        self.assertEqual(self.summary(lines), [
            "[holo2] board import: 1 new, 0 changed, 1 unchanged; "
            "0 pushes and 0 notes pending for Linear"])
        rows, ledger = self.rows(self.target)
        self.assertEqual(rows["KO-1"], (self.ticket, "KO-1", "uuid-KO-1", 3,
                                        '["uuid-KO-9"]', "ready"))
        self.assertEqual(ledger, [(3, self.ticket, "run 3 was here")])
        self.assertEqual(rows["KO-2"][2:], ("uuid-KO-2", None,
                                            '["uuid-KO-1"]', "backlog"))

        again = self.run_import(self.target)

        self.assertEqual(self.summary(again), [
            "[holo2] board import: 0 new, 0 changed, 2 unchanged; "
            "0 pushes and 0 notes pending for Linear"])
        self.assertEqual(self.rows(self.target), (rows, ledger))

    def test_a_dry_run_prints_the_same_summary_and_writes_nothing(self):
        copy = self.locate("copy")
        shutil.copyfile(self.target.store_path, copy.store_path)
        before = sha256(copy.store_path)

        dry = self.run_import(copy, dry_run=True)

        self.assertEqual(sha256(copy.store_path), before)
        self.assertEqual(self.summary(dry),
                         self.summary(self.run_import(self.target)))
        self.assertIn("[holo2] KO-2: new", dry)

    def test_a_changed_issue_is_counted_changed(self):
        self.board.issues[0]["title"] = "ticket KO-1, retitled"

        lines = self.run_import(self.target)

        self.assertIn("[holo2] KO-1: changed", lines)
        self.assertEqual(self.summary(lines), [
            "[holo2] board import: 1 new, 1 changed, 0 unchanged; "
            "0 pushes and 0 notes pending for Linear"])


class NativeRefusalTests(unittest.TestCase):
    def test_a_native_project_is_refused_before_linear_is_asked(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        repo = root / "repo"
        repo.mkdir()
        target = holophyte.project.Project.locate(repo)
        target.config_path.parent.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text('[board]\nkind = "native"\nkey = "NAT"\n')

        def asked(*args, **kwargs):
            raise AssertionError("Linear was asked")

        with patch.object(linear_provider, "_gql", asked), \
                contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            holophyte.cli.cli(["--board-import", str(repo)])

        self.assertIsInstance(raised.exception.code, str)  # exits 1
        self.assertIn("[board] kind", raised.exception.code)


if __name__ == "__main__":
    unittest.main()

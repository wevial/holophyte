"""KO-595: `--import-store PATH --dry-run` says what an import would move."""
import contextlib
import hashlib
import io
import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import holophyte.project
import store
import store.read
import store.tickets
from holophyte import store_import
from tests.phase_fixture import finish_run

AT = 1_700_000_000_000


def seed(path, runs):
    """A store at `path` holding `runs` finished runs, and its connection."""
    conn = store.open(str(path), migrate="owner")
    project = store.tickets.ensure_project(conn, "team-1", "/repos/one")
    for n in range(1, runs + 1):
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}")
        run_id = store.claim(conn, project, ticket, now=AT + n)
        finish_run(conn, run_id, "merged", now=AT + n + 1)
    return conn


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree(root):
    """Every file under `root`, by relative path, with its hash."""
    return {str(p.relative_to(root)): digest(p)
            for p in sorted(Path(root).rglob("*")) if p.is_file()}


class ImportStoreDryRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = self.root = Path(tmp.name)
        self.target = root / "repo"
        self.target.mkdir()
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.dest = holophyte.project.state_dir(self.target) / "store.db"
        self.dest.parent.mkdir(parents=True)
        self.source = root / "other.db"
        # Closed before the command runs, so each file is whole on disk and
        # its hash is the store, not a snapshot short of its WAL.
        seed(self.dest, 5).close()
        seed(self.source, 3).close()
        self.before = (digest(self.source), digest(self.dest))

    def run_cli(self, *flags):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                holophyte.cli.cli([*flags, str(self.target)])
            except SystemExit as exit_:
                return out.getvalue(), err.getvalue(), exit_
        return out.getvalue(), err.getvalue(), None

    def test_dry_run_reports_runs_and_writes_nothing(self):
        out, _, exit_ = self.run_cli("--import-store", str(self.source),
                                     "--dry-run")
        self.assertIsNone(exit_)
        lines = out.splitlines()
        runs = next(line for line in lines if line.startswith("runs "))
        self.assertRegex(runs, r"^runs  rows 3  ids 1\.\.3  next 6  offset 5"
                               r"  sha256 [0-9a-f]{64}$")
        self.assertEqual(
            lines[-1],
            f"source {self.source.resolve()}  schema {store.SCHEMA_VERSION}")
        self.assertEqual((digest(self.source), digest(self.dest)), self.before)

    def test_a_legacy_destination_is_not_adopted(self):
        # A target whose store still sits in the dotted-sibling layout: any
        # other mode would move it under the home, which is a write.
        legacy = self.root / "legacy"
        legacy.mkdir()
        seed(self.root / "legacy.holophyte.db", 5).close()
        before = tree(self.root)
        self.target = legacy
        out, _, exit_ = self.run_cli("--import-store", str(self.source),
                                     "--dry-run")
        self.assertIn("no destination store", str(exit_.code))
        self.assertEqual(out, "")
        self.assertEqual(tree(self.root), before)

    def test_a_different_schema_version_is_refused(self):
        older = store.SCHEMA_VERSION - 1
        conn = sqlite3.connect(self.source)
        conn.execute(f"PRAGMA user_version = {older}")
        conn.close()
        before = (digest(self.source), digest(self.dest))
        out, _, exit_ = self.run_cli("--import-store", str(self.source),
                                     "--dry-run")
        self.assertIsNotNone(exit_)
        self.assertIn(f"source store is schema {older}", str(exit_.code))
        self.assertIn(f"destination store is schema {store.SCHEMA_VERSION}",
                      str(exit_.code))
        self.assertEqual(out, "")
        self.assertEqual((digest(self.source), digest(self.dest)), before)

    def test_import_without_dry_run_is_refused(self):
        _, err, exit_ = self.run_cli("--import-store", str(self.source))
        self.assertEqual(exit_.code, 2)
        self.assertIn("--import-store has only its dry run yet", err)
        self.assertEqual((digest(self.source), digest(self.dest)), self.before)


class ChecksumTests(unittest.TestCase):
    def test_same_rows_in_two_files_hash_the_same(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        first = seed(Path(tmp.name) / "first.db", 3)
        self.addCleanup(first.close)
        for pid in (11, 12):
            first.execute(
                "INSERT INTO supervisorHeartbeats (pid, startedAt, lastBeat,"
                " passes) VALUES (?, ?, ?, 1)", (pid, AT, AT))
        first.commit()
        # The second file gets the first's rows written in reverse, so a
        # checksum that followed insertion order would differ.
        second = store.open(str(Path(tmp.name) / "second.db"), migrate="owner")
        self.addCleanup(second.close)
        second.execute("PRAGMA foreign_keys = OFF")
        for table, _ in store_import.schema_tables():
            second.execute(f'DELETE FROM "{table}"')
            rows = first.execute(f'SELECT * FROM "{table}"').fetchall()
            for row in reversed(rows):
                marks = ", ".join("?" * len(row))
                second.execute(f'INSERT INTO "{table}" VALUES ({marks})', row)
        second.commit()
        sums = [{t.table: t.sha256 for t in store_import.plan(conn, conn).tables}
                for conn in (first, second)]
        self.assertEqual(sums[0], sums[1])
        empty = hashlib.sha256(b"").hexdigest()
        self.assertNotEqual(sums[0]["runs"], empty)
        self.assertNotEqual(sums[0]["supervisorHeartbeats"], empty)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", s)
                            for s in sums[0].values()))

    def test_a_commit_mid_plan_is_not_seen(self):
        # A writer commits a heartbeat after the plan has counted the table
        # and before it checksums it: both must describe the rows as they
        # stood when the plan began, which here is none.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "live.db"
        writer = seed(path, 3)
        self.addCleanup(writer.close)
        real = store_import.checksum

        def commit_first(conn, table, id_column):
            if table == "supervisorHeartbeats":
                writer.execute(
                    "INSERT INTO supervisorHeartbeats (pid, startedAt,"
                    " lastBeat, passes) VALUES (11, ?, ?, 1)", (AT, AT))
                writer.commit()
            return real(conn, table, id_column)

        reader = store.read.open_readonly(path)
        self.addCleanup(reader.close)
        with patch.object(store_import, "checksum", commit_first):
            result = store_import.plan(reader, reader)
        beats = next(t for t in result.tables
                     if t.table == "supervisorHeartbeats")
        self.assertEqual((beats.rows, beats.sha256),
                         (0, hashlib.sha256(b"").hexdigest()))
        self.assertFalse(reader.in_transaction)


if __name__ == "__main__":
    unittest.main()

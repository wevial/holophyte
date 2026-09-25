"""KO-733: ticket revisions, ticket notes and the board columns, one additive
bump a store's previous build can still open and write."""
import ast
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
from holophyte.pool_handoff import _literal

FROZEN = Path(__file__).with_name("store_pre_revisions.sql")


def frozen_version():
    """The version named on the frozen schema's first comment line."""
    first = FROZEN.read_text().splitlines()[0]
    return int(re.search(r"schema (\d+)", first).group(1))


class PreviousStoreMigrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.db"
        conn = sqlite3.connect(self.path)
        # Frozen pre-change schema, not generated from the implementation.
        conn.executescript(FROZEN.read_text())
        conn.execute("INSERT INTO projects (linearTeamId, repoPath,"
                     " defaultBranch, autonomyProfile)"
                     " VALUES ('t', '/repo', 'main', 'personal')")
        conn.execute("INSERT INTO tickets (projectId, linearIssueId,"
                     " linearIdentifier, title, body, mirroredAt, status,"
                     " affinity)"
                     " VALUES (1, 'i1', 'KO-1', 'first', '## Summary', 1,"
                     " 'merged', 'any')")
        conn.execute("INSERT INTO tickets (projectId, linearIssueId,"
                     " linearIdentifier, title, mirroredAt, status, affinity)"
                     " VALUES (1, 'i2', 'KO-2', 'second', 1, 'ready', 'any')")
        conn.execute("INSERT INTO runs (ticketId, projectId, attempt, phase,"
                     " startedAt, lastHeartbeat, endedAt, outcome)"
                     " VALUES (1, 1, 1, 'done', 1, 2, 2, 'merged')")
        conn.execute(f"PRAGMA user_version = {frozen_version():d}")
        conn.commit()
        conn.close()

    def migrated(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def test_each_ticket_is_backfilled_as_revision_one_once(self):
        conn = self.migrated()
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            store.SCHEMA_VERSION)
        expected = [(1, 1, 1, "backfill", "first", "## Summary"),
                    (2, 1, 1, "backfill", "second", "")]
        rows = ("SELECT t.id, t.revision, r.revision, r.author, r.title, r.body"
                " FROM tickets t JOIN ticketRevisions r ON r.ticketId = t.id"
                " ORDER BY t.id")
        self.assertEqual(conn.execute(rows).fetchall(), expected)
        note = json.loads(store.schema.latest_migration_note(conn))
        self.assertEqual(note["readableFrom"], store.schema.READABLE_FROM)
        store.init(conn)
        self.assertEqual(conn.execute(rows).fetchall(), expected)

    def test_the_previous_build_opens_and_writes_the_migrated_store(self):
        self.migrated().close()
        with patch.object(store.schema, "SCHEMA_VERSION",
                          store.schema.READABLE_FROM):
            conn = store.open(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0],
            store.SCHEMA_VERSION)
        # The previous build's own statements, frozen: mirror_ticket()'s
        # INSERT, claim()'s INSERT and heartbeat()'s UPDATE.
        with conn:
            ticket = conn.execute(
                "INSERT INTO tickets"
                " (projectId, linearIssueId, linearIdentifier, title, body,"
                "  status, acceptanceCriteria, verificationCommands, timeBoxMs,"
                "  affinity, dependsOn, mirroredAt, url, boardState)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "i3", "KO-3", "third", "", "ready", '["a"]', '["true"]',
                 None, "any", "[]", 3, None, "Todo")).lastrowid
            run = conn.execute(
                "INSERT INTO runs"
                " (ticketId, projectId, attempt, phase, startedAt, lastHeartbeat,"
                "  timeBoxMs, ticketSnapshot, host, workerPid, workingMs, verifyMs)"
                " VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?, 0, 0)",
                (ticket, 1, 1, 3, 3, None, None, "host", 1)).lastrowid
            beat = conn.execute(
                "UPDATE runs SET lastHeartbeat = ? WHERE id = ?"
                " AND endedAt IS NULL", (4, run)).rowcount
        self.assertEqual(beat, 1)
        self.assertEqual(conn.execute(
            "SELECT boardColumn, priority, labels, filedAt, boardUpdatedAt,"
            " revision, pushState, pushFrom, pushAt, goneSince"
            " FROM tickets WHERE id = ?", (ticket,)).fetchone(),
            (None, None, "[]", None, None, 0, None, None, None, None))
        self.assertEqual(conn.execute(
            "SELECT revision, lastHeartbeat FROM runs WHERE id = ?",
            (run,)).fetchone(), (None, 4))
        self.assertEqual(conn.execute(
            "SELECT ticketSeq FROM projects").fetchone(), (0,))


class FreshStoreBoardSchemaTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO projects (linearTeamId, repoPath,"
                          " defaultBranch, autonomyProfile)"
                          " VALUES ('t', '/repo', 'main', 'personal')")
        self.conn.execute("INSERT INTO tickets (projectId, linearIssueId,"
                          " linearIdentifier, title, mirroredAt, status,"
                          " affinity) VALUES (1, 'i', 'KO-1', 't', 1,"
                          " 'ready', 'any')")

    def test_readable_from_reads_as_an_integer_below_the_version(self):
        source = Path(store.schema.__file__).read_text()
        found = {}
        for node in ast.parse(source).body:
            for name in (node.targets if isinstance(node, ast.Assign) else ()):
                if isinstance(name, ast.Name) and name.id in (
                        "SCHEMA_VERSION", "READABLE_FROM"):
                    found[name.id] = _literal(node.value, found)
        self.assertIsInstance(found["READABLE_FROM"], int)
        self.assertLess(found["READABLE_FROM"], found["SCHEMA_VERSION"])

    def test_board_column_admits_ready_and_refuses_doing_on_both_tables(self):
        writes = (
            "UPDATE tickets SET boardColumn = ? WHERE id = 1",
            "INSERT INTO ticketRevisions (ticketId, revision, at, author,"
            " title, boardColumn) VALUES (1, 1, 1, 'test', 't', ?)",
        )
        for sql in writes:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(sql, ("doing",))
                self.assertEqual(self.conn.execute(sql, ("ready",)).rowcount, 1)

    def test_a_second_note_with_the_same_dedup_key_is_refused(self):
        note = ("INSERT INTO ticketNotes (ticketId, at, author, kind, dedupKey,"
                " text) VALUES (1, 1, 'loop', 'failure', 'run-1', ?)")
        self.conn.execute(note, ("first",))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(note, ("again",))


if __name__ == "__main__":
    unittest.main()

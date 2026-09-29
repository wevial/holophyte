"""The gap layer record: an additive table, its writer and its per-layer count."""
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
from store.gap_layers import gap_layer_counts, record_gap_layer

FROZEN = Path(__file__).with_name("store_pre_gap_layers.sql")
LADDER = ["impossible", "static", "witness", "guidance", "review", "none"]


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
        conn.executescript(FROZEN.read_text())
        conn.execute("INSERT INTO projects (linearTeamId, repoPath,"
                     " defaultBranch, autonomyProfile)"
                     " VALUES ('t', '/repo', 'main', 'personal')")
        for issue, identifier, status in (("i1", "KO-1", "merged"),
                                          ("i2", "KO-2", "ready")):
            conn.execute("INSERT INTO tickets (projectId, linearIssueId,"
                         " linearIdentifier, title, mirroredAt, status,"
                         " affinity, revision)"
                         " VALUES (1, ?, ?, 't', 1, ?, 'any', 1)",
                         (issue, identifier, status))
        conn.execute("INSERT INTO runs (ticketId, projectId, attempt, phase,"
                     " startedAt, lastHeartbeat, endedAt, outcome)"
                     " VALUES (1, 1, 1, 'done', 1, 2, 2, 'merged')")
        conn.execute(f"PRAGMA user_version = {frozen_version():d}")
        conn.commit()
        self.ticket_columns = ", ".join(
            row[1] for row in conn.execute("PRAGMA table_info(tickets)"))
        self.tickets = conn.execute(
            f"SELECT {self.ticket_columns} FROM tickets ORDER BY id").fetchall()
        conn.close()

    def test_migration_adds_an_empty_table_and_keeps_the_tickets(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         store.SCHEMA_VERSION)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM gapLayers").fetchone(), (0,))
        self.assertEqual(
            conn.execute(f"SELECT {self.ticket_columns} FROM tickets"
                         " ORDER BY id").fetchall(),
            self.tickets)
        note = json.loads(store.schema.latest_migration_note(conn))
        self.assertEqual(note["readableFrom"], store.schema.READABLE_FROM)

    def test_the_oldest_readable_build_opens_and_writes_the_migrated_store(self):
        store.open(self.path).close()
        with patch.object(store.schema, "SCHEMA_VERSION",
                          store.schema.READABLE_FROM):
            conn = store.open(self.path)
        self.addCleanup(conn.close)
        with conn:
            conn.execute(
                "INSERT INTO tickets"
                " (projectId, linearIssueId, linearIdentifier, title, body,"
                "  status, acceptanceCriteria, verificationCommands, timeBoxMs,"
                "  affinity, dependsOn, mirroredAt, url, boardState, priority,"
                "  labels, boardColumn, filedAt, boardUpdatedAt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "i3", "KO-3", "third", "", "ready", '["a"]', '["true"]',
                 None, "any", "[]", 3, None, "Todo", None, "[]", None, None,
                 None))
        self.assertEqual(conn.execute(
            "SELECT linearIdentifier FROM tickets ORDER BY id").fetchall(),
            [("KO-1",), ("KO-2",), ("KO-3",)])


class GapLayerRecordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO projects (linearTeamId, repoPath,"
                          " defaultBranch, autonomyProfile)"
                          " VALUES ('t', '/repo', 'main', 'personal')")
        self.ko1, self.ko2 = (self.conn.execute(
            "INSERT INTO tickets (projectId, linearIssueId, linearIdentifier,"
            " title, mirroredAt, status, affinity)"
            " VALUES (1, ?, ?, 't', 1, 'merged', 'any')",
            (identifier.lower(), identifier)).lastrowid
            for identifier in ("KO-1", "KO-2"))
        self.conn.commit()

    def rows(self):
        return self.conn.execute(
            "SELECT ticketId, layer, note, carriedBy, author, at"
            " FROM gapLayers ORDER BY id").fetchall()

    def test_counts_take_each_gaps_latest_layer_in_ladder_order(self):
        record_gap_layer(self.conn, self.ko1, "witness", "a test pins it",
                         "operator", now=10)
        record_gap_layer(self.conn, self.ko1, "static", "a lint refuses it",
                         "operator", carried_by="HOLO-7", now=20)
        record_gap_layer(self.conn, self.ko2, "impossible", "the type forbids",
                         "reviewer", now=30)
        counts = gap_layer_counts(self.conn)
        self.assertEqual(list(counts.items()),
                         [("impossible", 1), ("static", 1), ("witness", 0),
                          ("guidance", 0), ("review", 0), ("none", 0)])
        self.assertEqual(self.rows()[:2], [
            (self.ko1, "witness", "a test pins it", None, "operator", 10),
            (self.ko1, "static", "a lint refuses it", "HOLO-7", "operator", 20),
        ])

    def test_an_empty_store_counts_every_layer_at_zero(self):
        self.assertEqual(list(gap_layer_counts(self.conn).items()),
                         [(layer, 0) for layer in LADDER])

    def test_a_bad_argument_is_refused_before_any_row(self):
        cases = [
            ("lint", dict(layer="lint")),
            ("note", dict(note="  ")),
            ("author", dict(author="")),
            ("carried_by", dict(carried_by="holo 7")),
            ("carried_by", dict(carried_by="HOLO-7\n")),
            ("carried_by", dict(carried_by=7)),
            ("not in the store", dict(ticket_id=999)),
        ]
        for problem, change in cases:
            call = dict(ticket_id=self.ko1, layer="guidance", note="lesson",
                        author="operator")
            call.update(change)
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(ValueError, problem):
                    record_gap_layer(self.conn, **call)
                self.assertEqual(self.rows(), [])

    def test_the_layer_check_refuses_a_raw_insert(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO gapLayers (ticketId, layer, note, author, at)"
                " VALUES (?, 'lint', 'lesson', 'operator', 1)", (self.ko1,))


if __name__ == "__main__":
    unittest.main()

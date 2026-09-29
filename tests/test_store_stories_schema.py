"""The story tables and columns: an additive bump an older build still uses."""
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store

FROZEN = Path(__file__).with_name("store_pre_stories.sql")
STORY_TABLES = ("stories", "storyWitnesses", "storyChildren",
                "witnessResults", "storyDecisions")


def frozen_version():
    """The version named on the frozen schema's first comment line."""
    first = FROZEN.read_text().splitlines()[0]
    return int(re.search(r"schema (\d+)", first).group(1))


def indexed_columns(conn, table):
    return {column
            for index in conn.execute(f"PRAGMA index_list({table})")
            for (*_, column) in conn.execute(f"PRAGMA index_info({index[1]})")}


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
                         " affinity, revision, boardColumn)"
                         " VALUES (1, ?, ?, 't', 1, ?, 'any', 1, 'ready')",
                         (issue, identifier, status))
        conn.execute("INSERT INTO runs (ticketId, projectId, attempt, phase,"
                     " startedAt, lastHeartbeat, endedAt, outcome)"
                     " VALUES (1, 1, 1, 'done', 1, 2, 2, 'merged')")
        conn.execute("INSERT INTO gapLayers (ticketId, layer, note, author, at)"
                     " VALUES (1, 'witness', 'a test pins it', 'operator', 3)")
        conn.execute('INSERT INTO interventions (runId, source, "trigger",'
                     " action, at) VALUES (1, 'human', 'manual', 'approve', 4)")
        conn.execute(f"PRAGMA user_version = {frozen_version():d}")
        conn.commit()
        self.ticket_columns = ", ".join(
            row[1] for row in conn.execute("PRAGMA table_info(tickets)"))
        self.tickets = conn.execute(
            f"SELECT {self.ticket_columns} FROM tickets ORDER BY id").fetchall()
        conn.close()

    def test_migration_adds_empty_story_tables_and_null_story_columns(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         store.SCHEMA_VERSION)
        for table in STORY_TABLES:
            with self.subTest(table=table):
                self.assertEqual(conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone(), (0,))
        self.assertEqual(conn.execute(
            "SELECT parentTicketId FROM tickets ORDER BY id").fetchall(),
            [(None,), (None,)])
        self.assertEqual(conn.execute(
            "SELECT storyGeneration FROM runs").fetchall(), [(None,)])
        self.assertEqual(conn.execute(
            "SELECT foundBy FROM gapLayers").fetchall(), [("operator",)])
        self.assertEqual(conn.execute(
            f"SELECT {self.ticket_columns} FROM tickets ORDER BY id"
        ).fetchall(), self.tickets)
        self.assertEqual(conn.execute(
            "SELECT runId, action FROM interventions WHERE action != 'migrate'"
        ).fetchall(), [(1, "approve")])
        note = json.loads(store.schema.latest_migration_note(conn))
        self.assertEqual(note["readableFrom"], store.schema.READABLE_FROM)

    def test_the_previous_build_writes_and_claims_on_the_migrated_store(self):
        store.open(self.path).close()
        with patch.object(store.schema, "SCHEMA_VERSION", frozen_version()):
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
            conn.execute(
                "INSERT INTO interventions"
                ' (runId, projectId, source, "trigger", action, note, question,'
                "  guidance, at)"
                " VALUES (1, 1, 'human', 'manual', 'operator_note', 'n', NULL,"
                " NULL, 5)")
        run_id = store.claim(conn, 1, 2)
        self.assertEqual(conn.execute(
            "SELECT ticketId, phase, storyGeneration FROM runs WHERE id = ?",
            (run_id,)).fetchone(), (2, "claimed", None))
        self.assertEqual(conn.execute(
            "SELECT linearIdentifier FROM tickets ORDER BY id").fetchall(),
            [("KO-1",), ("KO-2",), ("KO-3",)])

    def test_fresh_and_migrated_stores_index_the_parent_ticket(self):
        migrated = store.open(self.path)
        self.addCleanup(migrated.close)
        fresh = store.open(self.path.with_name("fresh.db"))
        self.addCleanup(fresh.close)
        for name, conn in (("fresh", fresh), ("migrated", migrated)):
            with self.subTest(store=name):
                self.assertIn("parentTicketId",
                              indexed_columns(conn, "tickets"))


class StoryConstraintTests(unittest.TestCase):
    ROWS = {
        "stories": {"ticketId": 1, "state": "planned"},
        "storyChildren": {"ticketId": 1, "storyId": 1, "role": "completes"},
        "storyDecisions": {"storyId": 1, "kind": "unmet", "question": "q",
                           "options": "[]", "defaultOption": "a", "at": 1},
        "witnessResults": {"storyId": 1, "witnessKey": "w", "mainSha": "s",
                           "verdict": "red", "redKind": "assert",
                           "verifier": "loop", "at": 1},
        "gapLayers": {"ticketId": 1, "layer": "witness", "note": "n",
                      "author": "operator", "foundBy": "witness", "at": 1},
    }

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
                          " affinity) VALUES (1, 'i1', 'KO-1', 't', 1,"
                          " 'ready', 'any')")

    def insert(self, table, **change):
        row = {**self.ROWS[table], **change}
        self.conn.execute(
            f"INSERT INTO {table} ({', '.join(row)})"
            f" VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))

    def test_a_value_outside_each_story_enum_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert("stories", state="bogus")
        self.insert("stories")
        for table, column in (("storyChildren", "role"),
                              ("storyDecisions", "kind"),
                              ("witnessResults", "verdict"),
                              ("witnessResults", "redKind"),
                              ("witnessResults", "verifier"),
                              ("gapLayers", "foundBy")):
            with self.subTest(column=f"{table}.{column}"):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.insert(table, **{column: "bogus"})

    def test_the_story_actions_valid_rows_and_a_null_red_kind_are_accepted(self):
        for action in ("approve_story", "decide"):
            self.conn.execute(
                'INSERT INTO interventions (projectId, source, "trigger",'
                " action, at) VALUES (1, 'human', 'manual', ?, 1)", (action,))
        for table in self.ROWS:
            self.insert(table)
        self.insert("witnessResults", verdict="green", redKind=None)
        self.assertEqual(self.conn.execute(
            "SELECT verdict, redKind FROM witnessResults ORDER BY id").fetchall(),
            [("red", "assert"), ("green", None)])
        self.assertEqual(self.conn.execute(
            "SELECT action FROM interventions WHERE projectId = 1"
            " ORDER BY id").fetchall(), [("approve_story",), ("decide",)])


if __name__ == "__main__":
    unittest.main()

"""Named loop writers change only their intended columns and join transactions."""
import tempfile
import unittest
from pathlib import Path

import store


class StoreSeamTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = store.open(self.path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        for i in (1, 2):
            project = store.ensure_project(self.conn, f"team-{i}", f"/repos/{i}")
            ticket = store.mirror_ticket(
                self.conn, project, f"issue-{i}", f"KO-{i}", "A ticket")
            run = store.claim(self.conn, project, ticket, now=1000)
            with store.transaction(self.conn):
                self.conn.execute(
                    "UPDATE runs SET mergeSha = ?, candidateSha = ?, prUrl = ?,"
                    " outcomeReason = ? WHERE id = ?",
                    ("a" * 40, "b" * 40, "old-url", "old reason", run))
                self.conn.execute(
                    "UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                    ("old question", ticket))
            if i == 1:
                self.project_id, self.ticket, self.run = project, ticket, run
        self.reader = store.open(self.path)
        self.addCleanup(self.reader.close)

    def snapshot(self, conn):
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        result = {}
        for (table,) in tables:
            cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            columns = [column[0] for column in cursor.description]
            result[table] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return result

    def cases(self):
        return [
            ("set_board_state", (self.ticket, "In Progress"),
             "tickets", self.ticket, {"boardState": "In Progress"}),
            ("set_question", (self.ticket, "merge?"),
             "tickets", self.ticket, {"blockedQuestion": "merge?"}),
            ("set_question", (self.ticket, None),
             "tickets", self.ticket, {"blockedQuestion": None}),
            ("clear_merge_sha", (self.run,),
             "runs", self.run, {"mergeSha": None}),
            ("set_pull_request", (self.run, "new-url", "c" * 40),
             "runs", self.run, {"prUrl": "new-url", "candidateSha": "c" * 40}),
            ("set_pull_request", (self.run, "url-only"),
             "runs", self.run, {"prUrl": "url-only"}),
            ("set_outcome_reason", (self.run, "wake breaker"),
             "runs", self.run, {"outcomeReason": "wake breaker"}),
            ("stamp_board_ask", (self.project_id, 123456),
             "projects", self.project_id, {"boardAskedAt": 123456}),
        ]

    def test_writers_commit_only_the_intended_row_columns(self):
        for name, args, table, row_id, changes in self.cases():
            with self.subTest(writer=name, args=args):
                expected = self.snapshot(self.reader)
                for row in expected[table]:
                    if row["id"] == row_id:
                        row.update(changes)
                getattr(store, name)(self.conn, *args)
                self.assertFalse(self.conn.in_transaction)
                self.assertEqual(self.snapshot(self.reader), expected)

    def test_writers_join_and_roll_back_with_the_callers_transaction(self):
        for name, args, _, _, _ in self.cases():
            with self.subTest(writer=name, args=args):
                before = self.snapshot(self.reader)
                with self.assertRaisesRegex(RuntimeError, "abort"):
                    with store.transaction(self.conn):
                        getattr(store, name)(self.conn, *args)
                        self.assertNotEqual(self.snapshot(self.conn), before)
                        self.assertEqual(self.snapshot(self.reader), before)
                        raise RuntimeError("abort")
                self.assertEqual(self.snapshot(self.reader), before)

    def test_question_kind_is_atomic_and_survives_prose_updates(self):
        for released in (False, True):
            with self.subTest(released=released):
                kind = "pull_request_closed" if released else "pull_request"
                if released:
                    store.release(self.conn, self.run, "failed", "verify failed")
                before = self.snapshot(self.reader)
                with self.assertRaisesRegex(RuntimeError, "abort"):
                    with store.transaction(self.conn):
                        store.set_question(self.conn, self.ticket, "merge?",
                                           park_kind=kind)
                        self.assertEqual(self.snapshot(self.reader), before)
                        raise RuntimeError("abort")
                self.assertEqual(self.snapshot(self.reader), before)
                store.set_question(self.conn, self.ticket, "merge?",
                                   park_kind=kind)
                store.set_question(self.conn, self.ticket, "Approval needed")
                self.assertEqual(self.conn.execute(
                    "SELECT parkKind FROM runs WHERE id = ?",
                    (self.run,)).fetchone(), (kind,))
                self.assertEqual(self.conn.execute(
                    "SELECT blockedQuestion FROM tickets WHERE id = ?",
                    (self.ticket,)).fetchone(), ("Approval needed",))


if __name__ == "__main__":
    unittest.main()

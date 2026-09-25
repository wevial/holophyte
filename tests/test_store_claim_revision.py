"""Phase 3 stage 3: a store-mode claim finalises only at the revision its
admission judged. `store.claim(expected_revision=)` refuses a ticket a
board edit moved on, or one no longer `ready` in column `ready`, and
`mirror_ticket(expected_revision=)` does not write a task older than its
row. Real SQLite: every racing party has a connection of its own on one
file, so the serialisation witnessed is the database's.

Run: python3 -m unittest discover -s tests -p 'test_store_claim_revision.py' -v
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import store  # noqa: E402 - after the sys.path insert above
import store.tickets  # noqa: E402 - after the sys.path insert above

ISSUE = "iss-1"


class ClaimRevisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "store.sqlite3"
        self.conn = self.open()
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", "/repos/holophyte")
        self.ticket = self.mirror(self.conn)

    def open(self):
        conn = store.open(self.path)
        self.addCleanup(conn.close)
        return conn

    def mirror(self, conn, title="add a thing", column="ready",
               criteria=("Given the thing, when it runs, then it works",),
               expected_revision=None):
        return store.tickets.mirror_ticket(
            conn, self.project_id, linear_issue_id=ISSUE, linear_identifier="KO-1",
            title=title, acceptance_criteria=criteria,
            verification_commands=["echo ok"], board_column=column,
            expected_revision=expected_revision)

    def row(self):
        return self.conn.execute(
            "SELECT revision, title, activeRunId FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone()

    def runs(self):
        return self.conn.execute(
            "SELECT id, revision FROM runs ORDER BY id").fetchall()

    def in_thread(self, work):
        """Run `work(conn)` on a thread with a connection of its own;
        answer the thread and a list its result or exception lands in."""
        answer = []

        def body():
            conn = store.open(self.path)
            try:
                answer.append(work(conn))
            except Exception as e:  # noqa: BLE001 - the test reads it
                answer.append(e)
            finally:
                conn.close()

        thread = threading.Thread(target=body)
        thread.start()
        return thread, answer

    def test_an_edit_committed_after_admission_refuses_the_claim(self):
        admitted = self.row()[0]
        self.mirror(self.open(), title="add the thing")

        with self.assertRaises(store.RevisionMoved) as refused:
            store.claim(self.conn, self.project_id, self.ticket,
                        expected_revision=admitted)

        self.assertEqual((refused.exception.expected, refused.exception.current),
                         (1, 2))
        self.assertIn("revision 1 to 2", str(refused.exception))
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.row(), (2, "add the thing", None))

    def test_an_edit_holding_the_write_lock_first_refuses_the_waiting_claim(self):
        locked, begun, at = threading.Event(), threading.Event(), {}

        def edit(conn):
            conn.execute("BEGIN IMMEDIATE")
            self.mirror(conn, title="add the thing")
            locked.set()
            # The claim's own BEGIN IMMEDIATE has started, against this
            # held write lock: it can only be waiting on it now.
            self.assertTrue(begun.wait(5))
            time.sleep(0.2)
            at["committed"] = time.monotonic()
            conn.commit()

        def claim_begins(statement):
            if statement == "BEGIN IMMEDIATE" and "begun" not in at:
                at["begun"] = time.monotonic()
                begun.set()

        thread, _ = self.in_thread(edit)
        self.assertTrue(locked.wait(5))
        self.conn.set_trace_callback(claim_begins)
        with self.assertRaises(store.RevisionMoved) as refused:
            store.claim(self.conn, self.project_id, self.ticket,
                        expected_revision=1)
        at["refused"] = time.monotonic()
        self.conn.set_trace_callback(None)
        thread.join(5)

        # Begun before the edit committed and refused after it: the claim
        # waited on the edit's lock, then read the edit's revision.
        self.assertLess(at["begun"], at["committed"])
        self.assertLess(at["committed"], at["refused"])
        self.assertGreaterEqual(at["refused"] - at["begun"], 0.2)
        self.assertEqual(refused.exception.current, 2)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.row(), (2, "add the thing", None))

    def test_a_claim_holding_the_write_lock_first_wins_and_the_edit_follows(self):
        inserting = threading.Event()

        def pause_at_the_run_insert(statement):
            if statement.startswith("INSERT INTO runs"):
                inserting.set()
                time.sleep(0.2)  # the edit is blocked on the lock by now

        self.conn.set_trace_callback(pause_at_the_run_insert)

        def edit(conn):
            self.assertTrue(inserting.wait(5))
            return self.mirror(conn, title="add the thing")

        thread, edited = self.in_thread(edit)
        run = store.claim(self.conn, self.project_id, self.ticket,
                          expected_revision=1)
        self.conn.set_trace_callback(None)
        thread.join(5)

        self.assertEqual(edited, [self.ticket])
        self.assertEqual(self.runs(), [(run, 1)])
        self.assertEqual(self.row(), (2, "add the thing", run))
        # The edit applies from the next claim.
        store.release(self.conn, run, "failed", "released for the next claim",
                      outcome_class="infra", failure_kind="infra")
        store.tickets.walk_ticket(self.conn, self.ticket, "ready")
        again = store.claim(self.conn, self.project_id, self.ticket,
                            expected_revision=2)
        self.assertEqual(self.runs(), [(run, 1), (again, 2)])

    def test_sibling_claims_at_one_revision_elect_exactly_one_run(self):
        start = threading.Barrier(4)

        def claim(conn):
            start.wait(5)
            return store.claim(conn, self.project_id, self.ticket,
                               expected_revision=1)

        threads = [self.in_thread(claim) for _ in range(4)]
        for thread, _ in threads:
            thread.join(10)
        answers = [answer[0] for _, answer in threads]

        won = [a for a in answers if isinstance(a, int)]
        lost = [a for a in answers if isinstance(a, store.ClaimConflict)]
        self.assertEqual((len(won), len(lost)), (1, 3), answers)
        self.assertEqual(self.runs(), [(won[0], 1)])
        self.assertEqual(self.row()[2], won[0])

    def test_a_ticket_out_of_the_ready_column_or_status_is_refused(self):
        self.mirror(self.conn, column="backlog")
        with self.assertRaises(store.ClaimConflict) as backlog:
            store.claim(self.conn, self.project_id, self.ticket,
                        expected_revision=2)
        self.assertIn("KO-1 is ready in column backlog", str(backlog.exception))

        self.mirror(self.conn, column="ready", criteria=())
        with self.assertRaises(store.ClaimConflict) as unspecced:
            store.claim(self.conn, self.project_id, self.ticket,
                        expected_revision=3)
        self.assertIn("needs_spec in column ready", str(unspecced.exception))
        self.assertEqual(self.runs(), [])

    def test_without_an_expected_revision_a_row_with_no_column_claims(self):
        other = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="iss-2",
            linear_identifier="KO-2", title="another thing",
            acceptance_criteria=["Given it, then it works"],
            verification_commands=["echo ok"])
        self.assertIsNone(self.conn.execute(
            "SELECT boardColumn FROM tickets WHERE id = ?", (other,)).fetchone()[0])

        run = store.claim(self.conn, self.project_id, other)

        self.assertEqual(self.runs(), [(run, 1)])

    def test_a_mirror_of_a_stale_revision_writes_nothing(self):
        self.mirror(self.open(), title="add the thing")

        answer = self.mirror(self.conn, title="add a thing",
                             expected_revision=1)

        self.assertEqual(answer, self.ticket)
        self.assertEqual(self.row(), (2, "add the thing", None))
        (count,) = self.conn.execute(
            "SELECT COUNT(*) FROM ticketRevisions WHERE ticketId = ?",
            (self.ticket,)).fetchone()
        self.assertEqual(count, 2)


if __name__ == "__main__":
    unittest.main()

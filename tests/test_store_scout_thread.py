"""A scout's thread: its table, its migration, and its writers and reads."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import store
import store.scout_thread as thread
import store.scouts as scouts
from holophyte.redact import REDACTED, values_held
from store.scout_thread import ThreadRefused
from store.scouts import ScoutRefused


def open_store(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "store.db"
    conn = store.open(path)
    test.addCleanup(conn.close)
    return path, conn


class ThreadCase(unittest.TestCase):
    def setUp(self):
        self.path, self.conn = open_store(self)
        self.project_id = store.ensure_project(self.conn, "team",
                                               "/repos/example")

    def scout_in(self, phase):
        scout_id = scouts.create_scout(self.conn, self.project_id, "why slow",
                                       "bug", 30)
        if phase == "queued":
            return scout_id
        scouts.move_scout(self.conn, scout_id, "planning", base_sha="abc",
                          host="writer", now=5000)
        scouts.move_scout(self.conn, scout_id, "swarming")
        if phase in ("done", "failed", "stopped"):
            scouts.end_scout(self.conn, scout_id, phase, f"ended {phase}",
                             totals={"tokens": 7}, now=7000)
        return scout_id

    def thread_rows(self):
        return self.conn.execute(
            "SELECT * FROM threadMessages ORDER BY id").fetchall()

    def refused(self, write, *named):
        rows, scout_rows = self.thread_rows(), self.conn.execute(
            "SELECT * FROM scouts ORDER BY id").fetchall()
        events = self.conn.execute("SELECT COUNT(*) FROM runEvents").fetchone()
        with self.assertRaises(ThreadRefused) as caught:
            write()
        for name in named:
            self.assertIn(str(name), str(caught.exception))
        self.assertEqual(self.thread_rows(), rows)
        self.assertEqual(self.conn.execute(
            "SELECT * FROM scouts ORDER BY id").fetchall(), scout_rows)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM runEvents").fetchone(), events)
        return caught.exception

    def ask(self, scout_id, text="what next?", now=None):
        return thread.post_message(self.conn, scout_id, "maintainer", "ask",
                                   text, now=now)


class ThreadMigrationTests(ThreadCase):
    def test_a_store_one_version_below_gains_an_empty_thread_table(self):
        scout_id = self.scout_in("swarming")
        scouts.record_scout_event(self.conn, scout_id, "first", "one", now=1)
        scouts.record_scout_event(self.conn, scout_id, "second", "two", now=2)
        scouts.record_scout_report(self.conn, scout_id, 1, {"summary": "s"})
        scouts.end_scout(self.conn, scout_id, "done", "answered", now=7000)
        snapshot = {table: self.conn.execute(
            f"SELECT * FROM {table} ORDER BY id").fetchall()
            for table in ("scouts", "scoutReports", "runEvents")}
        self.assertEqual(len(snapshot["runEvents"]), 2)
        self.conn.executescript(
            "DROP INDEX threadMessages_scoutId; DROP TABLE threadMessages;"
            f" PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1:d};")
        self.conn.close()

        conn = store.open(self.path)
        self.addCleanup(conn.close)

        self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                         (store.schema.SCHEMA_VERSION,))
        for table, rows in snapshot.items():
            self.assertEqual(conn.execute(
                f"SELECT * FROM {table} ORDER BY id").fetchall(), rows, table)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM threadMessages").fetchone(), (0,))
        note = json.loads(store.schema.latest_migration_note(conn))
        self.assertEqual(note["readableFrom"], store.schema.READABLE_FROM)
        self.assertEqual(note["to"], store.schema.SCHEMA_VERSION)


class PostTests(ThreadCase):
    def test_an_ask_reads_back_stripped_and_a_second_waits_its_turn(self):
        scout_id = self.scout_in("done")
        first = thread.post_message(self.conn, scout_id, " maintainer ",
                                    "ask", "  why here?  ", now=9000)
        (message,) = thread.messages(self.conn, scout_id)
        self.assertEqual(
            (message.id, message.project_id, message.mode, message.author,
             message.text, message.blocks, message.answers_id,
             message.consumed_by),
            (first, self.project_id, "ask", "maintainer", "why here?", [],
             None, None))
        self.refused(lambda: self.ask(scout_id, "and there?"),
                     f"message {first}")

    def test_asks_to_an_ended_scout_and_steers_while_one_waits_are_refused(self):
        for phase in ("failed", "stopped"):
            scout_id = self.scout_in(phase)
            with self.subTest(phase=phase):
                self.refused(lambda: self.ask(scout_id), phase)
        waiting = self.scout_in("done")
        asked = self.ask(waiting)
        self.refused(lambda: thread.post_message(
            self.conn, waiting, "maintainer", "steer", "check more"),
            f"message {asked}")
        self.refused(lambda: self.ask(9999), "scout 9999")
        rows = self.thread_rows()
        with self.assertRaises(ValueError):
            thread.post_message(self.conn, self.scout_in("done"), "maintainer",
                                "ask", "   ")
        self.assertEqual(self.thread_rows(), rows)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO threadMessages (projectId, scoutId, author, mode,"
                " text, at) VALUES (?, ?, 'maintainer', 'chat', 'hi', 1)",
                (self.project_id, waiting))
        self.assertEqual(self.thread_rows(), rows)

    def test_a_held_value_is_redacted_and_waiting_reads_oldest_first(self):
        first, second = self.scout_in("done"), self.scout_in("done")
        with values_held(["hunter2-secret-value"]):
            later = self.ask(second, "is hunter2-secret-value leaked?",
                             now=100)
        earlier = self.ask(first, "and this?", now=200)
        (stored,) = self.conn.execute(
            "SELECT text FROM threadMessages WHERE id = ?", (later,)).fetchone()
        self.assertNotIn("hunter2-secret-value", stored)
        self.assertEqual(stored, f"is {REDACTED} leaked?")
        self.assertEqual([m.id for m in thread.waiting_messages(self.conn)],
                         [later, earlier])


class TakeTests(ThreadCase):
    def answering(self):
        scout_id = self.scout_in("done")
        asked = self.ask(scout_id)
        thread.take_message(self.conn, asked, host="host-h", now=20000)
        return scout_id, asked

    def test_taking_an_ask_moves_the_scout_to_answering_once(self):
        scout_id = self.scout_in("done")
        self.conn.execute("UPDATE scouts SET pid = 4242 WHERE id = ?",
                          (scout_id,))
        self.conn.commit()
        before = scouts.scout(self.conn, scout_id)
        asked = self.ask(scout_id)
        self.assertEqual([m.id for m in thread.waiting_messages(self.conn)],
                         [asked])

        event_id = thread.take_message(self.conn, asked, host="host-h",
                                       now=before.heartbeat_at + 1000)

        row = scouts.scout(self.conn, scout_id)
        self.assertEqual((row.phase, row.pid, row.host),
                         ("answering", None, "host-h"))
        self.assertGreater(row.heartbeat_at, before.heartbeat_at)
        kind, payload = self.conn.execute(
            "SELECT kind, payload FROM runEvents WHERE id = ? AND scoutId = ?",
            (event_id, scout_id)).fetchone()
        self.assertEqual(kind, "scout_answer_started")
        self.assertEqual(json.loads(payload)["message"], asked)
        (message,) = thread.messages(self.conn, scout_id)
        self.assertEqual((message.consumed_by, message.consumed_at),
                         (event_id, before.heartbeat_at + 1000))
        self.refused(lambda: thread.take_message(self.conn, asked), asked)
        self.assertEqual(thread.waiting_messages(self.conn), [])

    def test_two_connections_competing_for_one_ask_take_it_once(self):
        scout_id = self.scout_in("done")
        asked = self.ask(scout_id)
        start, outcomes = threading.Barrier(2), {}

        def take(n):
            conn = store.open(self.path)
            try:
                start.wait()
                outcomes[n] = thread.take_message(conn, asked, host=f"host-{n}")
            except Exception as exc:  # noqa: BLE001 - the loser's error is asserted
                outcomes[n] = exc
            finally:
                conn.close()

        takers = [threading.Thread(target=take, args=(n,)) for n in range(2)]
        for taker in takers:
            taker.start()
        for taker in takers:
            taker.join(timeout=30)

        self.assertEqual(len(outcomes), 2, outcomes)
        refused = [out for out in outcomes.values() if isinstance(out, Exception)]
        self.assertEqual(len(refused), 1, outcomes)
        self.assertIsInstance(refused[0], ThreadRefused)
        (event_id,) = [out for out in outcomes.values() if out not in refused]
        started = self.conn.execute(
            "SELECT id FROM runEvents WHERE scoutId = ? AND kind = ?",
            (scout_id, "scout_answer_started")).fetchall()
        self.assertEqual(started, [(event_id,)])
        self.assertEqual(thread.messages(self.conn, scout_id)[0].consumed_by,
                         event_id)

    def test_a_steer_starts_the_next_pass_and_returns_with_its_first_end(self):
        scout_id = self.scout_in("swarming")
        for number in (1, 2):
            scouts.record_scout_report(self.conn, scout_id, number, {"n": number})
        scouts.end_scout(self.conn, scout_id, "done", "answered",
                         totals={"tokens": 9}, now=7000)
        first_end = scouts.scout(self.conn, scout_id)
        self.conn.execute("UPDATE scouts SET pid = 77 WHERE id = ?",
                          (scout_id,))
        self.conn.commit()
        steer = thread.post_message(self.conn, scout_id, "maintainer",
                                    "steer", "check the cache too")

        event_id = thread.take_message(self.conn, steer, host="host-h")

        row = scouts.scout(self.conn, scout_id)
        self.assertEqual((row.phase, row.pid), ("planning", None))
        kind, payload = self.conn.execute(
            "SELECT kind, payload FROM runEvents WHERE id = ?",
            (event_id,)).fetchone()
        self.assertEqual(kind, "scout_pass_started")
        self.assertEqual(json.loads(payload), {"message": steer, "pass": 3})
        for phase in ("swarming", "synthesizing", "done"):
            scouts.move_scout(self.conn, scout_id, phase)
        row = scouts.scout(self.conn, scout_id)
        self.assertEqual((row.phase, row.ended_at, row.reason, row.totals),
                         ("done", first_end.ended_at, "answered", {"tokens": 9}))

        first_run = self.scout_in("swarming")
        scouts.move_scout(self.conn, first_run, "synthesizing")
        with self.assertRaises(ScoutRefused):
            scouts.move_scout(self.conn, first_run, "done")
        self.assertEqual(scouts.scout(self.conn, first_run).phase,
                         "synthesizing")

    def test_an_answer_keeps_its_blocks_and_is_never_taken(self):
        scout_id, asked = self.answering()
        blocks = [{"type": "finding", "pass": 1, "index": 2},
                  {"type": "action", "position": 1}]
        answer = thread.post_message(self.conn, scout_id, "scout", "answer",
                                     "here is why", blocks=blocks,
                                     answers_id=asked)
        message = thread.messages(self.conn, scout_id)[-1]
        self.assertEqual((message.id, message.answers_id, message.blocks),
                         (answer, asked, blocks))

        other, other_ask = self.answering()
        self.refused(lambda: thread.post_message(
            self.conn, scout_id, "scout", "answer", "wrong thread",
            answers_id=other_ask), f"message {other_ask}")
        chart = self.refused(lambda: thread.post_message(
            self.conn, scout_id, "scout", "answer", "a chart",
            blocks=[{"type": "chart"}], answers_id=asked), "chart")
        self.assertEqual(chart.subject, f"scout {scout_id}")
        scouts.move_scout(self.conn, scout_id, "done")
        self.refused(lambda: thread.take_message(self.conn, answer),
                     "an answer is not taken")
        self.assertNotIn(answer,
                         [m.id for m in thread.waiting_messages(self.conn)])

    def test_an_answer_must_name_an_ask_of_its_scout(self):
        scout_id, asked = self.answering()
        answer = thread.post_message(self.conn, scout_id, "scout", "answer",
                                     "because", answers_id=asked)
        unknown = self.refused(lambda: thread.post_message(
            self.conn, scout_id, "scout", "answer", "unanchored"),
            "names its ask")
        self.assertEqual(unknown.subject, f"scout {scout_id}")
        self.refused(lambda: thread.post_message(
            self.conn, scout_id, "scout", "answer", "to an answer",
            answers_id=answer), "it is answer")
        scouts.move_scout(self.conn, scout_id, "done")
        steer = thread.post_message(self.conn, scout_id, "maintainer",
                                    "steer", "check more")
        thread.take_message(self.conn, steer, host="host-h")
        self.refused(lambda: thread.post_message(
            self.conn, scout_id, "scout", "answer", "to a steer",
            answers_id=steer), "it is steer")

    def test_an_answered_scout_returns_to_done_with_its_first_end(self):
        scout_id, _ = self.answering()
        scouts.move_scout(self.conn, scout_id, "done")
        row = scouts.scout(self.conn, scout_id)
        self.assertEqual((row.phase, row.ended_at, row.reason, row.totals),
                         ("done", 7000, "ended done", {"tokens": 7}))
        self.assertIsInstance(self.ask(scout_id, "one more"), int)

        failed = self.scout_in("failed")
        stale = self.conn.execute(
            "INSERT INTO threadMessages (projectId, scoutId, author, mode,"
            " text, at) VALUES (?, ?, 'maintainer', 'ask', 'late', 1)",
            (self.project_id, failed)).lastrowid
        self.conn.commit()
        self.refused(lambda: thread.take_message(self.conn, stale), "failed")

        again, _ = self.answering()
        before = scouts.scout(self.conn, again)
        with self.assertRaises(ScoutRefused):
            scouts.move_scout(self.conn, again, "planning", base_sha="abc")
        self.assertEqual(scouts.scout(self.conn, again), before)


if __name__ == "__main__":
    unittest.main()

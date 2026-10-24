"""The scout tables, their migration and the writers and reads over them."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import store
import store.scouts as scouts
from store.scouts import ScoutRefused

SCOUT_TABLES = ("scouts", "scoutReports", "scoutTasks", "scoutActions")


def open_store(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    path = Path(tmp.name) / "store.db"
    conn = store.open(path)
    test.addCleanup(conn.close)
    return path, conn


class ScoutMigrationTests(unittest.TestCase):
    def test_a_store_one_version_below_gains_empty_scout_tables(self):
        path, conn = open_store(self)
        project = store.ensure_project(conn, "team", "/repos/example")
        ticket = store.mirror_ticket(conn, project, "issue", "KO-1", "title")
        run = store.claim(conn, project, ticket, now=1000)
        store.record_event(conn, run, "first", "one", now=2000)
        store.record_event(conn, run, "second", "two", level="detail",
                           payload='{"k": 1}', now=3000)
        columns = "id, runId, projectId, seq, level, kind, summary, payload, at"
        before = conn.execute(f"SELECT {columns} FROM runEvents WHERE runId = ?"
                              " ORDER BY id", (run,)).fetchall()
        conn.executescript(
            "DROP INDEX runEvents_scoutId;"
            " ALTER TABLE runEvents DROP COLUMN scoutId;"
            " DROP TABLE scoutActions; DROP TABLE scoutTasks;"
            " DROP TABLE scoutReports; DROP TABLE scouts;"
            f" PRAGMA user_version = {store.schema.SCHEMA_VERSION - 1:d};")
        conn.close()

        conn = store.open(path)
        self.addCleanup(conn.close)

        self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                         (store.schema.SCHEMA_VERSION,))
        self.assertEqual(conn.execute(
            f"SELECT {columns} FROM runEvents WHERE runId = ? ORDER BY id",
            (run,)).fetchall(), before)
        self.assertEqual(len(before), 2)
        self.assertEqual(conn.execute(
            "SELECT scoutId FROM runEvents WHERE runId = ?", (run,)).fetchall(),
            [(None,), (None,)])
        for table in SCOUT_TABLES:
            self.assertEqual(conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone(), (0,), table)
        note = json.loads(store.schema.latest_migration_note(conn))
        self.assertEqual(note["readableFrom"], 50)
        self.assertEqual(note["to"], store.schema.SCHEMA_VERSION)


class ScoutCase(unittest.TestCase):
    def setUp(self):
        _, self.conn = open_store(self)
        self.project_id = store.ensure_project(self.conn, "team", "/repos/example")

    def new_scout(self, now=None):
        return scouts.create_scout(self.conn, self.project_id, "why is it slow",
                                   "bug", 30, now=now)

    def planning(self, now=None):
        scout_id = self.new_scout(now)
        scouts.move_scout(self.conn, scout_id, "planning", base_sha="abc123",
                          host="writer", now=5000)
        return scout_id

    def swarming(self):
        scout_id = self.planning()
        scouts.move_scout(self.conn, scout_id, "swarming")
        return scout_id


class ScoutPhaseTests(ScoutCase):
    def test_planning_records_its_start_and_a_second_planning_is_refused(self):
        scout_id = self.new_scout()
        with self.assertRaises(ValueError):
            scouts.move_scout(self.conn, scout_id, "planning")
        self.assertEqual(scouts.scout(self.conn, scout_id).phase, "queued")
        scouts.move_scout(self.conn, scout_id, "planning", base_sha="abc123",
                          now=5000)

        row = scouts.scout(self.conn, scout_id)
        self.assertEqual((row.phase, row.base_sha, row.started_at,
                          row.heartbeat_at), ("planning", "abc123", 5000, 5000))
        with self.assertRaises(ScoutRefused) as refused:
            scouts.move_scout(self.conn, scout_id, "planning", now=9000)
        self.assertIn(f"scout {scout_id}", str(refused.exception))
        self.assertIn("planning", str(refused.exception))
        self.assertEqual((refused.exception.scout_id, refused.exception.phase),
                         (scout_id, "planning"))
        self.assertEqual(scouts.scout(self.conn, scout_id), row)

    def test_moves_follow_the_edges_and_others_change_nothing(self):
        scout_id = self.planning()
        for phase in ("swarming", "synthesizing", "swarming", "synthesizing"):
            scouts.move_scout(self.conn, scout_id, phase)
            self.assertEqual(scouts.scout(self.conn, scout_id).phase, phase)

        queued = self.new_scout()
        for refused_id, phase in ((scout_id, "planning"), (queued, "swarming")):
            before = scouts.scout(self.conn, refused_id)
            with self.subTest(phase=before.phase, to=phase):
                with self.assertRaises(ScoutRefused):
                    scouts.move_scout(self.conn, refused_id, phase)
                self.assertEqual(scouts.scout(self.conn, refused_id), before)

    def test_only_an_unclaimed_planning_scout_moves_back_to_queued(self):
        unclaimed, claimed = self.planning(), self.planning()
        scouts.claim_scout(self.conn, claimed, 4242)
        scouts.move_scout(self.conn, unclaimed, "queued")
        row = scouts.scout(self.conn, unclaimed)
        self.assertEqual((row.phase, row.base_sha, row.started_at, row.host),
                         ("queued", None, None, None))
        before = scouts.scout(self.conn, claimed)
        with self.assertRaises(ScoutRefused):
            scouts.move_scout(self.conn, claimed, "queued")
        self.assertEqual(scouts.scout(self.conn, claimed), before)

    def test_an_ended_scout_refuses_every_further_write(self):
        ended = {}
        for phase in ("done", "failed", "stopped"):
            scout_id = self.swarming()
            scouts.end_scout(self.conn, scout_id, phase, f"ended {phase}",
                             now=7000)
            ended[scout_id] = phase

        for scout_id, phase in ended.items():
            row = scouts.scout(self.conn, scout_id)
            self.assertEqual((row.phase, row.reason, row.ended_at),
                             (phase, f"ended {phase}", 7000))
            writes = (
                lambda: scouts.end_scout(self.conn, scout_id, "done", "again"),
                lambda: scouts.move_scout(self.conn, scout_id, "synthesizing"),
                lambda: scouts.beat_scout(self.conn, scout_id, pid=42))
            for write in writes:
                with self.subTest(phase=phase), self.assertRaises(ScoutRefused):
                    write()
            self.assertEqual(scouts.scout(self.conn, scout_id), row)
        with self.assertRaises(ValueError):
            scouts.end_scout(self.conn, self.swarming(), "queued", "back")


class ScoutRecordTests(ScoutCase):
    def test_one_report_per_pass_and_positions_count_across_passes(self):
        scout_id = self.planning()
        scouts.record_scout_report(self.conn, scout_id, 1, {"summary": "first"})
        with self.assertRaises(ScoutRefused):
            scouts.record_scout_report(self.conn, scout_id, 1,
                                       {"summary": "second"})
        self.assertEqual(self.conn.execute(
            "SELECT pass, report FROM scoutReports WHERE scoutId = ?",
            (scout_id,)).fetchall(), [(1, '{"summary": "first"}')])

        scouts.record_scout_actions(self.conn, scout_id, 1,
                                    [{"title": "a"}, {"title": "b"}])
        scouts.record_scout_actions(self.conn, scout_id, 2, [{"title": "c"}])
        self.assertEqual(self.conn.execute(
            "SELECT pass, position, item FROM scoutActions WHERE scoutId = ?"
            " ORDER BY id", (scout_id,)).fetchall(),
            [(1, 1, '{"title": "a"}'), (1, 2, '{"title": "b"}'),
             (2, 3, '{"title": "c"}')])

    def test_an_action_item_is_filed_once(self):
        scout_id = self.planning()
        scouts.record_scout_actions(self.conn, scout_id, 1,
                                    [{"title": "a"}, {"title": "b"}])
        scouts.file_scout_action(self.conn, scout_id, 2, "KEY-1", now=8000)
        with self.assertRaises(ScoutRefused):
            scouts.file_scout_action(self.conn, scout_id, 2, "KEY-2")
        self.assertEqual(self.conn.execute(
            "SELECT position, ticketKey, filedAt FROM scoutActions"
            " WHERE scoutId = ? ORDER BY position", (scout_id,)).fetchall(),
            [(1, None, None), (2, "KEY-1", 8000)])

    def test_a_missing_or_blank_ticket_key_files_nothing(self):
        scout_id = self.planning()
        scouts.record_scout_actions(self.conn, scout_id, 1, [{"title": "a"}])
        for key in (None, "", "  "):
            with self.assertRaises(ValueError):
                scouts.file_scout_action(self.conn, scout_id, 1, key)
        scouts.file_scout_action(self.conn, scout_id, 1, "KEY-1", now=8000)
        self.assertEqual(self.conn.execute(
            "SELECT ticketKey, filedAt FROM scoutActions WHERE scoutId = ?",
            (scout_id,)).fetchall(), [("KEY-1", 8000)])

    def test_a_task_runs_from_queued_to_stopped_once(self):
        scout_id = self.swarming()
        first, second = scouts.record_scout_tasks(
            self.conn, scout_id, 1, 1,
            [{"question": "where", "paths": ["a.py", "b/c.py"], "mode": "read"},
             {"question": "why", "mode": "read"}])
        self.assertEqual(self.conn.execute(
            "SELECT id, pass, wave, phase, paths FROM scoutTasks ORDER BY id"
        ).fetchall(), [(first, 1, 1, "queued", '["a.py", "b/c.py"]'),
                       (second, 1, 1, "queued", "[]")])

        scouts.start_scout_task(self.conn, first, now=1000)
        scouts.end_scout_task(self.conn, first, "stopped", tokens=321,
                              excerpt="found it", now=1750)
        self.assertEqual(self.conn.execute(
            "SELECT phase, startedAt, endedAt, durationMs, tokens, excerpt"
            " FROM scoutTasks WHERE id = ?", (first,)).fetchone(),
            ("stopped", 1000, 1750, 750, 321, "found it"))
        with self.assertRaises(ScoutRefused):
            scouts.end_scout_task(self.conn, first, "done")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "CHECK"):
            scouts.record_scout_tasks(self.conn, scout_id, 1, 2,
                                      [{"question": "edit", "mode": "write"}])

    def test_a_queued_task_cannot_end_without_running(self):
        scout_id = self.swarming()
        (queued,) = scouts.record_scout_tasks(
            self.conn, scout_id, 1, 1, [{"question": "where", "mode": "read"}])
        for phase in ("done", "failed", "stopped"):
            with self.subTest(phase=phase), self.assertRaises(ScoutRefused):
                scouts.end_scout_task(self.conn, queued, phase, tokens=5,
                                      now=1750)
        self.assertEqual(self.conn.execute(
            "SELECT phase, startedAt, endedAt, durationMs, tokens"
            " FROM scoutTasks WHERE id = ?", (queued,)).fetchone(),
            ("queued", None, None, None, None))

    def test_an_ended_scout_refuses_task_ends_events_and_filing(self):
        scout_id = self.swarming()
        (task,) = scouts.record_scout_tasks(
            self.conn, scout_id, 1, 1, [{"question": "where", "mode": "read"}])
        scouts.start_scout_task(self.conn, task, now=1000)
        scouts.record_scout_actions(self.conn, scout_id, 1, [{"title": "a"}])
        scouts.end_scout(self.conn, scout_id, "done", "answered")

        with self.assertRaises(ScoutRefused):
            scouts.end_scout_task(self.conn, task, "done", now=2000)
        with self.assertRaises(ScoutRefused):
            scouts.record_scout_event(self.conn, scout_id, "late", "after")
        with self.assertRaises(ScoutRefused):
            scouts.file_scout_action(self.conn, scout_id, 1, "KEY-1")
        self.assertEqual(self.conn.execute(
            "SELECT phase, endedAt FROM scoutTasks WHERE id = ?",
            (task,)).fetchone(), ("running", None))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM runEvents WHERE scoutId = ?",
            (scout_id,)).fetchone(), (0,))
        self.assertEqual(self.conn.execute(
            "SELECT ticketKey, filedAt FROM scoutActions WHERE scoutId = ?",
            (scout_id,)).fetchone(), (None, None))

    def test_events_count_per_scout_and_scouts_read_by_phase_oldest_first(self):
        newer = self.new_scout(now=200)
        older = self.planning(now=100)
        scouts.move_scout(self.conn, self.planning(now=50), "swarming")
        scouts.record_scout_event(self.conn, newer, "planned", "one")
        scouts.record_scout_event(self.conn, older, "planned", "one")
        scouts.record_scout_event(self.conn, newer, "swarmed", "two")

        self.assertEqual(self.conn.execute(
            "SELECT runId, projectId, scoutId, seq, summary FROM runEvents"
            " WHERE scoutId IS NOT NULL ORDER BY id").fetchall(),
            [(None, self.project_id, newer, 1, "one"),
             (None, self.project_id, older, 1, "one"),
             (None, self.project_id, newer, 2, "two")])
        self.assertEqual(
            [(row.id, row.phase)
             for row in scouts.scouts(self.conn, ("queued", "planning"))],
            [(older, "planning"), (newer, "queued")])


if __name__ == "__main__":
    unittest.main()

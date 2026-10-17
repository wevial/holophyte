import unittest
from unittest.mock import patch

import store
import store.tickets
from tests.serve_fixture import ServeTestCase


class HoldsTests(ServeTestCase):
    def seed_changes(self):
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.ensure_project(conn, "team-1", self.target)
        with patch("store.operate.time.time", return_value=1000):
            with store.operate.acting_as("operator"):
                first = store.hold(conn, project, "maintenance")
        with patch("store.operate.time.time", return_value=2000):
            with store.operate.acting_as("supervisor"):
                second = store.release_hold(conn, project, "ready")
        with patch("store.operate.time.time", return_value=3000):
            with store.operate.acting_as("maintainer"):
                third = store.hold(conn, project, "schema bump")
        other = store.ensure_project(conn, "team-2", self.root / "other")
        store.hold(conn, other, "another project")
        store.record_project_intervention(conn, "migrate", "schema updated",
                                          project_id=project)
        self.expected = [
            {"id": third, "at": 3000000, "action": "hold",
             "note": "schema bump", "actor": "maintainer"},
            {"id": second, "at": 2000000, "action": "release_hold",
             "note": "ready", "actor": "supervisor"},
            {"id": first, "at": 1000000, "action": "hold",
             "note": "maintenance", "actor": "operator"}]
        return conn, project

    def test_changes_keep_recorded_fields_newest_first_for_only_this_project(self):
        self.seed_changes()
        self.start()
        code, _, body = self.request("GET", "/holds")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"admission": "held", "note": "schema bump",
                                "rows": self.expected, "limit": 20,
                                "truncated": False})

    def test_limit_truncates_caps_and_rejects_invalid_input(self):
        self.seed_changes()
        self.start()
        code, _, body = self.request("GET", "/holds?limit=2")
        self.assertEqual(code, 200)
        self.assertEqual(body["rows"], self.expected[:2])
        self.assertEqual((body["limit"], body["truncated"]), (2, True))
        code, _, body = self.request("GET", "/holds?limit=999")
        self.assertEqual((code, body["limit"], body["truncated"]),
                         (200, 200, False))
        for limit in ("abc", "0", "-1", ""):
            with self.subTest(limit=limit):
                code, _, body = self.request("GET", f"/holds?limit={limit}")
                self.assertEqual(code, 400)
                self.assertIn("limit", body["error"])

    def test_never_held_project_with_or_without_a_row_is_enabled(self):
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.init(conn)
        self.start()
        for has_row in (False, True):
            with self.subTest(has_row=has_row):
                if has_row:
                    store.ensure_project(conn, "team-1", self.target)
                code, _, body = self.request("GET", "/holds")
                self.assertEqual(code, 200)
                self.assertEqual(body, {"admission": "enabled", "note": None,
                                        "rows": [], "limit": 20,
                                        "truncated": False})

    def test_stop_orders_equal_times_by_id_and_preserves_unknown_actor(self):
        conn, project = self.seed_changes()
        with patch("store.operate.time.time", return_value=3000):
            stopped = store.tickets.set_admission(conn, project, "disabled", "stop")
        conn.execute("UPDATE interventions SET actor = NULL WHERE id = ?",
                     (stopped,))
        conn.commit()
        self.start()
        code, _, body = self.request("GET", "/holds")
        self.assertEqual(code, 200)
        self.assertEqual((body["admission"], body["note"]), ("disabled", "stop"))
        self.assertEqual(body["rows"], [
            {"id": stopped, "at": 3000000, "action": "disable",
             "note": "stop", "actor": None}, *self.expected])

    def test_no_store_returns_unavailable(self):
        self.start()
        code, _, body = self.request("GET", "/holds")
        self.assertEqual(code, 503)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()

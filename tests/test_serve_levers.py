"""`POST /actions/hold`, `/release-hold`, `/pause` and `/resume`
(`holophyte.serve_levers`, KO-609): the CLI's levers behind the daemon's
action token, each recording who and why, and `--resume` requiring a note.

Run: python3 -m unittest discover -s tests -p 'test_serve_levers*' -v
"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_serve  # noqa: E402 - after the insert; TokenTests' TOKEN and BEARER
from serve_fixture import ServeTestCase  # noqa: E402 - after the insert

import holophyte.cli  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
from holophyte.stop import stop_if_requested  # noqa: E402

LEVER_ROUTES = ("hold", "release-hold", "pause", "resume")


class LeverTests(ServeTestCase):
    TOKEN = test_serve.TokenTests.TOKEN
    BEARER = test_serve.TokenTests.BEARER

    def setUp(self):
        super().setUp()
        self.seed()

    def start_actions(self, on=True):
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{path}"\n'
                   + ("actions = true\n" if on else ""))

    def post(self, action, **body):
        return self.request("POST", f"/actions/{action}", self.BEARER,
                            body=body)

    def dump(self):
        conn = store.read.open_readonly(self.db)
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    def rows(self, sql, *params):
        conn = store.read.open_readonly(self.db)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def pause_run(self, note="reboot the writer"):
        """End the seeded run `paused` the way a worker does at a boundary."""
        conn = store.open(str(self.db))
        try:
            store.pause(conn, self.run, note)
            with self.assertRaises(store.RunEnded):
                stop_if_requested(conn, self.run, "working")
        finally:
            conn.close()

    def test_hold_and_release_record_who_and_why_then_a_second_hold_refuses(self):
        self.start_actions()
        code, _, body = self.post("hold", note="deploying", author="ko")
        self.assertEqual((code, body["ok"]), (200, True), body)
        self.assertEqual(self.rows("SELECT admission, holdNote FROM projects"),
                         [("held", "ko via the console: deploying")])

        code, _, body = self.post("hold", note="again")
        self.assertEqual((code, body["ok"]), (200, False))
        self.assertIn("ko via the console: deploying", body["detail"])

        code, _, body = self.post("release-hold", note="deployed")
        self.assertEqual((code, body["ok"]), (200, True), body)
        self.assertEqual(self.rows("SELECT admission, holdNote FROM projects"),
                         [("enabled", None)])
        self.assertEqual(self.rows(
            "SELECT action, note FROM interventions"
            " WHERE action IN ('hold', 'release_hold') ORDER BY id"),
            [("hold", "ko via the console: deploying"),
             ("release_hold", "maintainer via the console: deployed")])

    def test_pause_marks_a_live_run_and_refuses_an_ended_one(self):
        self.start_actions()
        code, _, body = self.post("pause", run=self.run, note="reboot",
                                  author="ko")
        self.assertEqual((code, body["ok"]), (200, True), body)
        self.assertEqual(self.rows(
            "SELECT i.action, i.guidance FROM runs r"
            " JOIN interventions i ON i.id = r.stopRequested WHERE r.id = ?",
            self.run), [("pause", "ko via the console: reboot")])

        conn = store.open(str(self.db))
        try:
            store.release(conn, self.run, "failed", reason="verify red")
        finally:
            conn.close()
        before = self.dump()
        code, _, body = self.post("pause", run=self.run, note="again")
        self.assertEqual((code, body["ok"]), (200, False))
        self.assertIn("failed", body["detail"])
        self.assertEqual(self.dump(), before)

    def test_resume_readies_a_paused_ticket_and_the_cli_demands_a_note(self):
        self.pause_run()
        self.start_actions()
        code, _, body = self.post("resume", ticket="KO-7", note="writer back",
                                  author="ko")
        self.assertEqual((code, body["ok"]), (200, True), body)
        self.assertEqual(self.rows("SELECT status FROM tickets"), [("ready",)])
        self.assertEqual(self.rows(
            "SELECT note FROM interventions WHERE action = 'resume'"),
            [("ko via the console: writer back",)])

        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            holophyte.cli.cli([str(self.target), "--resume", "KO-7"])
        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("--note", err.getvalue())

    def test_a_missing_or_blank_note_is_400_and_writes_nothing(self):
        self.pause_run()
        self.start_actions()
        before = self.dump()
        fields = {"pause": {"run": self.run}, "resume": {"ticket": "KO-7"}}
        for action in LEVER_ROUTES:
            for note in (None, "  "):
                with self.subTest(action=action, note=note):
                    body = dict(fields.get(action, {}))
                    if note is not None:
                        body["note"] = note
                    code, _, answer = self.post(action, **body)
                    self.assertEqual(code, 400, answer)
        self.assertEqual(self.dump(), before)

    def test_every_lever_is_404_with_actions_off(self):
        self.start_actions(on=False)
        before = self.dump()
        for action in LEVER_ROUTES:
            with self.subTest(action=action):
                code, _, _ = self.post(action, note="why", run=self.run,
                                       ticket="KO-7")
                self.assertEqual(code, 404)
        self.assertEqual(self.dump(), before)


if __name__ == "__main__":
    unittest.main()

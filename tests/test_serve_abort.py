"""`POST /actions/abort` (`holophyte.serve_levers`, KO-612): the CLI's
`--abort` behind the daemon's action token, recording who and why, with
`close` choosing whether the pull request is closed too.

Run: python3 -m unittest tests.test_serve_abort -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_serve  # noqa: E402 - after the insert; TokenTests' TOKEN and BEARER
from serve_fixture import ServeTestCase  # noqa: E402 - after the insert

import holophyte.serve_levers  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.read  # noqa: E402 - after the sys.path insert above
from holophyte.stop import abort_run  # noqa: E402

BOARD = '[board]\nteam = "team-1"\nproject_id = "project-1"\n'


class AbortRouteTests(ServeTestCase):
    TOKEN = test_serve.TokenTests.TOKEN
    BEARER = test_serve.TokenTests.BEARER

    def setUp(self):
        super().setUp()
        self.seed()
        path = self.root / "serve.token"
        path.write_text(self.TOKEN + "\n")
        path.chmod(0o600)
        self.start(f'{BOARD}[serve]\ntoken_file = "{path}"\nactions = true\n')

    def post(self, **body):
        return self.request("POST", "/actions/abort", self.BEARER, body=body)

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

    def test_abort_and_close_records_abort_close_with_the_reason(self):
        with patch.object(holophyte.serve_levers, "abort_run",
                          wraps=abort_run) as called:
            code, _, body = self.post(run=self.run, note="wrong approach",
                                      author="ko", close=True)
        self.assertEqual((code, body["ok"]), (200, True), body)
        reason = "ko via the console: wrong approach"
        self.assertEqual(called.call_count, 1)
        self.assertEqual(called.call_args.args[2:], (self.run, reason))
        self.assertIs(called.call_args.kwargs["close"], True)
        self.assertEqual(self.rows(
            "SELECT i.action, i.guidance FROM runs r"
            " JOIN interventions i ON i.id = r.stopRequested WHERE r.id = ?",
            self.run), [("abort_close", reason)])

    def test_an_ended_run_is_refused_and_a_missing_note_is_400(self):
        conn = store.open(str(self.db))
        try:
            store.release(conn, self.run, "failed", reason="verify red")
        finally:
            conn.close()
        before = self.dump()
        code, _, body = self.post(run=self.run, note="stop it")
        self.assertEqual((code, body["ok"]), (200, False), body)
        self.assertIn("failed", body["detail"])
        code, _, body = self.post(run=self.run, close=True)
        self.assertEqual(code, 400, body)
        self.assertEqual(self.dump(), before)


if __name__ == "__main__":
    unittest.main()

"""A real edit whose implementer requests a cooperative stop before returning."""
from pathlib import Path
from unittest.mock import patch

from fake_agent import IMPLEMENT, REQUEST_CHANGES, Commit

import holophyte.loop
import store


class PauseEdit:
    role = IMPLEMENT

    def __init__(self, db):
        self.db = db

    def play(self, cwd, turn):
        Path(cwd, "pause-work.txt").write_text("preserve this uncommitted work\n")
        conn = store.open(self.db)
        try:
            run = conn.execute("SELECT id FROM runs WHERE endedAt IS NULL"
                               " ORDER BY id DESC LIMIT 1").fetchone()[0]
            store.pause(conn, run, "reboot writer")
        finally:
            conn.close()
        return "edit left ready for pause"


class PauseReply:
    from fake_agent import REVIEW_ROLES as role

    def __init__(self, db, reply):
        self.db, self.reply = db, reply

    def play(self, cwd, turn):
        conn = store.open(self.db)
        try:
            run = conn.execute("SELECT id FROM runs WHERE endedAt IS NULL"
                               " ORDER BY id DESC LIMIT 1").fetchone()[0]
            store.pause(conn, run, "review checkpoint")
        finally:
            conn.close()
        return self.reply.text


class PauseFailureCases:
    def test_pause_after_failed_terminal_verification_resumes_its_result(self):
        from holophyte.stop import command
        calls = 0
        def verify(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                store.pause(kwargs["conn"], kwargs["run_id"], "inspect failed verify")
                return False, "terminal check failed"
            return True, "ok"
        with patch.object(holophyte.loop, "run_verify", side_effect=verify):
            self.loop(Commit(), REQUEST_CHANGES, Commit(), REQUEST_CHANGES, Commit())
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "reviewing")])
        command(self.tgt, "KO-131", None, resume=True)
        with patch.object(holophyte.loop, "run_verify") as verify_again:
            self.loop()
        verify_again.assert_not_called()
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs ORDER BY id"),
                         [("paused", None), ("failed", "verify")])

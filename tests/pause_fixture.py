"""A real edit whose implementer requests a cooperative stop before returning."""
from pathlib import Path

from fake_agent import IMPLEMENT

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

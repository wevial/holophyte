"""In store mode a board cancel reaches a live run (KO-741): the host
sweep's observation aborts it with source `supervisor` and trigger
`linear_cancelled`, the run ends `abandoned` with its work kept on the
branch, and the ticket is walked `abandoned` with no question."""
import contextlib
import io
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from abort_fixture import AbortEdit  # noqa: E402
from loop_fixture import BRANCH, LoopFixture  # noqa: E402
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

import store  # noqa: E402
from holophyte.board_sync import observe_board  # noqa: E402
from provider import FileProvider  # noqa: E402

STORE_MODE = ('[board]\nmode = "store"\nproject_id = "project-1"\n'
              'team = "team-1"\n')
NOTE = "{} was canceled on the board"


def a_board(root, identifier):
    """A file board holding `identifier` in the Canceled state."""
    root.mkdir()
    (root / f"{identifier}.md").write_text(f"# {identifier}\n")
    (root / f"{identifier}.state").write_text("Canceled\n")
    return FileProvider(root)


class CancelMidTurn(AbortEdit):
    """An implementer turn that leaves an edit uncommitted while the host
    sweep's observation sees its ticket canceled on the board."""

    def __init__(self, test, board):
        super().__init__(test.db)
        self.test, self.board, self.out = test, board, io.StringIO()

    def play(self, cwd, turn):
        Path(cwd, "abort-work.txt").write_text("keep this edit\n")
        conn = store.open(str(self.db))
        try:
            (project,) = conn.execute("SELECT id FROM projects").fetchone()
            observe_board(self.test.project, conn, project, self.board,
                          int(time.time() * 1000), self.out, {}, MINUTE)
        finally:
            conn.close()
        return "edit left for the cancel"


class CancelLiveRunTests(LoopFixture):
    def test_a_canceled_ticket_ends_its_live_run_abandoned_with_its_work(self):
        self.configure(STORE_MODE)
        turn = CancelMidTurn(self, a_board(self.target.parent / "board",
                                           "KO-131"))
        with patch.object(sys, "stdout", io.StringIO()):
            self.loop(turn)
        note = NOTE.format("KO-131")
        self.assertEqual(self.read("SELECT outcome, outcomeReason FROM runs"),
                         [("abandoned", note)])
        self.assertEqual(self.read("SELECT status, blockedQuestion FROM tickets"),
                         [("abandoned", None)])
        self.assertIn(BRANCH, self.branches())
        self.assertEqual(self.subjects(BRANCH)[0],
                         "WIP: preserve work at operator abort")
        self.assertEqual(self.git("show", f"{BRANCH}:abort-work.txt"),
                         "keep this edit\n")
        self.assertEqual(self.read(
            'SELECT source, "trigger" FROM interventions'
            " WHERE action = 'abort'"),
            [("supervisor", "linear_cancelled")])


class CancelGoneWorkerTests(SweepTestCase):
    def test_a_gone_workers_canceled_run_is_ended_in_the_same_pass(self):
        self.configure(STORE_MODE)
        run = self.a_run()
        ticket = self.ticket_of[run]
        dead = subprocess.Popen(["true"])
        dead.wait()
        with self.conn:
            self.conn.execute("UPDATE runs SET workerPid = ? WHERE id = ?",
                              (dead.pid, run))
        board = a_board(self.root / "board", "KO-1")
        out = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()):
            observe_board(self.project, self.conn, self.project_id, board,
                          T0 + MINUTE, out, {}, MINUTE)
        self.assertEqual(self.conn.execute(
            "SELECT outcome, outcomeReason FROM runs WHERE id = ?",
            (run,)).fetchone(), ("abandoned", NOTE.format("KO-1")))
        self.assertEqual(self.conn.execute(
            "SELECT status, blockedQuestion FROM tickets WHERE id = ?",
            (ticket,)).fetchone(), ("abandoned", None))

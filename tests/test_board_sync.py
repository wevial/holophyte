"""In store mode the host sweep asks the board the state of every open
ticket, records its column, and retires an issue seen gone twice (KO-739):
`observe_board()` on a real store with a file board."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

import store  # noqa: E402
from holophyte.board_sync import observe_board  # noqa: E402
from provider import FileProvider  # noqa: E402

ASK = 10 * MINUTE
STORE_MODE = ('[board]\nmode = "store"\nproject_id = "project-1"\n'
              'team = "team-1"\n')


class CountingBoard(FileProvider):
    """The file board, counting its `states()` asks."""

    def __init__(self, root):
        super().__init__(root)
        self.asks = 0

    def states(self, identifiers):
        self.asks += 1
        return super().states(identifiers)


class RefusingBoard(FileProvider):
    def states(self, identifiers):
        raise RuntimeError("the board is unreachable")


class ObserveBoardTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE)
        self.asked = {}
        # KO-1 idle: its last run ended and the ticket stayed in flight.
        self.run1 = self.a_run()
        store.release(self.conn, self.run1, "failed", "crashed")
        self.ko1 = self.ticket_of[self.run1]
        # KO-2 in flight under a live run.
        self.run2 = self.a_run()
        self.ko2 = self.ticket_of[self.run2]
        self.files = self.root / "board"
        self.files.mkdir()
        for identifier in ("KO-1", "KO-2"):
            (self.files / f"{identifier}.md").write_text(f"# {identifier}\n")

    def observe(self, board, at):
        out = io.StringIO()
        observe_board(self.project, self.conn, self.project_id, board, at,
                      out, self.asked, ASK)
        return out.getvalue()

    def row(self, ticket, columns):
        return self.conn.execute(f"SELECT {columns} FROM tickets WHERE id = ?",
                                 (ticket,)).fetchone()

    def latest_revision(self, ticket):
        return self.conn.execute(
            "SELECT revision, author, boardColumn FROM ticketRevisions"
            " WHERE ticketId = ? ORDER BY revision DESC LIMIT 1",
            (ticket,)).fetchone()

    def test_state_and_column_are_recorded_and_asked_once_per_ask_interval(self):
        board = CountingBoard(self.files)
        self.observe(board, T0 + MINUTE)
        for ticket in (self.ko1, self.ko2):
            self.assertEqual(self.row(ticket, "boardState, boardColumn"),
                             ("Todo", "ready"))
        before, _, _ = self.latest_revision(self.ko1)

        (self.files / "KO-1.state").write_text("Backlog\n")
        self.observe(board, T0 + MINUTE + ASK)
        self.assertEqual(self.row(self.ko1, "boardState, boardColumn"),
                         ("Backlog", "backlog"))
        revision, author, column = self.latest_revision(self.ko1)
        self.assertEqual((author, column), ("board", "backlog"))
        self.assertGreater(revision, before)
        self.assertEqual(self.row(self.ko1, "revision"), (revision,))

        self.observe(board, T0 + 2 * MINUTE + ASK)
        self.assertEqual(board.asks, 2)

    def test_gone_is_stamped_cleared_and_retires_on_a_second_sighting(self):
        board = CountingBoard(self.files)
        ko1 = self.files / "KO-1.md"
        ko1.unlink()
        self.observe(board, T0 + MINUTE)
        self.assertEqual(self.row(self.ko1, "goneSince, status"),
                         (T0 + MINUTE, "in_flight"))

        ko1.write_text("# KO-1\n")
        self.observe(board, T0 + MINUTE + ASK)
        self.assertEqual(self.row(self.ko1, "goneSince"), (None,))

        ko1.unlink()
        (self.files / "KO-2.md").unlink()
        self.observe(board, T0 + MINUTE + 2 * ASK)
        self.assertEqual(self.row(self.ko1, "status"), ("in_flight",))
        out = self.observe(board, T0 + MINUTE + 3 * ASK)

        self.assertEqual(self.row(self.ko1, "status"), ("abandoned",))
        self.assertEqual(self.conn.execute(
            "SELECT runId, source FROM interventions WHERE action = 'reconcile'"
        ).fetchall(), [(self.run1, "supervisor")])
        self.assertIn("reconciled KO-1: in_flight -> abandoned", out)
        self.assertEqual(self.conn.execute(
            "SELECT i.action, i.guidance, i.source FROM runs r"
            " JOIN interventions i ON i.id = r.stopRequested WHERE r.id = ?",
            (self.run2,)).fetchall(),
            [("pause", "the board no longer has KO-2", "supervisor")])
        self.assertEqual(self.row(self.ko2, "status"), ("in_flight",))

    def test_an_unaskable_board_writes_nothing_and_mirror_mode_asks_nothing(
            self):
        store.set_gone_since(self.conn, self.ko1, T0)
        before = self.conn.execute("SELECT * FROM tickets").fetchall()
        decisions = self.conn.execute("SELECT * FROM interventions").fetchall()
        refusing = RefusingBoard(self.files)
        first = self.observe(refusing, T0 + ASK)
        self.observe(refusing, T0 + 2 * ASK)
        self.assertIn("the board is unreachable", first)
        self.assertEqual(self.conn.execute("SELECT * FROM tickets").fetchall(),
                         before)
        self.assertEqual(self.conn.execute(
            "SELECT * FROM interventions").fetchall(), decisions)

        self.configure('[board]\nmode = "mirror"\nproject_id = "project-1"\n'
                       'team = "team-1"\n')
        self.asked.clear()
        board = CountingBoard(self.files)
        self.observe(board, T0 + 3 * ASK)
        self.assertEqual(board.asks, 0)

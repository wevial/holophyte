"""In store mode a status push is queued on the ticket and the host sweep's
observation delivers it, so a person's later move stands (KO-740):
`mirror_status()` and `observe_board()` on a real store with a file board,
and `board_for()` reading the mode."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board import mirror_status  # noqa: E402
from holophyte.board_sync import observe_board  # noqa: E402
from provider import FileProvider, board_for  # noqa: E402

ASK = 10 * MINUTE
STORE_MODE = ('[board]\nmode = "store"\nproject_id = "project-1"\n'
              'team = "team-1"\n')


class RecordingBoard(FileProvider):
    """The file board, recording each `set_state()` it is asked."""

    def __init__(self, root, store_mode):
        super().__init__(root)
        self.store_mode = store_mode
        self.pushed = []

    def set_state(self, issue_id, state_name):
        self.pushed.append((issue_id, state_name))
        super().set_state(issue_id, state_name)


class LostResponseBoard(RecordingBoard):
    """Applies the state, then raises, as a response lost on the way back."""

    def set_state(self, issue_id, state_name):
        super().set_state(issue_id, state_name)
        raise TimeoutError("the response was lost")


class QueuedPushTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE)
        self.files = self.root / "board"
        self.files.mkdir()
        self.ticket = self.a_ready_ticket("KO-1")

    def a_ready_ticket(self, identifier):
        """A `ready` ticket on the file board, observed in Todo."""
        (self.files / f"{identifier}.md").write_text(f"# {identifier}\n")
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=identifier,
            linear_identifier=identifier, title=identifier,
            acceptance_criteria=["Given the ticket, then it is worked"],
            verification_commands=["echo ok"], board_state="Todo")

    def push_row(self, ticket=None):
        return self.conn.execute(
            "SELECT pushState, pushFrom, pushAt, boardState FROM tickets"
            " WHERE id = ?", (ticket or self.ticket,)).fetchone()

    def observe(self, board, at):
        out = io.StringIO()
        observe_board(self.project, self.conn, self.project_id, board, at,
                      out, {}, ASK)
        return out.getvalue()

    def queue_in_progress(self):
        board = RecordingBoard(self.files, store_mode=True)
        self.assertTrue(mirror_status(self.conn, self.ticket, "in_flight",
                                      board))
        return board

    def test_store_mode_queues_the_push_and_mirror_mode_sends_it(self):
        board = self.queue_in_progress()
        state, origin, at, _ = self.push_row()
        self.assertEqual((state, origin), ("In Progress", "Todo"))
        self.assertIsNotNone(at)
        self.assertEqual(board.pushed, [])

        other = self.a_ready_ticket("KO-2")
        mirror = RecordingBoard(self.files, store_mode=False)
        self.assertTrue(mirror_status(self.conn, other, "in_flight", mirror))
        self.assertEqual(mirror.pushed, [("KO-2", "In Progress")])
        self.assertEqual(self.push_row(other)[:3], (None, None, None))

    def test_a_lost_response_waits_and_a_later_move_by_a_person_stands(self):
        self.queue_in_progress()
        board = LostResponseBoard(self.files, store_mode=True)
        out = self.observe(board, T0)
        self.assertEqual(board.pushed, [("KO-1", "In Progress")])
        self.assertIn("the response was lost", out)
        self.assertEqual(self.push_row()[:2], ("In Progress", "Todo"))

        (self.files / "KO-1.state").write_text("Backlog\n")
        self.observe(board, T0 + ASK)
        self.assertEqual(self.push_row()[:2], (None, None))
        self.assertEqual(self.push_row()[3], "Backlog")
        self.assertEqual(len(board.pushed), 1)
        self.assertEqual((self.files / "KO-1.state").read_text(), "Backlog\n")

    def test_a_lost_response_that_landed_is_cleared_without_a_resend(self):
        self.queue_in_progress()
        board = LostResponseBoard(self.files, store_mode=True)
        self.observe(board, T0)
        self.observe(board, T0 + ASK)
        self.assertEqual(self.push_row(), (None, None, None, "In Progress"))
        self.assertEqual(len(board.pushed), 1)


class BoardForModeTests(ConfigTestCase):
    def test_the_board_is_in_store_mode_only_when_the_table_says_so(self):
        board = ('[board]\nproject_id = "p-1"\nteam = "T"\n')
        for mode, expected in (("store", True), ("mirror", False)):
            with self.subTest(mode=mode):
                target = self.locate(board + f'mode = "{mode}"\n')
                self.assertIs(board_for(target).store_mode, expected)

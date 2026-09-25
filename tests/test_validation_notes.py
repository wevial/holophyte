"""In store mode the two admission refusals speak through notes (KO-745):
a body the template validator refuses gets one `validation` note naming
every problem, once per body, and a stale park's comment is a `stale` note
and its Backlog move a queued push. Mirror mode still writes the board."""
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import (  # noqa: E402
    INVALID_BODY,
    LoopFixture,
    StubProvider,
    a_task,
)

import holophyte.claim  # noqa: E402
import holophyte.dispatch  # noqa: E402
import holophyte.runs  # noqa: E402
import store.tickets as tickets  # noqa: E402
from holophyte.freshness import park_stale  # noqa: E402

# Two blocking problems: the Summary placeholder INVALID_BODY carries, and
# no What line.
TWO_PROBLEMS = INVALID_BODY.replace("**What:** Add the thing.\n\n", "")
PLACEHOLDER = "unfilled template placeholder in Summary"
NO_WHAT = "'**What:**' line missing"
GONE = "`holophyte/gone.py` (named in Implementation notes) is not on main"


class StoreModeBoard(StubProvider):
    store_mode = True

    def listing(self):
        return [dict(task, blocked_by=[]) for task in self.queue]


class ValidationNoteTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.conn = holophyte.runs.open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = tickets.ensure_project(
            self.conn, StubProvider.TEAM, str(self.target))

    def mirror(self, provider):
        with patch.object(sys, "stdout", io.StringIO()):
            holophyte.dispatch._mirror_queue(self.project, self.conn,
                                             self.project_id, provider)

    def notes(self, kind):
        return [text for (text,) in self.conn.execute(
            "SELECT n.text FROM ticketNotes n JOIN tickets t"
            " ON t.id = n.ticketId WHERE t.linearIdentifier = 'KO-131'"
            " AND n.kind = ? ORDER BY n.id", (kind,))]

    def status(self):
        return self.conn.execute(
            "SELECT status FROM tickets WHERE linearIdentifier = 'KO-131'"
        ).fetchone()[0]

    def test_a_refused_body_is_noted_once_and_again_when_edited(self):
        task = dict(a_task(), body=TWO_PROBLEMS)
        board = StoreModeBoard(task)
        self.mirror(board)
        self.mirror(board)

        self.assertEqual(self.status(), "needs_spec")
        (note,) = self.notes("validation")
        self.assertIn(PLACEHOLDER, note)
        self.assertIn(NO_WHAT, note)

        task["body"] = TWO_PROBLEMS.replace("The thing is wanted.",
                                            "The thing is still wanted.")
        self.mirror(StoreModeBoard(task))
        self.assertEqual(len(self.notes("validation")), 2)

    def test_mirror_mode_writes_no_note(self):
        self.mirror(StubProvider(dict(a_task(), body=TWO_PROBLEMS)))

        self.assertEqual(self.status(), "needs_spec")
        self.assertEqual(self.notes("validation"), [])

    def test_admission_after_the_mirror_adds_no_second_note(self):
        task = dict(a_task(), body=TWO_PROBLEMS)
        board = StoreModeBoard(task)
        self.mirror(board)

        with patch.object(sys, "stdout", io.StringIO()):
            admitted = holophyte.claim._admit_ticket(
                self.project, self.conn, self.project_id, board, task,
                SimpleNamespace(trips=[], watched=[]))

        self.assertIsNone(admitted)
        self.assertEqual(len(self.notes("validation")), 1)

    def park(self, provider, task):
        with patch.object(sys, "stdout", io.StringIO()):
            park_stale(self.project, self.conn, self.project_id, provider,
                       task, [GONE])

    def test_a_store_mode_stale_park_notes_and_queues_the_move(self):
        task = a_task()
        board = StoreModeBoard(task)
        self.park(board, task)

        (note,) = self.notes("stale")
        self.assertIn(GONE, note)
        self.assertEqual(self.conn.execute(
            "SELECT pushState FROM tickets WHERE linearIdentifier = 'KO-131'"
        ).fetchone()[0], "Backlog")
        self.assertIn("stale", board.labels["iss-131"])
        self.assertEqual((board.comments, board.states), ([], []))

    def test_a_mirror_mode_stale_park_writes_the_board(self):
        task = a_task()
        board = StubProvider(task)
        self.park(board, task)

        self.assertEqual(self.notes("stale"), [])
        self.assertEqual(len(board.comments), 1)
        self.assertIn(GONE, board.comments[0][1])
        self.assertEqual(board.states, [("iss-131", "Backlog")])

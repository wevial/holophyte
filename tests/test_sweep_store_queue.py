"""Phase 3 stage 3: with no loop live, the host sweep's owed tickets are the
store's queue. In store mode the sweep syncs the board's project at most
once a `board_ask_sec`, a `ready` row out of the ready column is owed
nothing, the mirror-mode board fallback (`board_ready()`) is never asked,
and another project row in the store is not synced.

Run: python3 -m unittest discover -s tests -p 'test_sweep_store_queue.py' -v
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import VALID_BODY  # noqa: E402
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board import mirror_status  # noqa: E402
from holophyte.claim_store import NOT_ASKED, SYNCED, sync_board  # noqa: E402
from holophyte.config_tables import sweep_config  # noqa: E402
from holophyte.supervisor import (  # noqa: E402
    fresh_memory,
    reconcile_parked_pull_requests,
)
from provider import FileProvider  # noqa: E402

BOARD = ('[board]\nproject_id = "project-1"\nteam = "team-1"\n'
         '[supervisor]\nboard_ask_sec = 600\n')
STORE_MODE = 'mode = "store"\n'


class CountingFiles(FileProvider):
    """A store-mode file board counting its listings."""

    store_mode = True

    def __init__(self, root):
        super().__init__(root)
        self.listed = 0

    def listing(self):
        self.listed += 1
        return super().listing()


def never(*args, **kwargs):
    raise AssertionError("the mirror-mode board fallback was asked")


class SweepStoreQueueTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.configure(BOARD.replace("team-1\"\n", "team-1\"\n" + STORE_MODE))
        self.memory = fresh_memory()
        files = self.root / "team-1"
        files.mkdir()
        (files / "KO-1.md").write_text(VALID_BODY)
        self.board = CountingFiles(files)

    def reconcile(self, at):
        """One sweep pass at `at`; the pairs a loop start was owed for, or
        None when none was started."""
        started = []
        with patch("holophyte.reconcile._reconcile_pull_requests"), \
                patch("holophyte.supervisor.linear_budget_low",
                      return_value=False), \
                patch("holophyte.supervisor.board_ready", never), \
                patch("holophyte.supervisor.start_loop_for",
                      lambda target, conn, owed, *a, **k: started.append(owed)):
            reconcile_parked_pull_requests(
                self.project, self.conn, at, self.board, io.StringIO(),
                knobs=sweep_config(self.project), memory=self.memory)
        return started[0] if started else None

    def ticket(self, identifier):
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def shelved_row(self):
        """KO-2, `ready` in the store but in the Backlog column."""
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="KO-2",
            linear_identifier="KO-2", title="a shelved thing",
            acceptance_criteria=["Given it, then it works"],
            verification_commands=["echo ok"], board_column="backlog")

    def test_the_queue_is_synced_once_a_board_ask_sec_and_is_what_is_owed(self):
        self.shelved_row()

        owed = self.reconcile(T0)

        self.assertEqual(owed, [(self.ticket("KO-1"), None)])
        self.assertEqual(self.board.listed, 1)
        self.reconcile(T0 + 5 * MINUTE)
        self.assertEqual(self.board.listed, 1)
        self.reconcile(T0 + 10 * MINUTE)
        self.assertEqual(self.board.listed, 2)

    def test_mirror_mode_still_owes_every_ready_row(self):
        self.configure(BOARD)
        shelved = self.shelved_row()

        self.assertEqual(self.reconcile(T0), [(shelved, None)])
        self.assertEqual(self.board.listed, 0)

    def test_an_empty_queue_starts_nothing_and_never_asks_the_fallback(self):
        (self.board.root / "KO-1.md").unlink()

        self.assertIsNone(self.reconcile(T0))
        self.assertEqual(self.board.listed, 1)

    def test_another_project_row_is_not_synced(self):
        other = self.another_project()

        self.reconcile(T0)

        self.assertEqual(self.board.listed, 1)
        self.assertEqual(self.conn.execute(
            "SELECT id, boardAskedAt FROM projects ORDER BY id").fetchall(),
            [(self.project_id, T0), (other, None)])

    def queued_push_and_note(self):
        """KO-1 in the store, observed in Todo, with a push to In Progress
        queued and one note unposted; the note's id."""
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="KO-1",
            linear_identifier="KO-1", title="a thing",
            acceptance_criteria=["Given it, then it works"],
            verification_commands=["echo ok"], board_state="Todo")
        self.assertTrue(mirror_status(self.conn, ticket, "in_flight",
                                      self.board))
        return store.record_note(self.conn, ticket, "ledger", "merged",
                                 dedup_key="merged", now=T0)

    def delivered(self, note):
        """The state the board holds KO-1 in, if a push set one, and
        whether the note was posted."""
        state = self.board.root / "KO-1.state"
        return (state.read_text().strip() if state.exists() else None,
                self.conn.execute(
                    "SELECT postedAt IS NOT NULL FROM ticketNotes"
                    " WHERE id = ?", (note,)).fetchone()[0])

    def test_a_held_project_s_pushes_and_notes_are_delivered(self):
        """KO-771: a hold stops claims, not the board catching up."""
        note = self.queued_push_and_note()
        store.hold(self.conn, self.project_id, "draining")
        self.memory.states_asked[self.project_id] = T0

        self.assertIsNone(self.reconcile(T0 + 10 * MINUTE))

        self.assertEqual(self.delivered(note), ("In Progress", 1))
        self.assertEqual(self.board.listed, 0)

    def test_a_disabled_project_s_pushes_and_notes_wait(self):
        note = self.queued_push_and_note()
        store.tickets.set_admission(self.conn, self.project_id, "disabled",
                                    "retired")
        self.memory.states_asked[self.project_id] = T0

        self.assertIsNone(self.reconcile(T0 + 10 * MINUTE))

        self.assertEqual(self.delivered(note), (None, 0))
        self.assertEqual(self.conn.execute(
            "SELECT pushState FROM tickets WHERE linearIdentifier = 'KO-1'"
        ).fetchone(), ("In Progress",))

    def test_a_held_project_or_a_low_budget_is_neither_asked_nor_stamped(self):
        """A sync that does not ask leaves `boardAskedAt` alone, so the
        next one is not put off an interval for an ask never made."""
        store.hold(self.conn, self.project_id, "draining")
        self.assertEqual(sync_board(self.project, self.conn, self.project_id,
                                    self.board, now=T0), NOT_ASKED)
        store.release_hold(self.conn, self.project_id, "drained")
        with patch("holophyte.supervisor.linear_budget_low",
                   return_value=True):
            self.assertEqual(sync_board(self.project, self.conn,
                                        self.project_id, self.board, now=T0),
                             NOT_ASKED)

        self.assertEqual(self.board.listed, 0)
        self.assertEqual(self.conn.execute(
            "SELECT boardAskedAt FROM projects WHERE id = ?",
            (self.project_id,)).fetchone(), (None,))
        with patch("holophyte.supervisor.linear_budget_low",
                   return_value=False):
            self.assertEqual(sync_board(self.project, self.conn,
                                        self.project_id, self.board, now=T0),
                             SYNCED)

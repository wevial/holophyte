"""Phase 3 stage 3: a store-mode project claims from the store end to end.

The loop runs on the real fixture repository with real git against a file
board in store mode whose `claim_next()` must never be called: the ticket
comes from `store.read.claimable()`, is admitted at its revision, read
back from the board, and claimed only at the revision admission passed.
A board edit made while the critic judges the ticket is admitted again; a
move to Backlog made then is not claimed and parks nothing; another
writer's lease label on the board is skipped.

Run: python3 -m unittest discover -s tests -p 'test_store_claim_loop.py' -v
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit  # noqa: E402
from loop_fixture import INVALID_BODY, VALID_BODY, LoopFixture  # noqa: E402

from holophyte.board import mirror_task  # noqa: E402
from holophyte.claim import _admit_ticket  # noqa: E402
from holophyte.claim_store import task_of  # noqa: E402
from holophyte.freshness import park_stale  # noqa: E402
from holophyte.pool import NOTHING_SEEN  # noqa: E402
from holophyte.runs import open_store  # noqa: E402
from provider import FileProvider  # noqa: E402
from store.read import claimable  # noqa: E402
from store.tickets import ensure_project  # noqa: E402

STORE_MODE = ('[board]\nmode = "store"\nproject_id = "project-1"\n'
              'team = "team-1"\n')
EDITED = "add a thing, edited on the board"


class StoreFiles(FileProvider):
    """The file board in store mode: statuses are queued, not pushed, and
    the board's own claim is never the loop's to call."""

    store_mode = True
    # Set, the board cannot be asked for one issue (the listing still can).
    down = False

    def claim_next(self, skip=(), order="identifier"):
        raise AssertionError("a store-mode loop asked the board to claim")

    def fetch_task(self, issue_id):
        if self.down:
            raise RuntimeError("the board is down")
        return super().fetch_task(issue_id)

    def ready_issues(self):
        return [super(StoreFiles, self).fetch_task(identifier)
                for identifier in self._identifiers()
                if self._state(identifier) == "Todo"]


class StoreClaimLoopTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE)
        self.files = self.target.parent / "team-1"
        self.files.mkdir()
        (self.files / "KO-131.md").write_text(VALID_BODY)
        self.board = StoreFiles(self.files)

    def run_loop(self, critic=None):
        """Run the loop with the critic patched to `critic` (admit by
        default); answer what it printed."""
        calls = []

        def judged(project, conn, project_id, provider, task):
            calls.append(task["title"])
            return (critic(len(calls), project, conn, project_id, provider,
                           task) if critic else True)

        self.critic_calls = calls
        out = io.StringIO()
        with patch("holophyte.freshness.critic_admits", judged), \
                patch.object(sys, "stdout", out):
            self.loop(Commit("the scripted work"), APPROVE, provider=self.board)
        return out.getvalue()

    def test_the_loop_claims_from_the_store_and_merges(self):
        self.run_loop()

        self.assertIn("the scripted work", self.subjects())
        self.assertEqual(self.read(
            "SELECT r.revision, t.revision, t.status, t.pushState FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId"),
            [(1, 1, "merged", "Done")])
        self.assertFalse((self.files / "KO-131.state").exists())

    def test_an_edit_during_admission_is_admitted_again_and_claimed(self):
        def edit_once(n, *_):
            if n == 1:
                (self.files / "KO-131.title").write_text(f"{EDITED}\n")
            return True

        out = self.run_loop(edit_once)

        self.assertEqual(len(self.critic_calls), 2)
        self.assertIn("revision moved from 1 to 2", out)
        ((revision, snapshot),) = self.read(
            "SELECT revision, ticketSnapshot FROM runs")
        self.assertEqual(revision, 2)
        self.assertEqual(json.loads(snapshot)["title"], EDITED)

    def test_a_move_to_backlog_during_admission_is_not_claimed(self):
        def shelve(n, *_):
            (self.files / "KO-131.state").write_text("Backlog\n")
            return True

        self.run_loop(shelve)

        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])
        self.assertEqual(self.read(
            "SELECT status, boardColumn, revision FROM tickets"),
            [("ready", "backlog", 2)])

    def test_another_writers_lease_label_on_the_board_is_skipped(self):
        (self.files / "KO-131.labels").write_text("holo:other-host\n")

        out = self.run_loop()

        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])
        self.assertIn("KO-131 is leased by other-host on the board;"
                      " skipping it", out)

    def test_a_deleted_issue_is_skipped_and_the_next_is_claimed(self):
        (self.files / "KO-132.md").write_text(VALID_BODY)

        def delete_the_first(n, *_):
            if n == 1:
                (self.files / "KO-131.md").unlink()
            return True

        out = self.run_loop(delete_the_first)

        self.assertIn("KO-131 is gone from the board; skipping it", out)
        self.assertEqual(self.read(
            "SELECT t.linearIdentifier, r.outcome FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId"), [("KO-132", "merged")])

    def test_a_board_that_cannot_be_read_back_stops_the_loop_nonzero(self):
        def board_down(n, *_):
            self.board.down = True
            return True

        out = self.run_loop(board_down)

        self.assertIn("KO-131 could not be read back from the board", out)
        self.assertIn("the board could not be read back at the claim;"
                      " stopping", out)
        self.assertNotIn("no ready tickets", out)
        self.assertEqual(self.rc, 1)
        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])

    def test_a_stale_verdict_on_a_superseded_revision_writes_nothing(self):
        """The critic parks the ticket stale while the board edits it: the
        verdict judged the old revision, so no `stale` label, note or
        Backlog push is written, and the new revision is judged again."""
        def stale_while_edited(n, project, conn, project_id, provider, task):
            if n > 1:
                return True
            (self.files / "KO-131.title").write_text(f"{EDITED}\n")
            self.mirror_live(conn, project_id)
            park_stale(project, conn, project_id, provider, task,
                       ["critic: stale \u2014 judged before the edit"],
                       admitted=True)
            return False

        out = self.run_loop(stale_while_edited)

        self.assertIn("KO-131 skipped: it changed on the board while it"
                      " was judged", out)
        self.assertNotIn("stale", (self.files / "KO-131.labels").read_text())
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM ticketNotes WHERE kind = 'stale'"), [(0,)])
        self.assertEqual(self.read(
            "SELECT r.revision, t.status FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId"), [(2, "merged")])

    def mirror_live(self, conn, project_id):
        """The board's edit reaching the store, as a sibling's sync would."""
        mirror_task(conn, project_id, self.board.fetch_task("KO-131"))

    def test_a_validation_refusal_of_a_superseded_revision_notes_nothing(self):
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        project_id = ensure_project(conn, self.board.team, self.target)
        ticket = mirror_task(conn, project_id, self.board.fetch_task("KO-131"))
        (self.files / "KO-131.title").write_text(f"{EDITED}\n")
        self.mirror_live(conn, project_id)
        (row,) = [r for r in claimable(conn, project_id)]
        stale = dict(task_of(row), body=INVALID_BODY, store_revision=1)

        with patch.object(sys, "stdout", io.StringIO()):
            admitted = _admit_ticket(self.project, conn, project_id,
                                     self.board, stale, NOTHING_SEEN)

        self.assertIsNone(admitted)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM ticketNotes")
                         .fetchone(), (0,))
        self.assertEqual(conn.execute(
            "SELECT status, revision FROM tickets WHERE id = ?",
            (ticket,)).fetchone(), ("ready", 2))

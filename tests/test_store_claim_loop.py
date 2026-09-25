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
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

from provider import FileProvider  # noqa: E402

STORE_MODE = ('[board]\nmode = "store"\nproject_id = "project-1"\n'
              'team = "team-1"\n')
EDITED = "add a thing, edited on the board"


class StoreFiles(FileProvider):
    """The file board in store mode: statuses are queued, not pushed, and
    the board's own claim is never the loop's to call."""

    store_mode = True

    def claim_next(self, skip=(), order="identifier"):
        raise AssertionError("a store-mode loop asked the board to claim")


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
            return critic(len(calls)) if critic else True

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
        def edit_once(n):
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
        def shelve(n):
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

"""A merged revert ticket returns the ticket its `Reverts: KEY at SHA` line
names to Backlog, through the loop's real merge recording against a native
board's store and a real git repository; a failed revert run leaves KEY as
it was.

Run: python3 -m unittest discover -s tests -p 'test_revert_close*.py' -v
"""
import contextlib
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from phase_fixture import advance_phase  # noqa: E402 - after the sys.path insert

import store  # noqa: E402
from holophyte.board.provider import board_for  # noqa: E402
from holophyte.loop import dispatch, pipeline  # noqa: E402
from holophyte.loop.gates import RunFailure  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from tests import test_cli_revert as cli_revert  # noqa: E402

REASON = "broke the <header> band"


class RevertCloseTests(ConfigTestCase):
    setUp = cli_revert.RevertCliTests.setUp
    git = cli_revert.RevertCliTests.git
    commit = cli_revert.RevertCliTests.commit
    project_with = cli_revert.RevertCliTests.project_with
    holo = cli_revert.RevertCliTests.holo
    read = cli_revert.RevertCliTests.read

    def file_revert(self):
        self.project_with(cli_revert.ENABLED, {"src/header.py": "broken\n"})
        with contextlib.closing(open_store(self.project)) as conn:
            (ticket_id,) = conn.execute(
                "SELECT id FROM tickets WHERE linearIdentifier = 'NAT-1'"
            ).fetchone()
            store.tickets.transition(conn, ticket_id, "merged")
        code, out, err = self.holo("revert", "NAT-1", REASON)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"Reverts: NAT-1 at {self.key_sha}",
                      self.read("SELECT body FROM tickets WHERE"
                                " linearIdentifier = 'NAT-3'")[0][0])

    def run_revert(self, task):
        with contextlib.closing(open_store(self.project)) as conn:
            (project_id,) = conn.execute("SELECT id FROM projects").fetchone()
            (ticket_id,) = conn.execute(
                "SELECT id FROM tickets WHERE linearIdentifier = 'NAT-3'"
            ).fetchone()
            run = store.claim(conn, project_id, ticket_id)
            store.tickets.transition(conn, ticket_id, "in_flight")
            store.set_phase(conn, run, "working")
            with patch.object(pipeline, "run_task", task), \
                    contextlib.redirect_stdout(io.StringIO()):
                dispatch._dispatch(self.project, conn, run,
                                   board_for(self.project), {"id": "NAT-3"},
                                   ticket_id, refresh=False)
        return run

    def key_state(self):
        return {
            "ticket": self.read(
                "SELECT status, boardColumn, boardState FROM tickets"
                " WHERE linearIdentifier = 'NAT-1'"),
            "notes": self.read(
                "SELECT n.kind, n.text FROM ticketNotes n JOIN tickets t"
                " ON t.id = n.ticketId WHERE t.linearIdentifier = 'NAT-1'"
                " ORDER BY n.id"),
            "events": self.read(
                "SELECT kind, summary FROM runEvents WHERE runId = ?"
                " ORDER BY id", self.runs["NAT-1"]),
            "run": self.read("SELECT outcome, mergeSha FROM runs WHERE id = ?",
                             self.runs["NAT-1"]),
        }

    def test_a_merged_revert_returns_its_ticket_to_backlog_with_the_reason(self):
        self.file_revert()

        def merge_the_revert(_target, _task, conn, run_id, _provider):
            advance_phase(conn, run_id, "merging")
            self.git("revert", "--no-edit", self.key_sha)
            return self.git("rev-parse", "HEAD")

        run = self.run_revert(merge_the_revert)
        revert_sha = self.git("rev-parse", "HEAD")
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"
                                   " WHERE id = ?", run),
                         [("merged", revert_sha)])
        state = self.key_state()
        self.assertEqual(state["ticket"], [("ready", "backlog", "Backlog")])
        self.assertIn(("move", f"Reverted by NAT-3: {REASON}"), state["notes"])
        reverted = [summary for kind, summary in state["events"]
                    if kind == "reverted"]
        self.assertEqual(len(reverted), 1, state["events"])
        self.assertIn("NAT-3", reverted[0])
        self.assertIn(revert_sha, reverted[0])
        self.assertEqual(state["run"], [("merged", self.key_sha)])
        self.assertEqual(
            board_for(self.project).states(["NAT-1"])["NAT-1"]["column"],
            "backlog")

    def test_a_failed_revert_run_leaves_its_ticket_as_it_was(self):
        self.file_revert()
        before = self.key_state()

        def fail(*_args):
            raise RunFailure("verify failed before merge")

        run = self.run_revert(fail)
        self.assertEqual(self.read("SELECT outcome FROM runs WHERE id = ?",
                                   run), [("failed",)])
        self.assertEqual(self.key_state(), before)
        self.assertEqual(before["ticket"], [("merged", "ready", None)])

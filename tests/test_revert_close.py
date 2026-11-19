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
from loop_fixture import MergeModeFixture, StubProvider  # noqa: E402
from phase_fixture import advance_phase  # noqa: E402 - after the sys.path insert

import store  # noqa: E402
from holophyte.board.provider import board_for  # noqa: E402
from holophyte.host import reconcile  # noqa: E402
from holophyte.loop import dispatch, pipeline  # noqa: E402
from holophyte.loop.gates import RunFailure  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from store.writes import set_board_state  # noqa: E402
from tests import test_cli_revert as cli_revert  # noqa: E402
from tests import test_pullrequest_parked as parked  # noqa: E402

REASON = r"broke the <header> band \n mid-word"


class MirrorBoard:
    native = False
    store_mode = False

    def __init__(self):
        self.writes = []

    def set_state(self, issue_id, state):
        self.writes.append((issue_id, state))

    def comment(self, task_id, body):
        pass


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
            store.tickets.transition(conn, self.ticket_id(conn, "NAT-1"),
                                     "merged")
        code, out, err = self.holo("revert", "NAT-1", REASON)
        self.assertEqual(code, 0, out + err)
        self.assertIn(f"Reverts: NAT-1 at {self.key_sha}",
                      self.read("SELECT body FROM tickets WHERE"
                                " linearIdentifier = 'NAT-3'")[0][0])

    def run_revert(self, task, board=None):
        with contextlib.closing(open_store(self.project)) as conn:
            (project_id,) = conn.execute("SELECT id FROM projects").fetchone()
            ticket_id = self.ticket_id(conn, "NAT-3")
            run = store.claim(conn, project_id, ticket_id)
            store.tickets.transition(conn, ticket_id, "in_flight")
            store.set_phase(conn, run, "working")
            with patch.object(pipeline, "run_task", task), \
                    contextlib.redirect_stdout(io.StringIO()):
                dispatch._dispatch(self.project, conn, run,
                                   board or board_for(self.project),
                                   {"id": "NAT-3"},
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
        run = self.run_revert(self.merge_the_revert)
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
        self.assertIn(REASON, reverted[0])
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

    def merge_the_revert(self, _target, _task, conn, run_id, _provider):
        advance_phase(conn, run_id, "merging")
        self.git("revert", "--no-edit", self.key_sha)
        return self.git("rev-parse", "HEAD")

    def test_a_non_native_board_keeps_the_ticket_merged_and_warns(self):
        self.file_revert()
        before = self.key_state()
        board = MirrorBoard()
        run = self.run_revert(self.merge_the_revert, board)
        self.assertEqual(self.read("SELECT outcome FROM runs WHERE id = ?",
                                   run), [("merged",)])
        self.assertEqual(self.key_state(), before)
        self.assertNotIn(("NAT-1", "Backlog"), board.writes)
        warnings = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'warning'", run)]
        self.assertTrue(any("NAT-1 still reads merged" in w
                            and f"Reverted by NAT-3: {REASON}" in w
                            for w in warnings), warnings)

    def edit_revert(self, *replacements):
        with contextlib.closing(open_store(self.project)) as conn:
            project_id, body, revision = conn.execute(
                "SELECT projectId, body, revision FROM tickets"
                " WHERE linearIdentifier = 'NAT-3'").fetchone()
            for old, new in replacements:
                self.assertIn(old, body)
                body = body.replace(old, new, 1)
            store.board.edit_ticket(conn, project_id, "NAT-3", body, revision)

    def test_a_fenced_example_does_not_hide_the_live_reverts_line(self):
        self.file_revert()
        self.merge_nat_2()
        self.edit_revert((
            "## Summary\n",
            f"## Summary\n\n  ~~~\nReverts: NAT-2 at {self.later_sha}\n"
            "  ~~~\t\n"))
        self.run_revert(self.merge_the_revert)
        self.assertEqual(self.key_state()["ticket"],
                         [("ready", "backlog", "Backlog")])
        self.assertEqual(self.read("SELECT status FROM tickets WHERE"
                                   " linearIdentifier = 'NAT-2'"),
                         [("merged",)])

    def test_a_hand_filed_revert_returns_its_ticket_with_the_written_why(self):
        self.file_revert()
        self.edit_revert(("# [Revert] Undo NAT-1: ", "# Put back "),
                         (f"Undo NAT-1's merge: {REASON.replace('<', '&lt;')}",
                          ""),
                         ("**Why:** ", "  Why: "),
                         ("## Summary\n", "## Summary\n\nWhy: a decoy\n"))
        with contextlib.closing(open_store(self.project)) as conn:
            conn.execute("DELETE FROM interventions WHERE \"action\" = 'revert'")
            conn.commit()
        (body,) = self.read("SELECT body FROM tickets WHERE"
                            " linearIdentifier = 'NAT-3'")[0]
        self.assertNotIn("Undo NAT-1's merge", body)
        written = body.split("\n  Why: ", 1)[1].splitlines()[0]
        self.run_revert(self.merge_the_revert)
        state = self.key_state()
        self.assertEqual(state["ticket"], [("ready", "backlog", "Backlog")])
        self.assertIn(("move", f"Reverted by NAT-3: {written}"),
                      state["notes"])
        self.assertTrue(any(kind == "reverted" and written in summary
                            for kind, summary in state["events"]),
                        state["events"])

    def test_an_inline_code_span_does_not_open_a_fence(self):
        self.file_revert()
        self.edit_revert(("## Summary\n",
                          "## Summary\n\n```git revert``` undoes it.\n"))
        self.run_revert(self.merge_the_revert)
        self.assertEqual(self.key_state()["ticket"],
                         [("ready", "backlog", "Backlog")])

    def test_a_ticket_already_in_backlog_still_records_the_revert(self):
        self.file_revert()
        with contextlib.closing(open_store(self.project)) as conn:
            set_board_state(conn, self.ticket_id(conn, "NAT-1"), "Backlog",
                            "backlog")
            conn.commit()
        self.run_revert(self.merge_the_revert)
        state = self.key_state()
        self.assertEqual(state["ticket"], [("ready", "backlog", "Backlog")])
        self.assertIn(("move", f"Reverted by NAT-3: {REASON}"), state["notes"])
        self.assertIn("reverted", [kind for kind, _ in state["events"]])

    def test_a_sha256_reverts_marker_returns_its_ticket(self):
        self.file_revert()
        long_sha = "ab" * 32
        with contextlib.closing(open_store(self.project)) as conn:
            conn.execute("UPDATE runs SET mergeSha = ? WHERE id = ?",
                         (long_sha, self.runs["NAT-1"]))
            conn.commit()
        self.edit_revert((f"Reverts: NAT-1 at {self.key_sha}",
                          f"Reverts: NAT-1 at {long_sha}"))
        self.run_revert(self.merge_the_revert)
        self.assertEqual(self.key_state()["ticket"],
                         [("ready", "backlog", "Backlog")])

    def test_a_summary_edited_after_claim_closes_out_the_claimed_marker(self):
        self.file_revert()
        self.merge_nat_2()

        def edit_then_merge(target, task, conn, run_id, provider):
            self.edit_revert((f"Reverts: NAT-1 at {self.key_sha}",
                              f"Reverts: NAT-2 at {self.later_sha}"))
            return self.merge_the_revert(target, task, conn, run_id, provider)

        self.run_revert(edit_then_merge)
        self.assertEqual(self.key_state()["ticket"],
                         [("ready", "backlog", "Backlog")])
        self.assertEqual(self.read("SELECT status FROM tickets WHERE"
                                   " linearIdentifier = 'NAT-2'"),
                         [("merged",)])

    def test_a_marker_indented_two_spaces_is_still_live(self):
        self.file_revert()
        self.edit_revert((f"Reverts: NAT-1 at {self.key_sha}",
                          f"  Reverts: NAT-1 at {self.key_sha}"))
        self.run_revert(self.merge_the_revert)
        self.assertEqual(self.key_state()["ticket"],
                         [("ready", "backlog", "Backlog")])

    def test_a_marker_continuing_a_paragraph_is_live_however_indented(self):
        self.file_revert()
        self.edit_revert((f"Reverts: NAT-1 at {self.key_sha}",
                          f"The revert:\n    Reverts: NAT-1 at {self.key_sha}"))
        self.run_revert(self.merge_the_revert)
        self.assertEqual(self.key_state()["ticket"],
                         [("ready", "backlog", "Backlog")])

    def test_a_tab_indented_marker_is_code_and_reopens_nothing(self):
        self.assert_hidden_marker_reopens_nothing("\t{}")

    def assert_hidden_marker_reopens_nothing(self, hidden):
        self.file_revert()
        before = self.key_state()
        self.edit_revert((f"Reverts: NAT-1 at {self.key_sha}",
                          hidden.format(f"Reverts: NAT-1 at {self.key_sha}")))
        self.run_revert(self.merge_the_revert)
        self.assertEqual(self.key_state(), before)

    def test_a_marker_inside_a_multiline_code_span_reopens_nothing(self):
        self.assert_hidden_marker_reopens_nothing("See `the line\n{}\n` here.")

    def test_a_fence_line_with_inner_text_does_not_close_the_fence(self):
        self.assert_hidden_marker_reopens_nothing("~~~\n~~~ ~~~\n{}\n~~~")

    def merge_nat_2(self):
        with contextlib.closing(open_store(self.project)) as conn:
            store.tickets.transition(conn, self.ticket_id(conn, "NAT-2"),
                                     "merged")

    @staticmethod
    def ticket_id(conn, key):
        return conn.execute("SELECT id FROM tickets WHERE"
                            " linearIdentifier = ?", (key,)).fetchone()[0]


class UnnamedGithubMergeTests(MergeModeFixture):
    parked_on_pr = parked.ParkedPullRequestTests.parked_on_pr
    fake_client = parked.ParkedPullRequestTests.fake_client

    def reconcile(self):
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        with contextlib.redirect_stdout(io.StringIO()):
            reconcile._reconcile_pull_requests(
                self.project, conn,
                conn.execute("SELECT id FROM projects").fetchone()[0],
                StubProvider())

    def test_a_merge_without_its_commit_yet_lands_once_github_names_it(self):
        self.parked_on_pr()
        merged = parked.ParkedPullRequestTests.MERGED_PULL
        self.fake_client(dict(merged, mergeCommit=None), merged)
        self.reconcile()
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.reconcile()
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

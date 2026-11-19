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
from phase_fixture import advance_phase, finish_run  # noqa: E402

import store  # noqa: E402
from holophyte.board.provider import board_for  # noqa: E402
from holophyte.host import reconcile  # noqa: E402
from holophyte.loop import dispatch, pipeline  # noqa: E402
from holophyte.loop.gates import RunFailure  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from store.writes import set_board_state  # noqa: E402
from tests import test_cli_revert as cli_revert  # noqa: E402
from tests import test_pullrequest_parked as parked  # noqa: E402

PULL_URL = "https://github.com/example/repo/pull/7"
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

    def test_a_second_revert_of_a_ticket_in_backlog_records_its_own_note(self):
        self.file_revert()
        with contextlib.closing(open_store(self.project)) as conn:
            key_id = self.ticket_id(conn, "NAT-1")
            set_board_state(conn, key_id, "Backlog", "backlog")
            store.revisions.record_board_fields(conn, key_id)
            conn.commit()
        self.run_revert(self.merge_the_revert)
        with contextlib.closing(open_store(self.project)) as conn:
            store.tickets.transition(conn, key_id, "in_flight")
            store.tickets.transition(conn, key_id, "merged")
            store.tickets.reopen_merged(conn, self.ticket_id(conn, "NAT-3"))
        self.run_revert(self.merge_again)
        state = self.key_state()
        self.assertEqual(state["ticket"], [("ready", "backlog", "Backlog")])
        self.assertEqual(state["notes"].count(
            ("move", f"Reverted by NAT-3: {REASON}")), 2, state["notes"])
        self.assertEqual([kind for kind, _ in state["events"]].count(
            "reverted"), 2, state["events"])

    def merge_again(self, _target, _task, conn, run_id, _provider):
        advance_phase(conn, run_id, "merging")
        return self.commit({"src/header.py": "header again\n"},
                           "NAT-3: revert again")

    def test_a_canceled_original_still_returns_to_backlog(self):
        self.file_revert()
        with contextlib.closing(open_store(self.project)) as conn:
            set_board_state(conn, self.ticket_id(conn, "NAT-1"), "Canceled",
                            "canceled")
            conn.commit()
        self.run_revert(self.merge_the_revert)
        state = self.key_state()
        self.assertEqual(state["ticket"], [("ready", "backlog", "Backlog")])
        self.assertIn(("move", f"Reverted by NAT-3: {REASON}"), state["notes"])

    def test_a_refused_move_keeps_the_reverted_event_and_warns(self):
        self.file_revert()

        def refuse(*_args, **_kwargs):
            raise store.board.FilingRefused(["the board refused the move"])

        with patch.object(store.board, "move_ticket", refuse):
            run = self.run_revert(self.merge_the_revert)
        state = self.key_state()
        self.assertEqual(state["ticket"], [("merged", "ready", None)])
        self.assertIn("reverted", [kind for kind, _ in state["events"]])
        self.assertTrue(any("the board refused the move" in summary
                            for (summary,) in self.read(
                                "SELECT summary FROM runEvents WHERE runId = ?"
                                " AND kind = 'warning'", run)))

    def test_a_github_merge_without_its_sha_lands_and_reopens_the_original(self):
        self.file_revert()
        with contextlib.closing(open_store(self.project)) as conn:
            (project_id,) = conn.execute("SELECT id FROM projects").fetchone()
            ticket_id = self.ticket_id(conn, "NAT-3")
            run = store.claim(conn, project_id, ticket_id)
            store.tickets.transition(conn, ticket_id, "in_flight")
            store.set_phase(conn, run, "working")
            advance_phase(conn, run, "merge_gate")
            store.park(conn, run, "awaiting_merge_approval", pr_url=PULL_URL)
            store.walk_ticket(conn, ticket_id, "blocked_on_operator")
            set_board_state(conn, ticket_id, "Canceled", "canceled")
            conn.commit()
            parked.ParkedPullRequestTests.fake_client(
                self, dict(parked.ParkedPullRequestTests.MERGED_PULL,
                           mergeCommit=None))
            with contextlib.redirect_stdout(io.StringIO()):
                verdict = reconcile._ask_before_cancel(
                    self.project, conn, board_for(self.project), ticket_id)
        self.assertEqual(verdict, "landed")
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"
                                   " WHERE id = ?", run), [("merged", None)])
        state = self.key_state()
        self.assertEqual(state["ticket"], [("ready", "backlog", "Backlog")])
        (reverted,) = [summary for kind, summary in state["events"]
                       if kind == "reverted"]
        self.assertIn("NAT-3", reverted)
        self.assertIn("sha: null", reverted)

    def test_a_sha256_reverts_marker_returns_its_ticket(self):
        self.file_revert()
        long_sha = "ab" * 32
        with contextlib.closing(open_store(self.project)) as conn:
            (project_id,) = conn.execute("SELECT id FROM projects").fetchone()
            ticket_id = self.ticket_id(conn, "NAT-1")
            run = store.claim(conn, project_id, ticket_id)
            store.set_phase(conn, run, "working")
            finish_run(conn, run, "merged", merge_sha=long_sha)
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
        run = self.run_revert(self.merge_the_revert)
        self.assertEqual(self.read("SELECT outcome FROM runs WHERE id = ?",
                                   run), [("merged",)])
        self.assertEqual(self.key_state(), before)

    def test_a_marker_inside_a_multiline_code_span_reopens_nothing(self):
        self.assert_hidden_marker_reopens_nothing("See `the line\n{}\n` here.")

    def test_a_marker_inside_an_html_comment_reopens_nothing(self):
        self.assert_hidden_marker_reopens_nothing("<!-- < sample\n{}\n-->")

    def test_a_marker_under_an_unclosed_html_comment_reopens_nothing(self):
        self.assert_hidden_marker_reopens_nothing("<!-- < sample\n\n{}")

    def test_a_comment_closing_inside_a_code_span_does_not_expose_a_marker(self):
        self.assert_hidden_marker_reopens_nothing(
            "See `a <!-- < ` --> `\n{}\n` here.")

    def test_a_marker_indented_under_a_heading_is_code_and_reopens_nothing(self):
        self.assert_hidden_marker_reopens_nothing("### Example\n    {}")

    def test_a_backtick_in_indented_code_does_not_hide_a_later_marker(self):
        self.assert_marker_stays_live("Before.\n\n    a ` b\n{}\nthen `c`.")

    def test_a_marker_after_an_html_comment_is_still_live(self):
        self.assert_marker_stays_live("<!-- < filed by hand -->\n{}")

    def test_comment_marks_in_fenced_examples_do_not_hide_a_live_marker(self):
        self.assert_marker_stays_live(
            "```\n<!-- < sample\n```\n\n{}\n\n```\n-->\n```")

    def assert_marker_stays_live(self, around):
        self.file_revert()
        marker = f"Reverts: NAT-1 at {self.key_sha}"
        self.edit_revert((marker, around.format(marker)))
        self.run_revert(self.merge_the_revert)
        self.assertEqual(self.key_state()["ticket"],
                         [("ready", "backlog", "Backlog")])

    def test_an_escaped_backtick_does_not_open_a_code_span(self):
        self.assert_hidden_marker_reopens_nothing(
            "Use \\` literally; `example\n{}\n`")

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


"""KO-709: a ticket naming files main no longer has is not claimed.

Since KO-381 a named path that does not exist is an advisory at filing,
so a ticket filed against files a later merge moved or deleted used to be
claimed and fail its run on the missing landmarks. The claim now asks
`main` for every file the criteria and the implementation notes name in
a code span, unless the body declares it new; a ticket naming one main
lacks lands in `needs_spec`, gets one board comment, moves to Backlog,
and the loop takes the next ticket.

KO-713: the same refusal for a function or class an implementation-notes
item names beside a file, when main's copy of that file no longer holds
the name, and for a `Depends on:` ticket neither the store nor the board
calls merged.

KO-716: the parked issue also carries a `stale` label, and the claim skips
an issue carrying it until the maintainer takes it off.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Both discovery and named-module unittest commands need the harness on sys.path.
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit  # noqa: E402 - after the sys.path insert
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    VALID_BODY,
    LoopFixture,
    StubProvider,
    a_task,
)

import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.claim  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above
from holophyte.freshness import stale_reasons  # noqa: E402

GONE = "holophyte/gone.py"
# The fixture's valid body, its implementation notes naming a file the
# fixture repository's main has never held.
STALE_BODY = VALID_BODY.replace(
    "## Implementation notes\n\n* None.\n",
    f"## Implementation notes\n\n* Edit `{GONE}` where the thing lives.\n")
# The same body declaring that file new.
NEW_FILE_BODY = VALID_BODY.replace(
    "## Implementation notes\n\n* None.\n",
    f"## Implementation notes\n\n* Add the new file `{GONE}` for the thing.\n")

CLAIM = "holophyte/claim.py"
# What main's `holophyte/claim.py` holds in the symbol tests.
CLAIM_SOURCE = ("class Claimer:\n"
                "    def admit_ticket(self):\n"
                "        return True\n")


def notes_body(item):
    """The fixture's valid body with `item` as its one implementation note."""
    return VALID_BODY.replace("## Implementation notes\n\n* None.\n",
                              f"## Implementation notes\n\n* {item}\n")


def depends_body(identifier):
    return VALID_BODY.replace("Depends on: none", f"Depends on: {identifier}")


class CommentRaises(StubProvider):
    """A board whose comment call fails, as Linear unreachable would."""

    def comment(self, task_id, body):
        raise RuntimeError("linear is down")


class BoardUnreachable(StubProvider):
    """A board whose closed-issue query fails, as Linear unreachable would."""

    def closed_identifiers(self, identifiers):
        raise RuntimeError("linear is down")


class LabelRaises(StubProvider):
    """A board that refuses the `stale` label; the lease label still lands."""

    def label_issue(self, issue_id, name):
        if name == "stale":
            raise RuntimeError("linear is down")
        super().label_issue(issue_id, name)


class ClaimFreshnessTests(LoopFixture):

    def runs_by_ticket(self):
        return self.read("SELECT t.linearIdentifier FROM runs r JOIN tickets t"
                         " ON t.id = r.ticketId ORDER BY r.id")

    def test_a_ticket_naming_a_file_main_lacks_is_parked_and_the_next_claimed(self):
        provider = StubProvider(dict(a_task(1), body=STALE_BODY), a_task(2))

        out = self.main_output(Commit("second ticket"), APPROVE,
                               provider=provider)

        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])
        self.assertEqual(
            self.read("SELECT status FROM tickets"
                      " WHERE linearIdentifier = 'KO-131'"),
            [("needs_spec",)])
        stale_comments = [body for issue, body in provider.comments
                          if issue == "iss-131"]
        self.assertEqual(len(stale_comments), 1)
        self.assertIn(GONE, stale_comments[0])
        self.assertEqual([s for s in provider.states if s[0] == "iss-131"],
                         [("iss-131", "Backlog")])
        self.assertIn("stale", provider.labels["iss-131"])
        self.assertIn("KO-131 skipped", out)

    def test_a_body_declaring_the_file_new_is_claimed(self):
        provider = StubProvider(dict(a_task(1), body=NEW_FILE_BODY))

        self.loop(Commit("the new file"), APPROVE, provider=provider)

        self.assertEqual(self.runs_by_ticket(), [("KO-131",)])
        self.assertFalse([body for _, body in provider.comments
                          if "Not claimed" in body])
        self.assertNotIn(("iss-131", "Backlog"), provider.states)

    def test_a_file_only_in_the_checked_out_branch_is_missing(self):
        self.git("checkout", "-q", "-b", "elsewhere")
        (self.target / "holophyte").mkdir()
        (self.target / GONE).write_text("x = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "only on elsewhere")
        self.assertTrue((self.target / GONE).exists())

        reasons = stale_reasons(self.target, STALE_BODY)

        self.assertEqual(len(reasons), 1)
        self.assertIn(GONE, reasons[0])
        self.assertIn("is not on main", reasons[0])

    def test_a_ticket_on_a_pull_request_skips_the_check(self):
        """The last run's branch holds the file main lacks: the ticket is
        admitted as before, with no comment and no move to Backlog."""
        task = dict(a_task(), body=STALE_BODY)
        provider = StubProvider(task)
        conn = holophyte.runs.open_store(self.project)
        self.addCleanup(conn.close)
        project_id = tickets.ensure_project(conn, provider.team, self.target)
        ticket = holophyte.board.mirror_task(conn, project_id, task)
        run_id = store.claim(conn, project_id, ticket)
        tickets.transition(conn, ticket, "in_flight")
        store.set_pull_request(conn, run_id,
                               "https://github.com/example/repo/pull/709")
        store.release(conn, run_id, "failed", "sent back to the babysitter")
        store.requeue(conn, ticket, "new review activity")
        conn.commit()

        with patch.object(sys, "stdout", io.StringIO()):
            admitted = holophyte.claim._admit_ticket(
                self.project, conn, project_id, provider, task,
                SimpleNamespace(trips=[], watched=[]))

        self.assertEqual(admitted, ticket)
        self.assertEqual(provider.comments, [])
        self.assertEqual(provider.states, [])

    def test_a_failed_comment_warns_and_the_loop_goes_on(self):
        provider = CommentRaises(dict(a_task(1), body=STALE_BODY), a_task(2))

        out = self.main_output(Commit("second ticket"), APPROVE,
                               provider=provider)

        self.assertIn("stale-ticket comment failed for KO-131", out)
        self.assertIn("KO-131 skipped", out)
        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])

    def test_a_ticket_labelled_stale_is_skipped_without_a_comment(self):
        """Moved back to Todo with the label still on: a body with no
        problem is not claimed, and the board is not told again."""
        provider = StubProvider(dict(a_task(1), labels=["stale"]), a_task(2))

        out = self.main_output(Commit("second ticket"), APPROVE,
                               provider=provider)

        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])
        self.assertEqual(
            self.read("SELECT status FROM tickets"
                      " WHERE linearIdentifier = 'KO-131'"),
            [("needs_spec",)])
        self.assertEqual([c for c in provider.comments if c[0] == "iss-131"],
                         [])
        self.assertIn("KO-131 skipped: labelled stale", out)

    def test_a_failed_label_warns_and_the_ticket_is_still_skipped(self):
        provider = LabelRaises(dict(a_task(1), body=STALE_BODY), a_task(2))

        out = self.main_output(Commit("second ticket"), APPROVE,
                               provider=provider)

        self.assertIn("stale label failed for KO-131", out)
        self.assertIn("KO-131 skipped", out)
        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])


class ClaimSymbolAndDependencyTests(LoopFixture):
    """KO-713: names a later merge renamed away, and dependencies not merged."""

    def runs_by_ticket(self):
        return self.read("SELECT t.linearIdentifier FROM runs r JOIN tickets t"
                         " ON t.id = r.ticketId ORDER BY r.id")

    def commit_claim_to_main(self):
        (self.target / "holophyte").mkdir()
        (self.target / CLAIM).write_text(CLAIM_SOURCE)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "claim module")

    def stale_comments(self, provider, issue="iss-131"):
        return [body for i, body in provider.comments
                if i == issue and "Not claimed" in body]

    def assert_claimed_without_comment(self, provider):
        self.assertEqual(self.runs_by_ticket(), [("KO-131",)])
        self.assertEqual(self.stale_comments(provider), [])

    def test_a_function_main_lacks_in_the_named_file_is_parked(self):
        self.commit_claim_to_main()
        body = notes_body(f"Change `_admit_ticket()` in `{CLAIM}`.")
        provider = StubProvider(dict(a_task(1), body=body), a_task(2))

        out = self.main_output(Commit("second ticket"), APPROVE,
                               provider=provider)

        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])
        comments = self.stale_comments(provider)
        self.assertEqual(len(comments), 1)
        self.assertIn("`_admit_ticket()`", comments[0])
        self.assertIn(CLAIM, comments[0])
        self.assertIn("KO-131 skipped", out)

    def test_a_class_beginning_with_an_acronym_main_lacks_is_parked(self):
        self.commit_claim_to_main()
        body = notes_body(f"Serve the claim from `HTTPServer` in `{CLAIM}`.")
        provider = StubProvider(dict(a_task(1), body=body), a_task(2))

        self.main_output(Commit("second ticket"), APPROVE, provider=provider)

        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])
        comments = self.stale_comments(provider)
        self.assertEqual(len(comments), 1)
        self.assertIn("`HTTPServer`", comments[0])

    def test_a_function_on_a_wrapped_line_of_the_item_is_checked(self):
        self.commit_claim_to_main()
        body = notes_body(f"In `{CLAIM}`, change the admission\n"
                          "  where `removed_function()` refuses.")
        provider = StubProvider(dict(a_task(1), body=body), a_task(2))

        self.main_output(Commit("second ticket"), APPROVE, provider=provider)

        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])
        comments = self.stale_comments(provider)
        self.assertEqual(len(comments), 1)
        self.assertIn("`removed_function()`", comments[0])

    def test_a_function_and_a_class_main_holds_are_claimed(self):
        self.commit_claim_to_main()
        body = notes_body(f"Change `Claimer` and `claim.admit_ticket()` in"
                          f" `{CLAIM}`.")
        provider = StubProvider(dict(a_task(1), body=body))

        self.loop(Commit("the change"), APPROVE, provider=provider)

        self.assert_claimed_without_comment(provider)

    def test_a_paragraph_after_the_notes_list_is_no_item(self):
        self.commit_claim_to_main()
        body = notes_body(f"Change `Claimer` and `admit_ticket()` in `{CLAIM}`."
                          "\n\nBackground for the implementer:\n"
                          "Keep the existing behavior.")
        provider = StubProvider(dict(a_task(1), body=body))

        self.loop(Commit("the change"), APPROVE, provider=provider)

        self.assert_claimed_without_comment(provider)

    def test_a_function_declared_new_is_not_checked(self):
        self.commit_claim_to_main()
        body = notes_body(f"Beside `admit_ticket()` in `{CLAIM}`, add a new"
                          " `refuse_ticket()`.")
        provider = StubProvider(dict(a_task(1), body=body))

        self.loop(Commit("the new function"), APPROVE, provider=provider)

        self.assert_claimed_without_comment(provider)

    def test_a_dependency_neither_store_nor_board_calls_merged_is_parked(self):
        provider = StubProvider(dict(a_task(1), body=depends_body("KO-900")),
                                a_task(2))

        self.main_output(Commit("second ticket"), APPROVE, provider=provider)

        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])
        comments = self.stale_comments(provider)
        self.assertEqual(len(comments), 1)
        self.assertIn("`KO-900` (named in Depends on) is not merged",
                      comments[0])

    def test_a_dependency_the_board_cannot_be_asked_about_is_parked(self):
        provider = BoardUnreachable(
            dict(a_task(1), body=depends_body("KO-900")), a_task(2))

        self.main_output(Commit("second ticket"), APPROVE, provider=provider)

        self.assertEqual(self.runs_by_ticket(), [("KO-132",)])
        comments = self.stale_comments(provider)
        self.assertEqual(len(comments), 1)
        self.assertIn("`KO-900` (named in Depends on) is not merged",
                      comments[0])

    def test_a_dependency_the_board_completed_is_claimed(self):
        provider = StubProvider(dict(a_task(1), body=depends_body("KO-900")))
        provider.closed = {"KO-900": "completed"}

        self.loop(Commit("the change"), APPROVE, provider=provider)

        self.assert_claimed_without_comment(provider)

    def mirror_dependency(self, provider, status):
        conn = holophyte.runs.open_store(self.project)
        self.addCleanup(conn.close)
        project_id = tickets.ensure_project(conn, provider.team, self.target)
        dependency = holophyte.board.mirror_task(
            conn, project_id, dict(a_task(), id="KO-900", issue_id="iss-900"))
        store.walk_ticket(conn, dependency, status)
        conn.commit()

    def test_a_dependency_the_board_completed_over_an_abandoned_mirror_is_claimed(self):
        provider = StubProvider(dict(a_task(1), body=depends_body("KO-900")))
        provider.closed = {"KO-900": "completed"}
        self.mirror_dependency(provider, "abandoned")

        self.loop(Commit("the change"), APPROVE, provider=provider)

        self.assert_claimed_without_comment(provider)

    def test_a_dependency_the_store_holds_merged_is_claimed(self):
        provider = StubProvider(dict(a_task(1), body=depends_body("KO-900")))
        self.mirror_dependency(provider, "merged")

        self.loop(Commit("the change"), APPROVE, provider=provider)

        self.assert_claimed_without_comment(provider)

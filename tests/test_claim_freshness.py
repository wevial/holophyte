"""KO-709: a ticket naming files main no longer has is not claimed.

Since KO-381 a named path that does not exist is an advisory at filing,
so a ticket filed against files a later merge moved or deleted used to be
claimed and fail its run on the missing landmarks. The claim now asks
`main` for every file the criteria and the implementation notes name in
a code span, unless the body declares it new; a ticket naming one main
lacks lands in `needs_spec`, gets one board comment, moves to Backlog,
and the loop takes the next ticket.

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


class CommentRaises(StubProvider):
    """A board whose comment call fails, as Linear unreachable would."""

    def comment(self, task_id, body):
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

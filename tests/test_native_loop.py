"""KO-762: a native project runs end to end with no Linear call.

The loop runs on the real fixture repository with real git against a
`[board] kind = "native"` project whose tickets are filed through
`store.board`: `NAT-2`, which depends on `NAT-1`, waits at
`blocked_on_deps` until `NAT-1` merges, then runs, with `LINEAR_API_KEY`
unset and Linear's transport failing the test. An unmerged dependency on
a native board is a wait, not a stale body, and the host sweep ends a
finished wait without listing the board.

Run: python3 -m unittest discover -s tests -p 'test_native_loop.py' -v
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import holophyte.review.freshness  # noqa: E402
import linear_provider  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board.board_sync import owed  # noqa: E402
from holophyte.board.native_board import NativeBoard  # noqa: E402
from holophyte.config.config_tables import sweep_config  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.review.freshness import park_stale, stale_reasons  # noqa: E402
from provider import FileProvider, board_for  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
DEPENDENT = VALID_BODY.replace("Depends on: none", "Depends on: NAT-1")
EVIDENCE = VALID_BODY.replace(
    "## Implementation notes",
    "## Evidence\n\n- The thing on its page.\n\n## Implementation notes")
LATER = "docs/later.md"
NAMING_LATER = VALID_BODY.replace(
    "## Implementation notes\n\n* None.\n",
    f"## Implementation notes\n\n* Extend `{LATER}` with the thing.\n")
CAPTURE = '[merge]\nmode = "pr"\nui_capture = "true"\nui_paths = ["web/**"]\n'


def no_linear(*args, **kwargs):
    raise AssertionError("a native project asked Linear")


class UnlistedBoard(NativeBoard):
    """The native board, failing the test when it is listed."""

    def listing(self):
        raise AssertionError("a native board was listed")

    ready_issues = listing


class Snapshot(Commit):
    """An implementer commit that first records both tickets' statuses."""

    def __init__(self, test, message):
        super().__init__(message)
        self.test = test

    def play(self, cwd, turn):
        self.test.seen.append(self.test.statuses())
        return super().play(cwd, turn)


class NativeLoopTests(LoopFixture):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(NATIVE)
        self.board = board_for(self.project)
        self.assertIsInstance(self.board, NativeBoard)
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        self.conn = conn
        self.project_id = store.tickets.ensure_project(
            conn, self.board.team, self.project.path)
        self.seen = []

    def file(self, body):
        return store.board.file_ticket(self.conn, self.project_id, "NAT",
                                       body, column="ready")

    def statuses(self):
        return dict(self.read("SELECT linearIdentifier, status FROM tickets"))

    def run_loop(self):
        fake = FakeAgent(Snapshot(self, "the first work"), APPROVE,
                         Snapshot(self, "the second work"), APPROVE)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake), \
                patch("holophyte.review.freshness.critic_admits",
                      return_value=True):
            self.rc = holophyte.cli.operator.main(self.project, self.board)
        return out.getvalue()

    def test_a_dependent_ticket_waits_then_runs_after_its_dependency(self):
        self.assertEqual(self.file(VALID_BODY), "NAT-1")
        self.assertEqual(self.file(DEPENDENT), "NAT-2")
        self.assertEqual(self.statuses(),
                         {"NAT-1": "ready", "NAT-2": "blocked_on_deps"})

        out = self.run_loop()

        self.assertEqual(self.statuses(),
                         {"NAT-1": "merged", "NAT-2": "merged"}, out)
        self.assertEqual(self.read(
            "SELECT t.linearIdentifier, r.outcome FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId ORDER BY r.startedAt, r.id"),
            [("NAT-1", "merged"), ("NAT-2", "merged")])
        self.assertEqual(self.seen, [
            {"NAT-1": "in_flight", "NAT-2": "blocked_on_deps"},
            {"NAT-1": "merged", "NAT-2": "in_flight"}])
        self.assertIn("the first work", self.subjects())
        self.assertIn("the second work", self.subjects())

    def test_evidence_the_project_stopped_capturing_is_not_claimed(self):
        self.configure(NATIVE + CAPTURE)
        self.assertEqual(self.file(EVIDENCE), "NAT-1")
        self.configure(NATIVE)
        self.board = board_for(self.project)

        out = self.run_loop()

        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)], out)
        self.assertEqual(self.statuses(), {"NAT-1": "needs_spec"}, out)
        self.assertIn("[merge] ui_capture", out)

    def test_an_unmerged_dependency_is_stale_only_on_a_file_board(self):
        self.file(VALID_BODY)
        self.file(VALID_BODY)
        self.file(VALID_BODY)
        body = VALID_BODY.replace("Depends on: none", "Depends on: NAT-3")
        self.assertEqual(self.statuses()["NAT-3"], "ready")
        files = self.target.parent / "files"
        files.mkdir()

        native = stale_reasons(self.target, body, self.conn, self.board)
        filed = stale_reasons(self.target, body, self.conn,
                              FileProvider(files))

        self.assertEqual(native, [])
        self.assertEqual(len(filed), 1)
        self.assertIn("`NAT-3`", filed[0])

    def test_the_sweep_ends_a_finished_wait_without_listing_the_board(self):
        self.file(VALID_BODY)
        self.file(DEPENDENT)
        (first,) = self.read(
            "SELECT id FROM tickets WHERE linearIdentifier = 'NAT-1'")
        store.tickets.walk_ticket(self.conn, first[0], "merged")
        self.assertEqual(self.statuses(),
                         {"NAT-1": "merged", "NAT-2": "blocked_on_deps"})
        board = UnlistedBoard(self.project, "NAT", self.board.team)

        with closing(open_store(self.project)) as conn:
            pairs = owed(self.project, conn, self.project_id, board, 0,
                         io.StringIO(), sweep_config(self.project))

        (second,) = self.read(
            "SELECT id FROM tickets WHERE linearIdentifier = 'NAT-2'")
        self.assertEqual([ticket for ticket, _ in pairs], [second[0]])
        self.assertEqual(self.statuses()["NAT-2"], "ready")

    def parked_on_later(self):
        """NAT-1, naming `LATER`, parked by the loop while main lacks it."""
        self.assertEqual(self.file(NAMING_LATER), "NAT-1")
        out = self.run_loop()
        self.assertEqual(self.statuses(), {"NAT-1": "needs_spec"}, out)
        self.assertIn("out of date with main", out)
        return self.read("SELECT id FROM tickets")[0][0]

    def sweep(self):
        board = UnlistedBoard(self.project, "NAT", self.board.team)
        with closing(open_store(self.project)) as conn:
            return owed(self.project, conn, self.project_id, board, 0,
                        io.StringIO(), sweep_config(self.project))

    def notes(self, ticket_id):
        return self.read(f"SELECT kind FROM ticketNotes WHERE ticketId ="
                         f" {ticket_id} ORDER BY id")

    def test_the_sweep_returns_a_stale_park_to_ready_once_main_has_it(self):
        ticket_id = self.parked_on_later()
        (self.target / "docs").mkdir()
        (self.target / LATER).write_text("# Later\n")
        self.git("add", LATER)
        self.git("commit", "-q", "-m", "add the later doc")

        pairs = self.sweep()

        self.assertEqual(self.statuses(), {"NAT-1": "ready"})
        self.assertEqual([ticket for ticket, _ in pairs], [ticket_id])
        self.assertEqual(self.notes(ticket_id)[-1], ("recheck",))

    def test_the_sweep_leaves_a_stale_park_while_main_still_lacks_it(self):
        ticket_id = self.parked_on_later()
        before = self.notes(ticket_id)

        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.sweep(), [])

        self.assertEqual(self.statuses(), {"NAT-1": "needs_spec"})
        self.assertEqual(self.notes(ticket_id), before)

    def commit_later(self):
        (self.target / "docs").mkdir()
        (self.target / LATER).write_text("# Later\n")
        self.git("add", LATER)
        self.git("commit", "-q", "-m", "add the later doc")

    def edit(self, body):
        (revision,) = self.read("SELECT revision FROM tickets")[0]
        store.board.edit_ticket(self.conn, self.project_id, "NAT-1", body,
                                revision)

    def critic_park(self):
        with patch.object(sys, "stdout", io.StringIO()):
            park_stale(self.project, self.conn, self.project_id, self.board,
                       self.board.fetch_task("NAT-1"),
                       ["critic: stale \u2014 already done"], admitted=True,
                       kind="critic")

    def test_the_sweep_leaves_a_critic_park_with_a_clean_body(self):
        self.assertEqual(self.file(VALID_BODY), "NAT-1")
        self.critic_park()
        (ticket_id,) = self.read("SELECT id FROM tickets")[0]
        before = self.notes(ticket_id)

        self.assertEqual(self.sweep(), [])

        self.assertEqual(self.statuses(), {"NAT-1": "needs_spec"})
        self.assertEqual(self.notes(ticket_id), before)

    def test_a_stale_park_after_a_critic_park_is_re_checked(self):
        ticket_id = self.parked_on_later()
        self.edit(VALID_BODY)
        self.critic_park()
        self.edit(NAMING_LATER)
        out = self.run_loop()
        self.assertEqual(self.statuses(), {"NAT-1": "needs_spec"}, out)
        self.commit_later()

        self.assertEqual([ticket for ticket, _ in self.sweep()], [ticket_id])
        self.assertEqual(self.statuses(), {"NAT-1": "ready"})

    def test_a_critic_park_landing_mid_re_check_is_kept(self):
        ticket_id = self.parked_on_later()
        self.commit_later()
        judged = holophyte.review.freshness.stale_reasons

        def critic_parks_first(*args, **kwargs):
            self.edit(NAMING_LATER)
            self.critic_park()
            return judged(*args, **kwargs)

        with patch.object(holophyte.review.freshness, "stale_reasons",
                          critic_parks_first):
            self.assertEqual(self.sweep(), [])

        self.assertEqual(self.statuses(), {"NAT-1": "needs_spec"})
        self.assertNotIn(("recheck",), self.notes(ticket_id))

    def unverified_main(self):
        """`main^{commit}` fails to verify; every other git call is real."""
        real = holophyte.review.freshness._git

        def fails_verify(repo, *args):
            if args == ("rev-parse", "--verify", "-q", "main^{commit}"):
                return False
            return real(repo, *args)

        return patch.object(holophyte.review.freshness, "_git", fails_verify)

    def test_an_unverifiable_main_leaves_a_stale_park_untouched(self):
        ticket_id = self.parked_on_later()
        before = self.notes(ticket_id)

        out = io.StringIO()
        with self.unverified_main(), patch.object(sys, "stdout", out):
            self.assertEqual(self.sweep(), [])

        self.assertEqual(self.statuses(), {"NAT-1": "needs_spec"})
        self.assertEqual(self.notes(ticket_id), before)
        self.assertIn("verif", out.getvalue().lower())

    def re_park(self):
        with patch.object(sys, "stdout", io.StringIO()):
            park_stale(self.project, self.conn, self.project_id, self.board,
                       self.board.fetch_task("NAT-1"),
                       [f"`{LATER}` (named in Implementation notes) is not"
                        " on main"])
        self.assertEqual(self.statuses(), {"NAT-1": "needs_spec"})

    def recheck_texts(self, ticket_id):
        return [text for (text,) in self.read(
            f"SELECT text FROM ticketNotes WHERE ticketId = {ticket_id}"
            " AND kind = 'recheck' ORDER BY id")]

    def test_a_repeated_recheck_verdict_writes_one_note_until_main_moves(self):
        ticket_id = self.parked_on_later()
        self.commit_later()
        self.sweep()
        first = self.recheck_texts(ticket_id)
        self.re_park()

        self.sweep()

        self.assertEqual(self.statuses(), {"NAT-1": "ready"})
        self.assertEqual(len(first), 1)
        self.assertEqual(self.recheck_texts(ticket_id), first)
        self.re_park()
        (self.target / "docs" / "more.md").write_text("# More\n")
        self.git("add", "docs/more.md")
        self.git("commit", "-q", "-m", "add more")

        self.sweep()

        texts = self.recheck_texts(ticket_id)
        self.assertEqual(len(texts), 2)
        self.assertNotEqual(texts[0], texts[1])

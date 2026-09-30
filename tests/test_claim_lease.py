"""The claim's board lease label, set, checked and cleared."""
from __future__ import annotations

import io
import sqlite3
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    LoopFixture,
    StubProvider,
    a_task,
)

import holophyte.board.projection  # noqa: E402 - after the sys.path insert above
import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import holophyte.loop.claim  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


class BoardLeaseLabelTests(LoopFixture):
    """The claim's second lease (KO-351): a `holo:HOST` label on the
    issue, which a writer with a store of its own can see where it cannot
    see this store's `activeRunId`."""

    HOST = "writer-1"

    def setUp(self):
        super().setUp()
        self.configure('[report]\nhost_label = "writer-1"\n')

    @staticmethod
    def label(host="writer-1"):
        return f"holo:{host}"

    def labelled(self, provider, labels, *more):
        task = a_task()
        task["labels"] = labels
        return provider(task, *more)

    def seed_ended_run(self, requeue=True):
        """A run of this store on KO-131 that ended `failed` -- with the
        board down, so its label is still on the issue -- and, unless told
        otherwise, the requeue that put the ticket back. Returns the run
        id."""
        conn = store.open(str(self.db))
        try:
            project = tickets.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            ticket = holophyte.board.projection.mirror_task(conn, project, a_task())
            run_id = store.claim(conn, project, ticket)
            tickets.transition(conn, ticket, "in_flight")
            store.release(conn, run_id, "failed", "the board was down")
            if requeue:
                store.requeue(conn, ticket, "board back")
            conn.commit()
        finally:
            conn.close()
        return run_id

    def test_a_claim_labels_before_the_implementer_and_a_merge_unlabels(self):
        """The label `holo:writer-1` is on the issue when the implementer's
        turn begins -- not after it, when a second writer's listing could
        already have offered the ticket -- and the merge's close-out takes
        it off."""
        provider = StubProvider(a_task())
        at_implement = []

        @dataclass
        class Witness(Commit):
            def play(self, cwd, turn):
                at_implement.append(list(provider.labels["iss-131"]))
                return super().play(cwd, turn)

        self.loop(Witness("the scripted work"), APPROVE, provider=provider)

        self.assertEqual(self.read("SELECT id, outcome FROM runs"),
                         [(1, "merged")])
        self.assertEqual(at_implement, [["holo:writer-1"]])
        self.assertEqual(provider.labels["iss-131"], [])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        # One read-back, between the add and the implementer.
        self.assertEqual(provider.read_calls, ["iss-131"])

    def test_a_ticket_another_writer_labelled_is_skipped_and_nothing_is_leased(self):
        provider = self.labelled(StubProvider, [self.label("writer-2")])

        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("[holo2] KO-131 is leased by writer-2 on the board;"
                      " skipping it", out)
        self.assertEqual(self.read("SELECT id FROM runs"), [])
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(provider.label_calls, [])
        self.assertEqual(provider.read_calls, [])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-2"])

    def test_this_writers_labels_with_no_live_run_are_stale_and_removed(self):
        """Run 1 ended with the board down and its label stayed. It is no
        lease: the store has no live run under it, so the claim goes ahead
        -- the stale label off under the store lease, then the fresh one
        on, then off again at the merge."""
        self.seed_ended_run()
        provider = self.labelled(StubProvider, [self.label()])

        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)

        self.assertIn("carries this writer's lease label holo:writer-1"
                      " with no live run; removing the stale label and"
                      " claiming", out)
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "failed"), (2, "merged")])
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], [])
        self.assertEqual(self.last_fake.roles, ["implement", "review"])

    def test_a_stale_label_seen_by_two_loops_is_touched_only_by_the_claim_holder(self):
        """Two loops on one store admit the same stale label. The one whose
        `store.claim()` lands takes the stale one off and writes its own;
        the other reaches its claim a moment later -- here, at the instant
        the first is writing its label -- waits its turn, and is refused
        by the store before it has touched the board. The loser's
        `_claim_run()` is the real one, on a thread of its own as a
        sibling loop's would be: the witness is that the provider saw no
        call from it at all."""
        self.seed_ended_run()
        db, target, project = self.db, self.target, self.project
        seen = type("Seen", (), {"trips": (), "watched": ()})()
        competitor = []
        threads = []

        def compete(provider, issue_id):
            conn = store.open(str(db))
            try:
                project_id = tickets.ensure_project(conn, StubProvider.TEAM,
                                               str(target))
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIssueId = ?",
                    (issue_id,)).fetchone()
                competitor.append(holophyte.loop.claim._claim_run(
                    project, conn, project_id, provider, a_task(), ticket_id, seen))
            finally:
                conn.close()

        class Contended(StubProvider):
            def label_issue(self, issue_id, name):
                if not threads:
                    thread = threading.Thread(target=compete,
                                              args=(self, issue_id))
                    threads.append(thread)
                    thread.start()
                    # The competitor is at its claim and stays there: the
                    # turn is this claim's until its label is written.
                    thread.join(0.5)
                    self.assertion = (thread.is_alive(), list(competitor))
                super().label_issue(issue_id, name)

        provider = self.labelled(Contended, [self.label()])
        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)
        threads[0].join(10)

        self.assertEqual(provider.assertion, (True, []))
        self.assertEqual(competitor, [holophyte.loop.claim.HELD])
        self.assertIn("lease already held by run 2; skipping it", out)
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "failed"), (2, "merged")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        # Every board call is the winner's; the loser made none.
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], [])

    def test_a_late_close_out_leaves_the_label_a_fresh_claim_re_asserted(self):
        """The label names the writer, not the run, so the store decides
        whether a close-out may take it off: run 1's release, arriving
        after run 2 has claimed the same ticket and re-asserted the label,
        finds the store naming run 2 as the live one and leaves the label
        on; run 2's own release takes it off."""
        ended = self.seed_ended_run()
        conn = store.open(str(self.db))
        try:
            project_id = tickets.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            (ticket_id,) = conn.execute("SELECT id FROM tickets").fetchone()
            live = store.claim(conn, project_id, ticket_id)
            conn.commit()
            provider = StubProvider(a_task())
            provider.label_issue("iss-131", "holo:writer-1")

            holophyte.board.projection.release_lease_label(self.project, conn,
                                                ticket_id, provider, ended)
            self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])

            holophyte.board.projection.release_lease_label(self.project, conn,
                                                ticket_id, provider, live)
            self.assertEqual(provider.labels["iss-131"], [])
        finally:
            conn.close()

    def test_a_close_out_racing_a_fresh_claim_cannot_strip_the_fresh_label(self):
        """Run 1's close-out looks at the store, finds no live run, and
        goes to the board -- and at that instant a sibling loop on this
        store reaches its claim of the same ticket. Were the claim to land
        in the gap, the removal would take the fresh run's label off and
        another writer could claim a ticket this store is working (review
        finding P1 on KO-351). The claim waits the close-out's turn
        instead: while the removal is in flight the store still names no
        live run, and the fresh claim's label is written after the removal
        and stays on the board."""
        ended = self.seed_ended_run()
        db, target, project = self.db, self.target, self.project
        seen = type("Seen", (), {"trips": (), "watched": ()})()
        claimed = []
        threads = []
        during = []

        def claim(provider, issue_id):
            conn = store.open(str(db))
            try:
                project_id = tickets.ensure_project(conn, StubProvider.TEAM,
                                               str(target))
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIssueId = ?",
                    (issue_id,)).fetchone()
                claimed.append(holophyte.loop.claim._claim_run(
                    project, conn, project_id, provider, a_task(), ticket_id, seen))
            finally:
                conn.close()

        class Racing(StubProvider):
            def unlabel_issue(self, issue_id, name):
                if not threads:
                    thread = threading.Thread(target=claim,
                                              args=(self, issue_id))
                    threads.append(thread)
                    thread.start()
                    thread.join(0.5)
                    peek = sqlite3.connect(db)
                    try:
                        during.append(peek.execute(
                            "SELECT activeRunId FROM tickets").fetchall())
                    finally:
                        peek.close()
                super().unlabel_issue(issue_id, name)

        provider = Racing(a_task())
        provider.label_issue("iss-131", "holo:writer-1")
        conn = store.open(str(self.db))
        try:
            (ticket_id,) = conn.execute("SELECT id FROM tickets").fetchone()
            holophyte.board.projection.release_lease_label(self.project, conn,
                                                ticket_id, provider, ended)
        finally:
            conn.close()
        threads[0].join(10)

        # No live run while the removal was in flight: the claim waited.
        self.assertEqual(during, [[(None,)]])
        self.assertEqual(claimed, [2])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(2,)])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])

    def test_a_foreign_label_on_read_back_backs_off_removing_only_our_own_label(self):
        """The admission check reads the listing; the read-back reads the
        issue as it is. A `holo:writer-2` that landed in between is
        writer-2's lease: this writer's own label comes off and nothing
        else on the issue does, the store lease goes back without a
        strike, no run starts, and the loop moves on to the next ticket."""
        class Raced(StubProvider):
            def label_issue(self, issue_id, name):
                # writer-2 labels KO-131 after this writer's listing and
                # before its write; the read-back has it.
                if issue_id == "iss-131":
                    self.labels[issue_id].append("holo:writer-2")
                super().label_issue(issue_id, name)

        provider = self.labelled(Raced, ["other"], a_task(2))
        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)

        self.assertIn("[holo2] KO-131 is leased by writer-2 on the board;"
                      " skipping it", out)
        self.assertEqual(provider.labels["iss-131"], ["other", "holo:writer-2"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-132", "holo:writer-1"),
                          ("unlabel", "iss-132", "holo:writer-1")])
        self.assertEqual(
            self.read("SELECT t.linearIdentifier, r.outcome, r.outcomeClass"
                      " FROM runs r JOIN tickets t ON t.id = r.ticketId"
                      " ORDER BY r.id"),
            [("KO-131", "failed", "infra"), ("KO-132", "merged", "work")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"),
                         [(None,), (None,)])
        # Only KO-132 was worked: one implement and one review turn.
        self.assertEqual(self.last_fake.roles, ["implement", "review"])
        self.assertEqual(provider.labels["iss-132"], [])

    def test_a_label_the_board_refuses_releases_the_lease_and_starts_no_run(self):
        """The add itself raised. The store lease goes back and no run
        starts; the removal that follows is best-effort, since a raise is
        not proof that nothing landed (review finding P2 on KO-351)."""
        class Refusing(StubProvider):
            def label_issue(self, issue_id, name):
                self.label_calls.append(("label", issue_id, name))
                raise RuntimeError("linear is down")

        provider = Refusing(a_task())
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.read_calls, [])
        self.assertEqual(self.branches(), ["main"])

    def test_an_add_that_landed_before_it_raised_is_taken_off_again(self):
        """The mutation applied and the response failed -- a timeout after
        the write. The claim is refused as before, and the label the board
        does hold comes off, so a refused claim does not leave a lease
        every other writer will honour forever."""
        class Landed(StubProvider):
            def label_issue(self, issue_id, name):
                super().label_issue(issue_id, name)
                raise RuntimeError("linear timed out after the write")

        provider = self.labelled(Landed, ["other"])
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.labels["iss-131"], ["other"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])

    def test_a_read_back_the_board_refuses_ends_with_our_label_off_and_no_lease(self):
        """The add landed and the read-back raised. The store lease goes
        back as before; this writer's label goes with it -- and only that
        one: the issue's other labels are not touched."""
        class HalfTaken(StubProvider):
            def issue_labels(self, issue_id):
                self.read_calls.append(issue_id)
                raise RuntimeError("linear timed out on the read-back")

        provider = self.labelled(HalfTaken, ["other"])
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.labels["iss-131"], ["other"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])

    def test_a_read_back_failure_tries_the_removal_once_and_still_releases(self):
        """The board is down for the read-back and for the removal that
        follows: one removal attempt, not a retry loop, and the store
        lease still goes back -- the label left behind is this writer's
        next claim's stale one."""
        class Down(StubProvider):
            def issue_labels(self, issue_id):
                raise RuntimeError("linear timed out on the read-back")

            def unlabel_issue(self, issue_id, name):
                self.label_calls.append(("unlabel", issue_id, name))
                raise RuntimeError("linear is down")

        provider = Down(a_task())
        self.main_output(Commit("never reached"), APPROVE, provider=provider)

        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM runEvents WHERE kind = 'warning'"
                      " AND summary LIKE '%could not be removed%'"), [(1,)])

    def test_a_requeue_takes_off_the_label_before_the_ticket_is_claimable(self):
        """`--requeue KO-131` removes this writer's label -- run 1's, the
        one whose close-out the board missed -- while the ticket is still
        `in_flight` and no loop can claim it, so a fresh claim's label can
        never be the one it takes off; nothing else on the issue moves."""
        ended = self.seed_ended_run(requeue=False)
        db = self.db
        status_at_removal = []

        class Watched(StubProvider):
            def unlabel_issue(self, issue_id, name):
                conn = store.open(str(db))
                try:
                    status_at_removal.append(conn.execute(
                        "SELECT status FROM tickets WHERE linearIssueId = ?",
                        (issue_id,)).fetchone()[0])
                finally:
                    conn.close()
                super().unlabel_issue(issue_id, name)

        provider = self.labelled(Watched, ["other", self.label()])
        out = io.StringIO()
        holophyte.cli.operator.requeue(self.project, "KO-131", "board back", out,
                               provider=provider)

        self.assertEqual(out.getvalue().strip(),
                         f"[holo2] KO-131 requeued after run {ended}")
        self.assertEqual(status_at_removal, ["in_flight"])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["other"])

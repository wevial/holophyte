"""`factory.py TARGET --requeue KO-n --note TEXT`: the failed ticket goes back
in the queue with its intervention row, through the command line.

The mode is the store's `requeue()` behind argparse, so what is tested here
is the wiring: the identifier resolves in the target's store, the requeued
line is printed, a refusal is a non-zero exit naming the reason with nothing
written, a target with no `[board]` table exits naming the key before the
store is touched, and a `--requeue` with no `--note` never reaches the store
at all. The claim that follows a requeue opens its implement prompt with
the note and the failed run's last unresolved findings (KO-718).

Run: python3 -m unittest discover -s tests -p 'test_cli_*' -v
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.board
import holophyte.cli
import holophyte.project
import holophyte.stop
import linear_provider
import store
import store.read
import store.tickets
from holophyte.config_tables import board_config
from holophyte.runs import open_store
from tests.fake_agent import APPROVE, Commit
from tests.loop_fixture import LoopFixture, StubProvider, a_task
from tests.phase_fixture import park_run

MINUTE = 60 * 1000
T0 = 1_700_000_000_000


class StubBoard:
    """The board `cli()` asks `board_for()` for, recording the label calls."""

    instance = None

    def __init__(self, team):
        self.team = team
        self.unlabelled = []
        StubBoard.instance = self

    def unlabel_issue(self, issue_id, name):
        self.unlabelled.append((issue_id, name))


def stub_board_for(target):
    """`board_for()` with the stub: no `[board]` table is no board."""
    settings = board_config(target)
    return StubBoard(settings.team) if settings is not None else None


class RequeueCliTests(unittest.TestCase):
    """A target with a store holding one ticket and its ended (or live) run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.dict(os.environ,
                             {"HOLOPHYTE_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.target = holophyte.project.Project.locate(self.repo)
        self.with_board()
        conn = open_store(self.target)
        self.addCleanup(conn.close)
        self.conn = conn
        self.project_id = store.tickets.ensure_project(conn, "team-1", self.repo)
        self.ticket = store.tickets.mirror_ticket(
            conn, self.project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)
        store.tickets.transition(conn, self.ticket, "in_flight")
        self.run = store.claim(conn, self.project_id, self.ticket, now=T0)

    def with_board(self):
        self.target.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.target.config_path.write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n')

    def fail_the_run(self):
        store.release(self.conn, self.run, "failed", "verify failed",
                      now=T0 + MINUTE)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        # The board `cli()` builds stands in for Linear: `--requeue` takes
        # the requeued ticket's lease label off it (KO-351), and the stub
        # records the call instead of reaching for the network.
        self.board = StubBoard.instance = None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch.object(holophyte.cli, "board_for", stub_board_for):
            holophyte.cli.cli([str(self.repo), *args])
        self.board = StubBoard.instance
        return out.getvalue(), err.getvalue()

    def interventions(self):
        return self.conn.execute(
            'SELECT runId, "action" FROM interventions'
            " WHERE action != 'migrate'").fetchall()

    def status(self):
        return self.conn.execute(
            "SELECT status FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone()[0]

    def test_shelved_board_state_refuses_requeue_without_writes(self):
        self.fail_the_run()
        for state in ("Backlog", "Canceled", "Done"):
            with self.subTest(state=state):
                task = linear_provider.parse_task({
                    "identifier": "KO-1", "id": "issue-1", "title": "a ticket",
                    "description": "", "state": {"name": state}})
                holophyte.board.mirror_task(self.conn, self.project_id, task)
                before = list(self.conn.iterdump())
                with self.assertRaisesRegex(SystemExit, state):
                    self.cli("--requeue", "KO-1", "--note", "retry")
                self.assertEqual(list(self.conn.iterdump()), before)
                self.assertEqual(StubBoard.instance.unlabelled, [])

    def test_requeue_walks_the_failed_ticket_to_ready_with_its_row(self):
        self.fail_the_run()

        out, _ = self.cli("--requeue", "KO-1", "--note", "contract fixed")

        self.assertEqual(out.strip(), f"[holo2] KO-1 requeued after run {self.run}")
        self.assertEqual(self.status(), "ready")
        # The board lease goes with the store's: the requeued issue's
        # `holo:` label for this writer is taken off (KO-351).
        self.assertEqual([name.startswith("holo:") for _, name in
                          self.board.unlabelled], [True])
        self.assertEqual(self.board.unlabelled[0][0], "issue-1")
        self.assertEqual(self.interventions(), [(self.run, "requeue")])
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertIn("contract fixed", summary)
        # The narrative's copy, beside the interventions row: the note is the
        # ledger entry's text and the row names the same run.
        entries = store.read.ledger(self.conn, self.run)
        self.assertEqual([(e.kind, e.source) for e in entries],
                         [("intervention", "operator")])
        self.assertIn("contract fixed", entries[0].text)
        self.assertIn("requeue", entries[0].text)
        (intervention_at,) = self.conn.execute(
            "SELECT at FROM interventions WHERE runId = ?",
            (self.run,)).fetchone()
        self.assertEqual(entries[0].at, intervention_at)

    def test_a_requeue_of_a_run_with_a_pull_request_names_it_in_the_note(self):
        """KO-407: a failed run that left its branch open as a pull request
        (one that adopted the PR, or parked on it, before it failed) keeps
        the link while the ticket waits: the requeue's intervention note
        names the PR URL."""
        url = "https://github.com/example/repo/pull/2177"
        park_run(self.conn, self.run, "awaiting_merge_approval",
                   "parked on its pull request", pr_url=url)
        store.release(self.conn, self.run, "failed",
                      "the babysit pass died", now=T0 + MINUTE)

        out, _ = self.cli("--requeue", "KO-1", "--note", "github went down")

        self.assertEqual(out.strip(),
                         f"[holo2] KO-1 requeued after run {self.run}")
        self.assertEqual(self.status(), "ready")
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertIn("github went down", summary)
        self.assertIn(url, summary)
        entries = store.read.ledger(self.conn, self.run)
        self.assertEqual(entries[-1].kind, "intervention")
        self.assertIn(url, entries[-1].text)

    def test_requeue_clears_a_question_after_a_failed_merge_gate(self):
        store.set_phase(self.conn, self.run, "merge_gate", now=T0)
        store.release(self.conn, self.run, "failed", "merge lock timed out",
                      now=T0 + MINUTE)
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")
        self.conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                          ("Retry the merge lock wait?", self.ticket))
        # Also cover the contract's failed row retaining its work phase.
        self.conn.execute("UPDATE runs SET phase = 'merge_gate' WHERE id = ?",
                          (self.run,))
        self.conn.commit()

        self.cli("--requeue", "KO-1", "--note", "lock released")

        self.assertEqual(self.conn.execute(
            "SELECT status, blockedQuestion FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone(), ("ready", None))
        self.assertEqual(self.interventions(), [(self.run, "requeue")])
        self.assertEqual(self.board.unlabelled[0][0], "issue-1")
        self.assertIn("lock released", store.read.ledger(self.conn, self.run)[-1].text)

    def test_requeue_walks_an_aborted_ticket_to_ready_and_clears_its_question(self):
        """KO-719: `--abort` ends the run `abandoned` and parks the ticket
        with the note as its question; `--requeue` is the way back."""
        store.abort(self.conn, self.run, "claimed before the body was fixed",
                    now=T0 + MINUTE)
        with self.assertRaises(holophyte.stop.Aborted):
            holophyte.stop.end_aborted(self.conn, self.run)
        self.assertEqual(self.conn.execute(
            "SELECT status, blockedQuestion FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone(),
            ("blocked_on_operator", "claimed before the body was fixed"))

        out, _ = self.cli("--requeue", "KO-1", "--note", "body corrected")

        self.assertEqual(out.strip(), f"[holo2] KO-1 requeued after run {self.run}")
        self.assertEqual(self.conn.execute(
            "SELECT status, blockedQuestion FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone(), ("ready", None))
        self.assertEqual(self.interventions(),
                         [(self.run, "abort"), (self.run, "requeue")])
        self.assertIn("body corrected",
                      store.read.ledger(self.conn, self.run)[-1].text)

    def test_requeue_refuses_an_abandoned_run_that_was_not_aborted(self):
        store.release(self.conn, self.run, "abandoned", "canceled on the board",
                      now=T0 + MINUTE)
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")
        self.conn.commit()
        before = list(self.conn.iterdump())

        with self.assertRaises(SystemExit) as raised:
            self.cli("--requeue", "KO-1", "--note", "retry")

        self.assertIn("not aborted", str(raised.exception))
        self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(StubBoard.instance.unlabelled, [])

    def test_requeue_refuses_parked_candidates_and_pull_requests_without_writes(self):
        # Only a `not_reproduced` park is admitted (KO-658); a merge question
        # still names the command that answers it.
        park_run(self.conn, self.run, "awaiting_merge_approval",
                   "merge?", candidate_sha="a" * 40, now=T0,
                   park_kind="question")
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")
        self.conn.execute("UPDATE tickets SET blockedQuestion = 'merge?' WHERE id = ?",
                          (self.ticket,))
        for pr_url, guidance in (
                (None, "--approve or --babysit"),
                ("https://github.com/example/repo/pull/1", "--babysit")):
            with self.subTest(pr_url=pr_url):
                self.conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?",
                                  (pr_url, self.run))
                self.conn.commit()
                before = list(self.conn.iterdump())
                with self.assertRaises(SystemExit) as raised:
                    self.cli("--requeue", "KO-1", "--note", "retry")
                self.assertIn(
                    f"KO-1 is parked awaiting merge approval; use {guidance}",
                    str(raised.exception))
                self.assertEqual(list(self.conn.iterdump()), before)
                self.assertEqual(StubBoard.instance.unlabelled, [])

    def test_a_target_with_no_board_exits_naming_the_key_and_writes_nothing(self):
        self.fail_the_run()
        self.target.config_path.unlink()
        before = self.status()

        with patch.dict(os.environ, {"HOLO2_PROJECT_ID": "p-env",
                                     "HOLO2_TEAM": "T"}), \
                self.assertRaises(SystemExit) as raised:
            self.cli("--requeue", "KO-1", "--note", "contract fixed")

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("[board] project_id", str(raised.exception))
        self.assertEqual(self.status(), before)
        self.assertEqual(self.interventions(), [])

    def test_each_refusal_exits_non_zero_naming_it_and_writes_nothing(self):
        # A live run first, then the same ticket once it is ready, then an
        # identifier the store never mirrored: three refusals, no row.
        with self.assertRaises(SystemExit) as live:
            self.cli("--requeue", "KO-1", "--note", "too soon")
        self.assertIn("still live", str(live.exception))
        self.assertEqual(self.status(), "in_flight")

        self.fail_the_run()
        self.cli("--requeue", "KO-1", "--note", "contract fixed")
        before = self.interventions()
        with self.assertRaises(SystemExit) as ready:
            self.cli("--requeue", "KO-1", "--note", "again")
        self.assertIn("is ready", str(ready.exception))

        with self.assertRaises(SystemExit) as unknown:
            self.cli("--requeue", "KO-404", "--note", "who?")
        self.assertIn("KO-404", str(unknown.exception))
        for raised in (live, ready, unknown):
            self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(self.interventions(), before)

    def test_requeue_without_a_note_is_an_argparse_error(self):
        self.fail_the_run()
        with patch.object(store, "requeue") as requeue:
            with self.assertRaises(SystemExit) as raised:
                _, err = self.cli("--requeue", "KO-1")
        # argparse's usage exit, before any store is opened.
        self.assertEqual(raised.exception.code, 2)
        requeue.assert_not_called()
        self.assertEqual(self.status(), "in_flight")

    def test_a_note_without_requeue_is_an_argparse_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.cli("--report", "--note", "stray")
        self.assertEqual(raised.exception.code, 2)


class RequeuedClaimTests(LoopFixture):
    """KO-718: the claim after a requeue opens the implement prompt with the
    operator's note and the failed run's last unresolved findings; the fake
    implementer records the prompt the loop hands it."""

    def fail_a_run(self, *rounds, note=None):
        """KO-131's first run, failed after `rounds` of (verdict, messages),
        then requeued with `note` when one is given; return the run id."""
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        project_id = store.tickets.ensure_project(
            conn, StubProvider.TEAM, self.target)
        ticket = holophyte.board.mirror_task(conn, project_id, a_task())
        run = store.claim(conn, project_id, ticket, now=T0)
        store.tickets.transition(conn, ticket, "in_flight")
        for n, (verdict, messages) in enumerate(rounds, 1):
            store.record_review_round(
                conn, run, n, verdict, "reviewer-model",
                findings=[{"path": f"app{i}.py", "severity": "p1",
                           "message": m} for i, m in enumerate(messages)],
                started_at=T0, ended_at=T0 + MINUTE)
        store.release(conn, run, "failed", "review rejected", now=T0 + MINUTE)
        if note is not None:
            store.requeue(conn, ticket, note, now=T0 + 2 * MINUTE)
        conn.commit()
        return run

    def implement_prompt(self):
        fake, _ = self.loop(Commit("the thing", path="app.txt"), APPROVE)
        self.assertEqual(fake.roles[0], "implement")
        return fake.turns[0].goal

    def test_the_note_and_last_findings_open_the_prompt_before_the_ticket(self):
        run = self.fail_a_run(
            ("changes_requested", ["an earlier round's finding"]),
            ("changes_requested", ["the parser crashes on empty input",
                                   "the test mirrors the implementation"]),
            note="fix the parser crash")

        prompt = self.implement_prompt()

        ticket_at = prompt.index("Implement this task in this repo:")
        for text in (f"(run {run})", "fix the parser crash",
                     "the parser crashes on empty input",
                     "the test mirrors the implementation", "round 2"):
            self.assertIn(text, prompt[:ticket_at])
        self.assertNotIn("an earlier round's finding", prompt)

    def test_a_first_claim_gets_no_block(self):
        self.assertNotIn("previous attempt", self.implement_prompt())

    def test_a_requeue_with_no_review_rounds_gets_the_note_alone(self):
        run = self.fail_a_run(note="fix the parser crash")

        prompt = self.implement_prompt()

        opening = prompt[:prompt.index("Implement this task in this repo:")]
        self.assertIn(f"Context from the previous attempt (run {run})", opening)
        self.assertIn("fix the parser crash", opening)
        self.assertNotIn("findings", opening)

    def test_unreadable_stored_findings_still_let_the_note_through(self):
        run = self.fail_a_run(("changes_requested", ["lost"]),
                              note="fix the parser crash")
        conn = open_store(self.project)
        self.addCleanup(conn.close)
        conn.execute("UPDATE reviewRounds SET findings = '{not json'"
                     " WHERE runId = ?", (run,))
        conn.commit()

        opening = self.implement_prompt().split(
            "Implement this task in this repo:")[0]

        self.assertIn(f"Context from the previous attempt (run {run})", opening)
        self.assertIn("fix the parser crash", opening)

    def test_findings_past_the_cap_are_cut_and_say_so(self):
        long = ["x" * 1000, "y" * 1000]
        self.fail_a_run(("changes_requested", long), note="retry")

        prompt = self.implement_prompt()

        self.assertIn("findings cut to 1,500 characters", prompt)
        self.assertIn("x" * 1000, prompt)
        self.assertIn("y" * 490, prompt)
        self.assertNotIn("y" * 500, prompt)


if __name__ == "__main__":
    unittest.main()

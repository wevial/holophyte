"""`factory.py TARGET --approve KO-n [--note TEXT]`: the ticket parked for
merge approval is released, through the command line.

The mode is the store's `approve()` behind argparse, so what is tested here
is the wiring and the contract the ticket states: a parked ticket gets its
`approve` intervention row with the note, its run's resume point at the
merge gate and its status back to `ready`; a ticket in any other state --
ready, in flight, merged -- is refused with a non-zero exit naming that
state and nothing written; a target with no `[board]` table exits naming
the key before the store is touched.

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

import holophyte.cli.arguments
import holophyte.cli.entry
import holophyte.cli.report
import holophyte.config.project
import holophyte.serve.serve_runs
import store
import store.read
import store.tickets
from holophyte.loop.runs import open_store
from tests.host_fixture import git
from tests.phase_fixture import finish_run, park_run
from tests.test_serve_merge import FakeGithub

BRANCH = "task/ko-1-a-ticket"
PULL = "https://github.com/example/repo/pull/7"

MINUTE = 60 * 1000
T0 = 1_700_000_000_000


class ApproveCliTests(unittest.TestCase):
    """A target with a store holding one ticket, parked or otherwise."""

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
        self.target = holophyte.config.project.Project.locate(self.repo)
        self.target.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.target.config_path.write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n')
        conn = open_store(self.target)
        self.addCleanup(conn.close)
        self.conn = conn
        self.project_id = store.tickets.ensure_project(conn, "team-1", self.repo)
        self.github = FakeGithub()
        self.github.install(self)
        self.ticket = store.tickets.mirror_ticket(
            conn, self.project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)

    def claim(self):
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, self.project_id, self.ticket, now=T0)
        return self.run

    def park(self, pr_url=None):
        """The state `[merge] approve = "human"` leaves: the run walked to
        the gate and parked there, the ticket blocked asking `merge?`;
        `pr_url` is what `[merge] mode = "pr"` records on the run."""
        self.claim()
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, self.run, phase, now=T0 + MINUTE)
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")
        self.conn.execute(
            "UPDATE tickets SET blockedQuestion = 'merge?' WHERE id = ?",
            (self.ticket,))
        self.conn.commit()
        park_run(self.conn, self.run, "awaiting_merge_approval",
                   now=T0 + 2 * MINUTE, pr_url=pr_url)

    def park_at_pull_request(self):
        """`park(PULL)` under `[merge] mode = "pr"` and `approve = "human"`,
        its branch pushed to a real bare `origin` at the parked candidate;
        GitHub's API answers through `self.github`, ready until told not."""
        with self.target.config_path.open("a") as config:
            config.write('[merge]\nmode = "pr"\napprove = "human"\n')
        origin = self.root / "origin.git"
        git(self.root, "init", "--bare", "-b", "main", str(origin))
        git(self.repo, "init", "-b", BRANCH)
        (self.repo / "candidate").write_text("candidate\n")
        git(self.repo, "add", "candidate")
        git(self.repo, "commit", "-m", "candidate")
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "origin", BRANCH)
        sha = git(self.repo, "rev-parse", "HEAD")
        self.park(pr_url=PULL)
        store.set_branch(self.conn, self.run, BRANCH)
        self.conn.execute("UPDATE runs SET candidateSha = ? WHERE id = ?",
                          (sha, self.run))
        self.conn.commit()
        self.github.head = sha

    def dump(self):
        return list(self.conn.iterdump())

    def intervention_note(self):
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        return summary.removeprefix("human approve: ")

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            holophyte.cli.entry.cli([str(self.repo), *args])
        return out.getvalue(), err.getvalue()

    def interventions(self):
        return self.conn.execute(
            'SELECT runId, "action" FROM interventions'
            " WHERE action != 'migrate'").fetchall()

    def ticket_row(self):
        return self.conn.execute(
            "SELECT status, activeRunId, lastRunId FROM tickets WHERE id = ?",
            (self.ticket,)).fetchone()

    def run_row(self):
        return self.conn.execute(
            "SELECT phase, outcome, resumePhase, endedAt FROM runs"
            " WHERE id = ?", (self.run,)).fetchone()

    def test_approve_releases_the_parked_run_and_readies_the_ticket(self):
        self.park()

        out, _ = self.cli("--approve", "KO-1", "--note", "ok")

        self.assertIn(f"KO-1 approved: run {self.run}", out)
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertEqual(summary, "human approve: ok")
        phase, outcome, resume_phase, ended = self.run_row()
        self.assertEqual(resume_phase, "merge_gate")
        self.assertNotEqual(phase, "awaiting_merge_approval")
        self.assertIsNotNone(ended)
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))
        self.assertEqual(self.conn.execute(
            "SELECT parkKind FROM runs WHERE id = ?", (self.run,)).fetchone(),
            (None,))
        # Released, the ticket is claimable again -- the loop's next pass
        # is what takes the candidate to the gate.
        self.assertTrue(store.tickets.pickable(self.conn, self.ticket))

    def test_a_pull_request_github_shows_ready_is_released(self):
        self.park_at_pull_request()

        out, _ = self.cli("--approve", "KO-1", "--note", "ok")

        self.assertIn(f"KO-1 approved: run {self.run}", out)
        self.assertIn(("graphql",), [call[:1] for call in self.github.calls])
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        self.assertEqual(self.intervention_note(), "ok")
        self.assertEqual(self.run_row()[2], "merge_gate")
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))

    def test_a_pull_request_not_ready_is_refused_naming_why(self):
        self.park_at_pull_request()
        self.github.review = None
        before = self.dump()

        with self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", "--note", "ok")

        # A message as SystemExit's code is how the interpreter exits 1.
        self.assertIsInstance(raised.exception.code, str)
        self.assertIn("KO-1: review not approved", raised.exception.code)
        self.assertEqual(self.dump(), before)

    def test_an_unreadable_github_refuses_and_force_releases_past_it(self):
        self.park_at_pull_request()
        self.github.unreachable = True
        before = self.dump()

        with self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1")

        self.assertIn("KO-1: github unreadable", str(raised.exception))
        self.assertEqual(self.dump(), before)
        self.cli("--approve", "KO-1", "--force", "--note", "GitHub is down")
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        self.assertEqual(self.intervention_note(), "forced past readiness:"
                         " github_unreadable; GitHub is down")

    def test_force_releases_a_pull_request_not_ready_recording_the_reason(self):
        self.park_at_pull_request()
        self.github.review = "REVIEW_REQUIRED"

        out, _ = self.cli("--approve", "KO-1", "--force", "--note",
                          "teammate approved in chat")

        self.assertIn(f"KO-1 approved: run {self.run}", out)
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        note = self.intervention_note()
        self.assertTrue(note.startswith(
            "forced past readiness: review_not_approved"), note)
        self.assertIn("teammate approved in chat", note)
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))

    def test_force_without_a_note_is_an_argparse_error_writing_nothing(self):
        self.park_at_pull_request()
        self.github.review = None
        before = self.dump()
        for note in ((), ("--note", "  ")):
            with self.subTest(note=note), \
                    self.assertRaises(SystemExit) as raised:
                self.cli("--approve", "KO-1", "--force", *note)
            self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.github.calls, [])

    def test_babysit_releases_the_parked_run_without_approving(self):
        """`--babysit KO-n` is `--approve`'s twin with its own action: the
        same release and resume point, a `babysit` intervention row, and
        the candidate the next claim carries is not marked approved."""
        self.park(pr_url="https://example.test/pull/7")

        out, _ = self.cli("--babysit", "KO-1")

        self.assertIn(f"KO-1 sent back to the babysitter: run {self.run}", out)
        self.assertEqual(self.interventions(), [(self.run, "babysit")])
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertEqual(summary, "human babysit: sent back to the babysitter")
        phase, outcome, resume_phase, ended = self.run_row()
        self.assertEqual((outcome, resume_phase), ("abandoned", "merge_gate"))
        self.assertIsNotNone(ended)
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))
        carried = store.read.approved_candidate(self.conn, self.ticket,
                                                self.run + 1)
        self.assertEqual((carried.run_id, carried.approved),
                         (self.run, False))

    def test_approval_stamp_survives_activity_and_babysit_clears_it(self):
        self.park(pr_url="https://example.test/pull/7")
        store.babysit(self.conn, self.ticket, "look again", now=T0 + 3 * MINUTE)
        store.record_intervention(self.conn, self.run, "launch_loop",
                                  "resume", source="supervisor")
        self.assertFalse(store.read.approved_candidate(
            self.conn, self.ticket, self.run + 1).approved)

        def repark():
            # Re-create the parked boundary on this candidate to exercise
            # each release's writes against an existing stamp.
            self.conn.execute("UPDATE runs SET phase = 'awaiting_merge_approval',"
                              " outcome = NULL, endedAt = NULL WHERE id = ?",
                              (self.run,))
            store.tickets.walk_ticket(self.conn, self.ticket,
                                      "blocked_on_operator")
            self.conn.commit()

        def stamp():
            return self.conn.execute(
                "SELECT approvedAt, approvedBy FROM runs WHERE id = ?",
                (self.run,)).fetchone()

        self.assertEqual(stamp(), (None, None))
        repark()
        with patch("getpass.getuser", return_value="operator"):
            store.approve(self.conn, self.ticket, "merge",
                          now=T0 + 4 * MINUTE)
        store.record_intervention(self.conn, self.run, "operator_note",
                                  "later activity")
        self.assertEqual(stamp(), (T0 + 4 * MINUTE, "operator"))
        self.assertIn(
            f"KO-1 run {self.run}: approved by operator at 2023-11-14T22:17:20+00:00",
            holophyte.cli.report.report_lines(self.conn))
        status, detail = holophyte.serve.serve_runs.run_detail(
            self.target, str(self.run))
        self.assertEqual(status, 200)
        self.assertEqual((detail["run"]["approved_at"],
                          detail["run"]["approved_by"]),
                         (T0 + 4 * MINUTE, "operator"))
        self.assertTrue(store.read.approved_candidate(
            self.conn, self.ticket, self.run + 1).approved)
        repark()
        store.babysit(self.conn, self.ticket, "look again")
        store.record_intervention(self.conn, self.run, "launch_loop",
                                  "resume", source="supervisor")
        self.assertEqual(stamp(), (None, None))
        self.assertFalse(store.read.approved_candidate(
            self.conn, self.ticket, self.run + 1).approved)

        repark()
        store.approve(self.conn, self.ticket, "merge")
        self.conn.execute("UPDATE runs SET outcome = 'failed' WHERE id = ?",
                          (self.run,))
        store.tickets.walk_ticket(self.conn, self.ticket, "in_flight")
        self.conn.commit()
        store.requeue(self.conn, self.ticket, "retry failed candidate")
        self.assertEqual(stamp(), (None, None))

    def test_a_babysitter_of_a_run_parked_with_no_pull_request_is_refused(self):
        """A run parked under `[merge] mode = "local"` has no threads to look
        at again, and releasing it would take the candidate through the
        local gate, which merges: `--babysit` exits naming the missing PR
        with nothing written, and the ticket stays parked for `--approve`."""
        self.park()

        with self.assertRaises(SystemExit) as raised:
            self.cli("--babysit", "KO-1", "--note", "bots are done")

        self.assertIn("no pull request", str(raised.exception))
        self.assertIn("--approve", str(raised.exception))
        self.assertEqual(self.interventions(), [])
        phase, outcome, resume_phase, ended = self.run_row()
        self.assertEqual((phase, outcome, resume_phase, ended),
                         ("awaiting_merge_approval", None, None, None))
        self.assertEqual(self.ticket_row(),
                         ("blocked_on_operator", None, self.run))
        self.assertFalse(store.tickets.pickable(self.conn, self.ticket))
        # `--approve` is still the answer for it.
        self.cli("--approve", "KO-1", "--note", "ok")
        self.assertEqual(self.interventions(), [(self.run, "approve")])

    def test_a_bare_babysitter_records_the_default_note_and_refuses_ready(self):
        self.park(pr_url="https://example.test/pull/7")
        self.cli("--babysit", "KO-1")
        self.assertEqual(self.interventions(), [(self.run, "babysit")])
        # The persisted note is the literal the ticket names, not whatever
        # the module's default happens to be.
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertEqual(summary, "human babysit: sent back to the babysitter")

        with self.assertRaises(SystemExit) as raised:
            self.cli("--babysit", "KO-1")

        self.assertIn("KO-1 is ready", str(raised.exception))
        self.assertEqual(len(self.interventions()), 1)

    def test_a_bare_approve_records_the_default_note(self):
        self.park()

        self.cli("--approve", "KO-1")

        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertEqual(
            summary, f"human approve: {holophyte.cli.arguments.APPROVE_DEFAULT_NOTE}")

    def test_a_ticket_not_parked_is_refused_naming_its_state(self):
        """Ready with no run, in flight with a live run, merged: each exits
        non-zero naming the ticket's state, and the store is untouched."""
        with self.assertRaises(SystemExit) as ready:
            self.cli("--approve", "KO-1", "--note", "ok")
        self.assertIn("KO-1 is ready", str(ready.exception))

        self.claim()
        with self.assertRaises(SystemExit) as live:
            self.cli("--approve", "KO-1", "--note", "ok")
        self.assertIn("KO-1 is in_flight", str(live.exception))
        self.assertIn(f"run {self.run} still live", str(live.exception))
        self.assertEqual(self.run_row()[:2], ("claimed", None))

        finish_run(self.conn, self.run, "merged", now=T0 + MINUTE)
        store.tickets.transition(self.conn, self.ticket, "merged")
        with self.assertRaises(SystemExit) as merged:
            self.cli("--approve", "KO-1", "--note", "ok")
        self.assertIn("KO-1 is merged, not blocked_on_operator",
                      str(merged.exception))
        self.assertEqual(self.ticket_row(), ("merged", None, self.run))

        with self.assertRaises(SystemExit) as unknown:
            self.cli("--approve", "KO-404")
        self.assertIn("KO-404", str(unknown.exception))

        for raised in (ready, live, merged, unknown):
            self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(self.interventions(), [])
        self.assertEqual(self.run_row()[2], None)

    def test_a_ticket_walked_off_the_park_by_hand_is_refused_by_status(self):
        """The run still sits in `awaiting_merge_approval`, but an operator
        walked the ticket on to `ready` from the REPL: the status decides,
        the refusal names it, and the parked run is left as it was."""
        self.park()
        store.tickets.walk_ticket(self.conn, self.ticket, "ready")
        self.assertEqual(self.run_row()[0], "awaiting_merge_approval")

        with self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", "--note", "ok")

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("KO-1 is ready, not blocked_on_operator",
                      str(raised.exception))
        self.assertEqual(self.interventions(), [])
        self.assertEqual(self.run_row(),
                         ("awaiting_merge_approval", None, None, None))
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))

    def test_a_target_with_no_board_exits_naming_the_key_and_writes_nothing(self):
        self.park()
        self.target.config_path.unlink()

        with patch.dict(os.environ, {"HOLO2_PROJECT_ID": "p-env",
                                     "HOLO2_TEAM": "T"}), \
                self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1")
        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("[board] project_id", str(raised.exception))
        self.assertEqual(self.ticket_row()[0], "blocked_on_operator")
        self.assertEqual(self.interventions(), [])

    def test_a_blank_note_with_approve_is_an_argparse_error(self):
        self.park()
        with self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", "--note", "  ")
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.interventions(), [])


if __name__ == "__main__":
    unittest.main()

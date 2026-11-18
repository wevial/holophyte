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
import json
import os
import subprocess
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
from holophyte.loop.branch_sync import _candidate_drift
from holophyte.loop.runs import open_store
from tests.fake_agent import Commit, Reply
from tests.host_fixture import git
from tests.loop_fixture import BRANCH as LOOP_BRANCH
from tests.loop_fixture import LoopFixture, StubProvider, a_task
from tests.phase_fixture import finish_run, park_run
from tests.test_serve_merge import FakeGithub, review_rule

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

    def park_at_pull_request(self, approve="human"):
        """`park(PULL)` under `[merge] mode = "pr"` and `approve`,
        its branch pushed to a real bare `origin` at the parked candidate;
        GitHub's API answers through `self.github`, ready until told not."""
        with self.target.config_path.open("a") as config:
            config.write(f'[merge]\nmode = "pr"\napprove = "{approve}"\n')
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
        self.assertEqual(self.github.calls, [])

    def test_a_pull_request_park_replacing_the_local_one_is_not_approved(self):
        """The local park is released elsewhere and a newer run parks on a
        pull request GitHub has not approved, between the command reading
        the park and its write: the write refuses the newer run."""
        self.park()
        local = self.run
        approve = store.approve

        def replaced_first(conn, ticket_id, note, **kwargs):
            approve(self.conn, self.ticket, "released elsewhere")
            self.park(pr_url=PULL)
            return approve(conn, ticket_id, note, **kwargs)

        with patch.object(store, "approve", replaced_first), \
                self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", "--note", "ok")

        self.assertIn(f"newest run is {self.run}, not run {local}",
                      str(raised.exception))
        self.assertEqual(self.interventions(), [(local, "approve")])
        self.assertEqual(self.run_row()[:2], ("awaiting_merge_approval", None))
        self.assertEqual(self.ticket_row(),
                         ("blocked_on_operator", None, self.run))

    def test_a_pull_request_github_shows_ready_is_released(self):
        self.park_at_pull_request()

        out, _ = self.cli("--approve", "KO-1", "--note", "ok")

        self.assertIn(f"KO-1 approved: run {self.run}", out)
        self.assertIn(("graphql",), [call[:1] for call in self.github.calls])
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        self.assertEqual(self.intervention_note(), "ok")
        self.assertEqual(self.run_row()[2], "merge_gate")
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))

    def test_an_exempt_required_review_is_approved_without_force(self):
        self.park_at_pull_request(approve="human")
        self.github.review = "REVIEW_REQUIRED"
        self.github.rules = [review_rule(24, 1)]
        self.github.rulesets = {
            24: {"name": "human-review", "current_user_can_bypass": "exempt"}}

        out, _ = self.cli("--approve", "KO-1", "--note", "ready to land")

        self.assertIn(f"KO-1 approved: run {self.run}", out)
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        self.assertEqual(self.intervention_note(), "ready to land")
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

    def test_a_draft_pull_request_is_refused_naming_draft(self):
        self.park_at_pull_request()
        self.github.draft = True
        before = self.dump()

        with self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", "--note", "ok")

        self.assertIn("KO-1: draft (the pull request is a draft)",
                      str(raised.exception))
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

    def test_an_auto_approve_pull_request_park_is_released_unread(self):
        self.park_at_pull_request(approve="auto")
        self.github.review = None

        out, _ = self.cli("--approve", "KO-1", "--note", "ok")

        self.assertIn(f"KO-1 approved: run {self.run}", out)
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        self.assertEqual(self.intervention_note(), "ok")
        self.assertEqual(self.run_row()[2], "merge_gate")
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))
        self.assertEqual(self.github.calls, [])

    def test_force_on_an_auto_approve_park_records_the_note_as_given(self):
        self.park_at_pull_request(approve="auto")
        self.github.review = None

        self.cli("--approve", "KO-1", "--force", "--note", "ok")

        self.assertEqual(self.interventions(), [(self.run, "approve")])
        self.assertEqual(self.intervention_note(), "ok")
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))
        self.assertEqual(self.github.calls, [])

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



STUCK = ("swept by the supervisor in phase reviewing: review_stuck (rounds 1"
         " and 2 share 0.90 of their findings (0.8 threshold)); branch and"
         " worktree preserved for a human")


class SweptApproveCliTests(unittest.TestCase):
    """A run the supervisor swept with its branch checked out where the next
    claim resumes it: an earlier candidate E judged by round 1, the
    candidate R judged by round 2, then a WIP commit W no round judged."""

    setUp = ApproveCliTests.setUp
    claim = ApproveCliTests.claim
    cli = ApproveCliTests.cli
    interventions = ApproveCliTests.interventions
    ticket_row = ApproveCliTests.ticket_row

    def commit(self, text, message):
        (self.wt / "work").write_text(text)
        git(self.wt, "add", "work")
        git(self.wt, "commit", "-m", message)
        return git(self.wt, "rev-parse", "HEAD")

    def branch_with_wip(self):
        git(self.repo, "init", "-b", "main")
        git(self.repo, "commit", "--allow-empty", "-m", "base")
        self.wt = holophyte.config.project.worktree_path(self.target, BRANCH)
        git(self.repo, "worktree", "add", "-b", BRANCH, str(self.wt))
        self.earlier = self.commit("first try\n", "an earlier candidate")
        self.reviewed = self.commit("reviewed\n", "the reviewed candidate")
        self.wip = self.commit("half an edit\n",
                               "WIP: implementer ended mid-edit")

    def judged(self, rnd, sha, statuses):
        store.record_event(
            self.conn, self.run, "blast_radius", f"round {rnd} blast radius",
            level="detail", payload=json.dumps(
                {"round": rnd, "sha": sha, "tier": "low"}))
        if statuses is not None:
            store.record_event(
                self.conn, self.run, "criteria", f"review round {rnd}",
                level="detail", payload=json.dumps({"round": rnd, "criteria": [
                    {"id": f"C{n}", "n": n, "status": status, "text": "t"}
                    for n, status in enumerate(statuses, 1)]}))
        store.record_review_round(
            self.conn, self.run, rnd,
            "error" if statuses is None else "changes_requested", "codex",
            findings=[{"path": "work", "line": 1, "severity": "p1",
                       "message": "the same finding"}],
            started_at=T0 + rnd, ended_at=T0 + rnd)

    def sweep(self, newest=("met", "met"), reason=STUCK, kind="swept"):
        """Round 2 judged R with `newest`; round 1 judged E with the
        opposite verdict, so reading any round but the newest shows."""
        self.claim()
        store.set_branch(self.conn, self.run, BRANCH)
        for phase in ("working", "verifying", "reviewing"):
            store.set_phase(self.conn, self.run, phase, now=T0 + MINUTE)
        oldest = (("met", "met") if newest is None or "unwitnessed" in newest
                  else ("met", "unwitnessed"))
        self.judged(1, self.earlier, oldest)
        self.judged(2, self.reviewed, newest)
        store.release(self.conn, self.run, "failed", reason,
                      now=T0 + 2 * MINUTE, failure_kind=kind)

    def ref(self, name):
        return subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", name], cwd=self.repo,
            capture_output=True, text=True).stdout.strip() or None

    def refused(self, *args):
        """The command exits non-zero and neither the store nor a ref changes."""
        store_before, tip = list(self.conn.iterdump()), self.ref(
            f"refs/heads/{BRANCH}")
        worktree_before = self.worktree_state()
        with self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", *args)
        self.assertNotEqual(raised.exception.code, 0)
        self.assertEqual(list(self.conn.iterdump()), store_before)
        self.assertEqual(self.worktree_state(), worktree_before)
        self.assertEqual(self.ref(f"refs/heads/{BRANCH}"), tip)
        self.assertIsNone(self.ref(f"refs/holophyte/dropped/{self.run}"))
        return str(raised.exception)

    def worktree_state(self):
        """What the worktree's HEAD names, its index and its status."""
        return (git(self.wt, "symbolic-ref", "HEAD"),
                git(self.wt, "rev-parse", "HEAD"),
                git(self.wt, "ls-files", "--stage"),
                git(self.wt, "status", "--porcelain", "--ignored"))

    def assert_released_at_reviewed(self):
        self.assertEqual(self.ref(f"refs/heads/{BRANCH}"), self.reviewed)
        self.assertEqual(self.ref(f"refs/holophyte/dropped/{self.run}"),
                         self.wip)
        self.assertEqual(self.ticket_row(), ("ready", None, self.run))
        self.assertTrue(store.tickets.pickable(self.conn, self.ticket))
        carried = store.read.approved_candidate(self.conn, self.ticket, -1)
        self.assertEqual((carried.run_id, carried.sha, carried.approved),
                         (self.run, self.reviewed, True))
        self.assertIsNone(_candidate_drift(self.wt, BRANCH, carried.sha))

    def test_a_stuck_run_is_approved_at_its_newest_reviewed_commit(self):
        self.branch_with_wip()
        self.sweep()

        out, _ = self.cli("--approve", "KO-1", "--note", "non-blocking")

        self.assert_released_at_reviewed()
        self.assertEqual(self.interventions(), [(self.run, "approve")])
        self.assertIn(f"approved at {self.reviewed}", out)
        self.assertIn(f"refs/holophyte/dropped/{self.run}: {self.wip[:7]}",
                      out)

    def test_an_unwitnessed_criterion_refuses_unless_forced(self):
        self.branch_with_wip()
        self.sweep(newest=("met", "unwitnessed"))

        self.assertIn("left C2 unwitnessed",
                      self.refused("--note", "non-blocking"))

        self.cli("--approve", "KO-1", "--force", "--note", "non-blocking")

        self.assert_released_at_reviewed()
        (summary,) = self.conn.execute(
            "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
            " 'intervention'", (self.run,)).fetchone()
        self.assertEqual(summary, "human approve: forced past criteria:"
                                  " C2 unwitnessed; non-blocking")

    def test_a_newest_round_whose_review_errored_refuses_unless_forced(self):
        self.branch_with_wip()
        self.sweep(newest=None)

        self.assertIn("no criterion judged", self.refused("--note", "ok"))

        self.cli("--approve", "KO-1", "--force", "--note", "non-blocking")

        self.assert_released_at_reviewed()

    def test_a_branch_not_descending_from_the_reviewed_commit_is_refused(self):
        self.branch_with_wip()
        git(self.wt, "reset", "--hard", "main")
        git(self.wt, "commit", "--allow-empty", "-m", "rewritten")
        self.sweep()

        self.assertIn("does not descend from the reviewed commit",
                      self.refused("--note", "ok"))

    def test_an_ignored_file_the_rewind_would_overwrite_is_refused(self):
        self.branch_with_wip()
        git(self.wt, "rm", "-q", "work")
        (self.wt / ".gitignore").write_text("work\n")
        git(self.wt, "add", ".gitignore")
        git(self.wt, "commit", "-m", "WIP: implementer ended mid-edit")
        (self.wt / "work").write_text("local only\n")
        self.sweep()

        self.assertIn("the rewind would overwrite work", self.refused("--note", "ok"))
        self.assertEqual((self.wt / "work").read_text(), "local only\n")

    def reviewed_then_ignored(self, name, pattern):
        """R tracks `name`; W stops tracking it and ignores `pattern`."""
        self.branch_with_wip()
        git(self.wt, "reset", "-q", "--hard", self.earlier)
        (self.wt / name).parent.mkdir(exist_ok=True)
        (self.wt / name).write_text("reviewed\n")
        git(self.wt, "add", "--", name)
        self.reviewed = self.commit("reviewed\n", "the reviewed candidate")
        git(self.wt, "rm", "-rq", "--", name.split("/")[0])
        (self.wt / ".gitignore").write_text(pattern + "\n")
        git(self.wt, "add", ".gitignore")
        git(self.wt, "commit", "-m", "WIP: implementer ended mid-edit")

    def test_an_ignored_file_with_a_non_ascii_carriage_return_name_is_kept(
            self):
        name = "r\u00e9sum\u00e9\r.txt"
        self.reviewed_then_ignored(name, "*.txt")
        (self.wt / name).write_text("local only\n")
        self.sweep()

        self.assertIn(f"the rewind would overwrite {name}",
                      self.refused("--note", "ok"))
        self.assertEqual((self.wt / name).read_text(), "local only\n")

    def test_an_ignored_file_where_a_reviewed_directory_goes_is_kept(self):
        self.reviewed_then_ignored("cache/input", "cache")
        (self.wt / "cache").write_text("local only\n")
        self.sweep()

        self.assertIn("the rewind would overwrite cache",
                      self.refused("--note", "ok"))
        self.assertEqual((self.wt / "cache").read_text(), "local only\n")

    def test_a_reviewed_file_with_a_non_utf8_name_is_rewound(self):
        name = os.fsdecode(b"caf\xe9")
        self.reviewed_then_ignored(name, "nothing")
        self.wip = git(self.wt, "rev-parse", "HEAD")
        self.sweep()

        self.cli("--approve", "KO-1", "--note", "ok")

        self.assert_released_at_reviewed()
        self.assertEqual((self.wt / name).read_text(), "reviewed\n")

    def test_a_tracked_directory_replacing_a_reviewed_file_is_rewound(self):
        self.branch_with_wip()
        git(self.wt, "rm", "-q", "work")
        (self.wt / "work").mkdir()
        (self.wt / "work" / "part").write_text("half an edit\n")
        git(self.wt, "add", "work")
        git(self.wt, "commit", "-m", "WIP: implementer ended mid-edit")
        self.wip = git(self.wt, "rev-parse", "HEAD")
        self.sweep()

        self.cli("--approve", "KO-1", "--note", "ok")

        self.assert_released_at_reviewed()
        self.assertEqual((self.wt / "work").read_text(), "reviewed\n")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_a_ref_update_git_rejects_restores_the_worktree(self):
        self.branch_with_wip()
        self.sweep()
        reflog = self.repo / ".git" / "logs" / "refs" / "heads" / BRANCH
        reflog.chmod(0o444)
        self.addCleanup(reflog.chmod, 0o644)

        self.assertIn("cannot move the refs", self.refused("--note", "ok"))

        reflog.chmod(0o644)
        self.cli("--approve", "KO-1", "--note", "ok")
        self.assert_released_at_reviewed()

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_a_dropped_ref_git_cannot_log_leaves_the_branch_at_its_tip(self):
        self.branch_with_wip()
        self.sweep()
        reflog = (self.repo / ".git" / "logs" / "refs" / "holophyte"
                  / "dropped" / str(self.run))
        reflog.parent.mkdir(parents=True)
        reflog.write_text("")
        reflog.chmod(0o444)
        self.addCleanup(reflog.chmod, 0o644)

        self.assertIn("cannot lock the refs", self.refused("--note", "ok"))

    def wip_in(self, mode):
        """W adds stuck/new, in a directory then given `mode`."""
        self.branch_with_wip()
        (self.wt / "stuck").mkdir()
        (self.wt / "stuck" / "new").write_text("wip only\n")
        git(self.wt, "add", "stuck")
        git(self.wt, "commit", "-m", "WIP: implementer ended mid-edit")
        self.sweep()
        (self.wt / "stuck").chmod(mode)
        self.addCleanup((self.wt / "stuck").chmod, 0o755)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_a_wip_file_the_rewind_cannot_remove_is_refused(self):
        self.wip_in(0o555)

        self.assertIn("could not remove stuck/new",
                      self.refused("--note", "ok"))

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_a_wip_file_in_an_unlistable_directory_is_refused(self):
        self.wip_in(0o111)

        self.assertIn("could not remove stuck/new",
                      self.refused("--note", "ok"))

    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_a_checkout_failing_part_way_restores_the_worktree(self):
        self.branch_with_wip()
        git(self.wt, "reset", "-q", "--hard", self.earlier)
        (self.wt / "zz").mkdir()
        (self.wt / "zz" / "f").write_text("reviewed\n")
        git(self.wt, "add", "zz")
        self.reviewed = self.commit("reviewed\n", "the reviewed candidate")
        (self.wt / "zz" / "f").write_text("half an edit\n")
        git(self.wt, "add", "zz")
        self.wip = self.commit("half an edit\n",
                               "WIP: implementer ended mid-edit")
        self.sweep()
        (self.wt / "zz").chmod(0o555)
        self.addCleanup((self.wt / "zz").chmod, 0o755)

        self.assertIn("cannot check out", self.refused("--note", "ok"))

        (self.wt / "zz").chmod(0o755)
        self.cli("--approve", "KO-1", "--note", "ok")
        self.assert_released_at_reviewed()

    def test_a_run_failed_on_verify_is_refused_as_before(self):
        self.branch_with_wip()
        self.sweep(reason="verify failed: exit 1", kind="verify")

        self.assertIn("KO-1 is in_flight, not blocked_on_operator",
                      self.refused("--note", "ok"))

    def test_a_stale_lock_on_the_dropped_ref_refuses_and_a_retry_approves(self):
        self.branch_with_wip()
        self.sweep()
        lock = self.repo / ".git" / "refs" / "holophyte" / "dropped" / (
            f"{self.run}.lock")
        lock.parent.mkdir(parents=True)
        lock.write_text("")

        self.assertIn("cannot lock the refs", self.refused("--note", "ok"))

        lock.unlink()
        self.cli("--approve", "KO-1", "--note", "ok")
        self.assert_released_at_reviewed()

    def test_a_worker_commit_while_the_rewind_holds_the_refs_is_not_lost(self):
        """The swept worker commits its last edit while approve is moving
        the worktree: the commit cannot land, approve refuses, and the edit
        is still in the worktree."""
        self.branch_with_wip()
        self.sweep()
        rewound = holophyte.cli.operator._moved_tree

        def worker_commits_first(worktree, sha, tip):
            (worktree / "work").write_text("the worker's last edit\n")
            late = subprocess.run(
                ["git", "-c", "user.name=w", "-c", "user.email=w@w",
                 "commit", "-qam", "WIP: implementer ended mid-edit"],
                cwd=worktree, capture_output=True, text=True)
            self.assertNotEqual(late.returncode, 0)
            return rewound(worktree, sha, tip)

        with patch.object(holophyte.cli.operator, "_moved_tree",
                          worker_commits_first), \
                self.assertRaises(SystemExit) as raised:
            self.cli("--approve", "KO-1", "--note", "ok")

        self.assertIn("nothing approved", str(raised.exception))
        self.assertEqual(self.ref(f"refs/heads/{BRANCH}"), self.wip)
        self.assertEqual((self.wt / "work").read_text(),
                         "the worker's last edit\n")
        self.assertEqual(self.interventions(), [])

    def test_a_ticket_renamed_since_the_run_is_refused(self):
        self.branch_with_wip()
        self.sweep()
        self.conn.execute("UPDATE tickets SET title = 'a renamed ticket'"
                          " WHERE id = ?", (self.ticket,))
        self.conn.commit()

        self.assertIn("the next claim resumes task/ko-1-a-renamed-ticket",
                      self.refused("--note", "ok"))

    def test_a_ticket_in_the_backlog_column_is_refused(self):
        self.branch_with_wip()
        self.sweep()
        self.conn.execute("UPDATE tickets SET boardState = 'Icebox',"
                          " boardColumn = 'backlog' WHERE id = ?",
                          (self.ticket,))
        self.conn.commit()

        self.assertIn("not blocked_on_operator", self.refused("--note", "ok"))


MET_BUT_CHANGES = Reply(
    "Blocker: the scripted change is incomplete.\n"
    "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
    "VERDICT: REQUEST_CHANGES")


class SweptApproveLoopTests(LoopFixture):
    """The real loop reviews a candidate, the supervisor's sweep fails the
    run while its fix turn commits WIP, and after approve the next claim
    takes the reviewed candidate through the merge gate."""

    def sweep_during_the_fix_turn(self):
        db = self.db

        class SweptFix(Commit):
            def play(self, cwd, turn):
                conn = store.open(str(db))
                try:
                    (run_id,) = conn.execute("SELECT id FROM runs").fetchone()
                    store.release(
                        conn, run_id, "failed",
                        STUCK.replace("phase reviewing", "phase addressing"),
                        failure_kind="swept")
                finally:
                    conn.close()
                return super().play(cwd, turn)

        self.loop(Commit("the reviewed work"), MET_BUT_CHANGES,
                  SweptFix("WIP: implementer ended mid-edit"))

    def test_the_next_claim_merges_the_reviewed_commit_without_the_wip(self):
        self.sweep_during_the_fix_turn()
        reviewed = self.git("rev-parse", f"{LOOP_BRANCH}~1").strip()
        wip = self.git("rev-parse", LOOP_BRANCH).strip()
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "swept")])
        out = io.StringIO()
        holophyte.cli.operator.approve(self.project, "KO-131", "non-blocking",
                                       out=out)
        self.assertIn(f"approved at {reviewed}", out.getvalue())

        fake, _ = self.loop(provider=StubProvider(a_task()))

        self.assertEqual(fake.roles, [])
        self.assertIn("the reviewed work", self.subjects())
        self.assertNotIn("WIP: implementer ended mid-edit", self.subjects())
        self.assertEqual(
            self.git("rev-parse", "refs/holophyte/dropped/1").strip(), wip)
        self.assertEqual(
            self.read("SELECT id, outcome FROM runs ORDER BY id"),
            [(1, "failed"), (2, "merged")])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])


if __name__ == "__main__":
    unittest.main()

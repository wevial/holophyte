"""Wiring contract: timing telemetry on the run row, and the burndown report.

A finished run has to say how long it took, what it was budgeted, and how many
rounds it needed, in columns rather than in prose: the ledger line was the only
reading of that data until now, and a rendering of the newest 25 entries is not
something a calibration question can be asked of. These tests assert the two
halves — close-out stamps the row, and `--report` renders those rows without
claiming a ticket, cutting a worktree or reaching Linear.

Run: python3 -m unittest discover -s tests -p 'test_wiring*' -v
"""
from __future__ import annotations

import importlib.util
import io
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)

import store  # noqa: E402 - after the sys.path insert above


class StubProvider:
    """The provider seam `main()` drives, recording what it was asked for."""

    TEAM = "team-under-test"

    def __init__(self, *tasks):
        self.queue = list(tasks)
        self.claims = 0
        self.states = []
        self.comments = []

    def claim_next(self, skip=()):
        self.claims += 1
        for i, task in enumerate(self.queue):
            if task["id"] not in skip:
                return self.queue.pop(i)
        return None

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, task_id, body):
        self.comments.append((task_id, body))


class Tripwire:
    """Stands in for a module nothing may touch: every attribute raises."""

    def __init__(self, what):
        object.__setattr__(self, "what", what)

    def __getattr__(self, name):
        raise AssertionError(f"{self.what}.{name} was reached")


def no_network():
    """Fail any attempt to open a socket, at the one call urllib makes."""
    def refuse(*args, **kwargs):
        raise AssertionError("a network connection was attempted")

    return patch.multiple(socket, socket=refuse, create_connection=refuse)


class CloseOutTelemetryTests(unittest.TestCase):
    """One full task over a real repo, agent turns faked, asserted over the
    row the close-out left behind and the line the window rendered from it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.target.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.target / "README.md").write_text("base\n")
        self.git("add", "README.md")
        self.git("commit", "-q", "-m", "base")

        self.db = root / "repo.holophyte.db"
        for name, value in (("TARGET", self.target), ("STORE_PATH", self.db),
                            ("WORKTREES", root / "repo.worktrees")):
            patcher = patch.object(factory, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def read(self, sql):
        """Query the store over a connection the factory never touched."""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.execute(sql).fetchall()

    def loop(self, *replies):
        """Drive `main()` over one 5-minute ticket, faking only the agents."""
        replies = list(replies)
        turns = []

        def fake_agent(role, goal, cwd, *, base_sha=None, candidate_sha=None,
                       timeout=None):
            turns.append(role)
            if role != "implement":
                return replies.pop(0)
            n = sum(1 for turn in turns if turn == "implement")
            (Path(cwd) / f"change{n}.txt").write_text(f"work {n}\n")
            self.git("add", "-A", cwd=cwd)
            self.git("commit", "-q", "-m", f"work {n}", cwd=cwd)
            return f"committed work {n}"

        provider = StubProvider(
            {"id": "KO-133", "issue_id": "iss-133", "title": "time the runs",
             "verify": "echo ok", "budget_min": 5, "contracts": [],
             "criteria": ["Given a merged ticket, when close-out completes, "
                          "then the run row carries its timing"]})
        with patch.dict(sys.modules, {"linear_provider": provider}):
            with patch.object(factory, "agent", fake_agent):
                factory.main(provider)
        return provider

    def test_close_out_stamps_the_run_row_and_the_window_reads_it_back(self):
        """The row a merged run leaves, and the ledger line rendered from it.

        The line is parsed back into numbers and held against the columns:
        FINDINGS.md and the run row are two readings of one close-out, and a
        file that says a run took two rounds while the row says four is the
        drift this ticket exists to remove.
        """
        self.loop("- factory.py:1: name the estimate\nVERDICT: REQUEST_CHANGES",
                  "VERDICT: APPROVE")

        (started, ended, estimate, rounds, outcome), = self.read(
            "SELECT startedAt, endedAt, timeBoxMs, reviewRoundCount, outcome"
            " FROM runs")
        self.assertEqual(outcome, "merged")
        self.assertIsNotNone(ended)
        self.assertGreaterEqual(ended, started)
        self.assertEqual(estimate, 5 * 60 * 1000)  # the ticket's 5 min budget
        # The count is the rounds that were actually filed, not a tally the
        # loop carried: the reviewer's changes_requested and the approval.
        (filed,), = self.read("SELECT COUNT(*) FROM reviewRounds")
        self.assertEqual((rounds, filed), (2, 2))

        line, = [line for line in (self.target / "FINDINGS.md").read_text()
                 .splitlines() if line.startswith("actual: ")]
        actual, shown_estimate, shown_rounds = (
            part.split(": ")[1] for part in line.split(" · "))
        self.assertEqual(actual, f"{(ended - started) / 60000:.1f} min")
        self.assertEqual(shown_estimate, f"{estimate // 60000} min")
        self.assertEqual(shown_rounds, str(rounds))

    def test_the_run_keeps_the_estimate_it_was_claimed_under(self):
        """A re-mirrored ticket does not restate what a finished run was given.

        The whole reason the estimate is a run column: Linear's points can be
        edited after the fact, and an estimate-vs-actual reading that moved
        with them would make finished runs answer for a budget they never had.
        """
        conn = store.open(str(self.db))
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.ensure_project(conn, "team-1", self.target)
        mirror = dict(linear_issue_id="iss-1", linear_identifier="KO-1",
                      title="a ticket")
        ticket = store.mirror_ticket(conn, project, time_box_ms=25 * 60 * 1000,
                                     **mirror)
        run_id = store.claim(conn, project, ticket)
        store.release(conn, run_id, "merged")

        store.mirror_ticket(conn, project, time_box_ms=90 * 60 * 1000, **mirror)

        self.assertEqual(
            conn.execute("SELECT timeBoxMs FROM tickets").fetchone()[0],
            90 * 60 * 1000)
        self.assertEqual(
            conn.execute("SELECT timeBoxMs FROM runs WHERE id = ?",
                         (run_id,)).fetchone()[0],
            25 * 60 * 1000)


class ReportTests(unittest.TestCase):
    """`--report` over a seeded store: what it prints, and what it never does."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = self.root / "repo"
        self.target.mkdir()
        # Where `cli()`'s retarget will look: the target's directory under a
        # HOLOPHYTE_HOME of this test's own, never the operator's real one.
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.db = factory.state_dir(self.target) / "store.db"
        self.db.parent.mkdir(parents=True)
        self.worktrees = self.root / "repo.worktrees"
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project = store.ensure_project(self.conn, "team-1", self.target)
        # `cli()` retargets the module for real, so what it overwrites is put
        # back rather than left pointing at this test's temporary directory.
        original = {name: getattr(factory, name)
                    for name in ("TARGET", "STORE_PATH", "WORKTREES")}

        def restore():
            for name, value in original.items():
                setattr(factory, name, value)

        self.addCleanup(restore)

    def completed_run(self, n, actual_min, estimate_min, rounds, outcome):
        """One ended run of its own ticket, with the timing the test chose."""
        ticket = store.mirror_ticket(
            self.conn, self.project, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}",
            time_box_ms=estimate_min and estimate_min * 60 * 1000)
        at = 1_700_000_000_000 + n * 3_600_000
        run_id = store.claim(self.conn, self.project, ticket, now=at)
        for number in range(1, rounds + 1):
            store.record_review_round(self.conn, run_id, number, "pass",
                                      "codex-sol-medium", started_at=at)
        store.release(self.conn, run_id, outcome, now=at + actual_min * 60_000)

    def three_runs(self):
        """Three finished runs whose numbers are known by hand."""
        self.completed_run(1, actual_min=5, estimate_min=25, rounds=2,
                           outcome="merged")
        self.completed_run(2, actual_min=40, estimate_min=20, rounds=1,
                           outcome="failed")
        self.completed_run(3, actual_min=3, estimate_min=25, rounds=0,
                           outcome="merged")

    def test_a_line_per_run_with_its_ratio_and_a_summary(self):
        self.three_runs()

        lines = factory.report_lines(self.conn)

        self.assertEqual([line.split() for line in lines[:4]], [
            ["ticket", "actual", "estimate", "ratio", "rounds", "outcome"],
            ["KO-1", "5.0", "25", "0.20", "2", "merged"],
            ["KO-2", "40.0", "20", "2.00", "1", "failed"],
            ["KO-3", "3.0", "25", "0.12", "0", "merged"],
        ])
        # 0.20, 2.00 and 0.12: a mean the one blown budget carries, and a
        # median that says what a typical ticket actually costs.
        self.assertEqual(lines[4],
                         "3 runs · mean ratio 0.77 · median ratio 0.20")
        self.assertEqual(len(lines), 5)

    def test_a_run_with_no_estimate_is_shown_but_not_averaged(self):
        """An older run, or a ticket Linear gave no points: not a ratio of 0."""
        self.three_runs()
        self.completed_run(4, actual_min=7, estimate_min=None, rounds=0,
                           outcome="merged")

        lines = factory.report_lines(self.conn)

        self.assertEqual(lines[4].split(), ["KO-4", "7.0", "n/a", "n/a", "0",
                                            "merged"])
        self.assertEqual(lines[5], "4 runs · 3 with an estimate · "
                                   "mean ratio 0.77 · median ratio 0.20")

    def test_report_prints_the_table_and_claims_nothing(self):
        """The mode as an operator runs it: `factory.py --report <target>`.

        The provider and the socket module are tripwires, so a claim, a state
        push or any connection at all fails the test rather than passing
        quietly; the store and the filesystem say the rest.
        """
        self.three_runs()
        self.conn.commit()
        out = io.StringIO()

        with patch.dict(sys.modules,
                        {"linear_provider": Tripwire("linear_provider")}):
            with no_network(), patch.object(sys, "stdout", out):
                factory.cli(["--report", str(self.target)])

        printed = out.getvalue().splitlines()
        self.assertEqual(printed[0].split()[0], "ticket")
        # The table is the five lines it always was, and below it the one
        # line on the supervisor: none has ever beaten in this store.
        self.assertEqual(printed[:5], factory.report_lines(self.conn))
        self.assertEqual(printed[5], "supervisor: none recorded")
        self.assertEqual(len(printed), 6)
        self.assertEqual(factory.STORE_PATH, self.db)
        # Nothing was claimed: three runs went in, three are there, all ended,
        # and the lease the loop would have taken is free.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*), COUNT(endedAt) FROM runs").fetchone(), (3, 3))
        self.assertIsNone(
            self.conn.execute(
                "SELECT activeRunId FROM projects").fetchone()[0])
        # And no branch was cut for one: the worktrees directory is the first
        # thing a started run creates.
        self.assertFalse(self.worktrees.exists())

    def report_with_a_heartbeat(self, age_ms):
        """`--report` as the operator runs it, over a store whose one
        supervisor last beat `age_ms` before now."""
        now = int(time.time() * 1000)
        store.record_supervisor_heartbeat(self.conn, pid=4242, started_at=now,
                                          now=now - age_ms)
        self.conn.commit()
        out = io.StringIO()
        with no_network(), patch.object(sys, "stdout", out):
            factory.cli(["--report", str(self.target)])
        return out.getvalue().splitlines()

    def test_a_fresh_heartbeat_reports_a_live_supervisor_with_its_age(self):
        printed = self.report_with_a_heartbeat(age_ms=12_000)

        self.assertRegex(printed[-1],
                         r"^supervisor: live, last heartbeat 1[2-9]s ago"
                         rf" \(pid 4242 on {socket.gethostname()}\)$")

    def test_a_heartbeat_past_the_stale_threshold_reports_stale(self):
        """Nine minutes against the default five: the watcher stopped."""
        printed = self.report_with_a_heartbeat(age_ms=9 * 60_000)

        self.assertRegex(printed[-1],
                         r"^supervisor: stale, last heartbeat 9m ago"
                         rf" \(pid 4242 on {socket.gethostname()}\)$")

    def test_a_target_with_no_store_is_reported_not_created(self):
        out = io.StringIO()

        with no_network(), patch.object(sys, "stdout", out):
            factory.cli(["--report", str(self.root / "elsewhere")])

        self.assertIn("no store at", out.getvalue())
        self.assertFalse(factory.state_dir(self.root / "elsewhere").exists())


if __name__ == "__main__":
    unittest.main()

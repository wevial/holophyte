"""`store.read`: the typed read views agree with the SQL they replaced.

Every read the factory used to run as an embedded `SELECT` is kept here
verbatim as the oracle, run against a store populated only through the
public write API, and compared field by field with what the named read
function returns. The oracle is the *previous* code, not a restatement of the
new one: a read that dropped a column, reordered a tuple, or filtered
differently disagrees with it.

Run: python3 -m unittest discover -s tests -p 'test_store_read*' -v
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import store
import store.read as read

T0 = 1_700_000_000_000
MIN = 60_000

# The sweep's phase filter as `factory.py` derives it; restated here so this
# module does not import the factory to test the store.
SWEEPABLE = tuple(
    phase for phase in store.PHASES
    if phase not in store.ENDED_PHASES and phase != "blocked_on_operator")


class PopulatedStore(unittest.TestCase):
    """Two projects' worth of runs, written only through the write API.

    Project one: a merged run with two review rounds (one still open), a
    failed run a human closed out by hand, a failed run nobody touched, an
    infra failure, and a run still in flight. Project two: one more run in
    flight, so `live_runs` has two rows to order.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / "holophyte.db"
        self.conn = store.open(str(self.path))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        c = self.conn
        p1 = store.ensure_project(c, "team-1", self.root / "repo-1")
        p2 = store.ensure_project(c, "team-2", self.root / "repo-2")

        def ticket(project, n, time_box=25 * MIN):
            return store.mirror_ticket(
                c, project, linear_issue_id=f"issue-{n}",
                linear_identifier=f"KO-{n}", title=f"ticket {n}",
                time_box_ms=time_box)

        # KO-1: merged after two rounds; the second round has not ended.
        self.t1 = ticket(p1, 1)
        self.merged = store.claim(c, p1, self.t1, now=T0)
        store.record_review_round(
            c, self.merged, 1, "changes_requested", "codex-sol-medium",
            findings=[{"path": "store/read.py", "line": 7, "severity": "p1",
                       "message": "the docstring names the wrong column"}],
            verification_results=[{"command": "ruff check .", "exitCode": 0,
                                   "output": ""}],
            started_at=T0 + MIN, ended_at=T0 + 2 * MIN)
        store.record_review_round(
            c, self.merged, 2, "pass", "codex-sol-medium",
            started_at=T0 + 3 * MIN)
        store.release(c, self.merged, "merged", now=T0 + 10 * MIN)

        # KO-2: three attempts. The first failed and was closed out by a
        # human; the second failed and was not; the third is infra.
        self.t2 = ticket(p1, 2, time_box=None)
        closed = store.claim(c, p1, self.t2, now=T0 + 20 * MIN)
        store.record_intervention(
            c, closed, "close_out", "operator closed it out by hand",
            now=T0 + 25 * MIN)
        store.release(c, closed, "failed", reason="worker stalled",
                      now=T0 + 26 * MIN)
        failed = store.claim(c, p1, self.t2, now=T0 + 30 * MIN)
        store.release(c, failed, "failed", reason="verify failed",
                      now=T0 + 40 * MIN)
        infra = store.claim(c, p1, self.t2, now=T0 + 50 * MIN)
        store.release(c, infra, "failed", reason="reviewer container",
                      now=T0 + 51 * MIN, outcome_class="infra")
        self.failed, self.closed = failed, closed

        # KO-3 and KO-4: two runs still in flight, one per project, with a
        # strike on file for the first.
        self.t3 = ticket(p1, 3)
        self.live = store.claim(c, p1, self.t3, now=T0 + 60 * MIN)
        store.set_phase(c, self.live, "working", now=T0 + 61 * MIN)
        store.record_strike(c, self.live, True, T0 + 61 * MIN,
                            now=T0 + 70 * MIN)
        self.t4 = ticket(p2, 4)
        self.live2 = store.claim(c, p2, self.t4, now=T0 + 62 * MIN)


class OracleTests(PopulatedStore):
    """Each read against the SELECT `factory.py` embedded before this module."""

    def test_ended_runs_agree_with_the_report_and_findings_selects(self):
        rows = read.ended_runs(self.conn)
        report = self.conn.execute(
            "SELECT t.linearIdentifier, r.startedAt, r.endedAt, r.timeBoxMs,"
            " r.reviewRoundCount, r.outcome, r.host"
            " FROM runs r JOIN tickets t ON t.id = r.ticketId"
            " WHERE r.endedAt IS NOT NULL"
            " ORDER BY r.endedAt, r.id").fetchall()
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [(r.linearIdentifier, r.startedAt, r.endedAt, r.timeBoxMs,
              r.reviewRoundCount, r.outcome, r.host) for r in rows],
            report)
        findings = self.conn.execute(
            "SELECT r.endedAt, t.linearIdentifier, r.outcome, r.outcomeReason,"
            " r.branch, r.startedAt, r.timeBoxMs, r.reviewRoundCount, r.id"
            " FROM runs r JOIN tickets t ON t.id = r.ticketId"
            " WHERE r.endedAt IS NOT NULL").fetchall()
        self.assertEqual(
            sorted((r.endedAt, r.linearIdentifier, r.outcome, r.outcomeReason,
                    r.branch, r.startedAt, r.timeBoxMs, r.reviewRoundCount,
                    r.id) for r in rows),
            sorted(findings))

    def test_review_rounds_agree_with_the_findings_select(self):
        rows = read.review_rounds(self.conn)
        oracle = self.conn.execute(
            "SELECT COALESCE(rr.endedAt, rr.startedAt), t.linearIdentifier,"
            " rr.round, rr.verdict, rr.reviewerModel, rr.verificationResults,"
            " rr.findings, rr.id"
            " FROM reviewRounds rr JOIN runs r ON r.id = rr.runId"
            " JOIN tickets t ON t.id = r.ticketId").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted((r.endedAt if r.endedAt is not None else r.startedAt,
                    r.linearIdentifier, r.round, r.verdict, r.reviewerModel,
                    r.verificationResults, r.findings, r.id) for r in rows),
            sorted(oracle))

    def test_live_runs_agree_with_the_sweep_select(self):
        rows = read.live_runs(self.conn, SWEEPABLE)
        oracle = self.conn.execute(
            "SELECT r.id, t.linearIdentifier, r.phase, r.lastHeartbeat,"
            " r.startedAt, r.timeBoxMs, r.host"
            " FROM runs r JOIN tickets t ON t.id = r.ticketId"
            " WHERE r.endedAt IS NULL"
            f"   AND r.phase IN ({', '.join('?' * len(SWEEPABLE))})"
            " ORDER BY r.id", SWEEPABLE).fetchall()
        self.assertEqual([r.id for r in rows], [self.live, self.live2])
        self.assertEqual(
            [(r.id, r.linearIdentifier, r.phase, r.lastHeartbeat, r.startedAt,
              r.timeBoxMs, r.host) for r in rows],
            oracle)
        # The phase filter is the caller's: a run in a phase not asked for
        # is not live to that caller.
        self.assertEqual(read.live_runs(self.conn, ("claimed",))[0].id,
                         self.live2)

    def test_ticket_by_id_carries_every_column_the_four_callers_read(self):
        for ticket_id in (self.t1, self.t2, self.t3):
            with self.subTest(ticket=ticket_id):
                row = read.ticket_by_id(self.conn, ticket_id)
                status = self.conn.execute(
                    "SELECT status FROM tickets WHERE id = ?",
                    (ticket_id,)).fetchone()[0]
                (run_id,) = self.conn.execute(
                    "SELECT COALESCE(activeRunId, lastRunId) FROM tickets"
                    " WHERE id = ?", (ticket_id,)).fetchone()
                mirror = self.conn.execute(
                    "SELECT linearIssueId, linearIdentifier, status"
                    " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
                self.assertEqual(row.status, status)
                self.assertEqual(
                    row.activeRunId if row.activeRunId is not None
                    else row.lastRunId, run_id)
                self.assertEqual(
                    (row.linearIssueId, row.linearIdentifier, row.status),
                    mirror)
        self.assertIsNone(read.ticket_by_id(self.conn, 999))

    def test_run_snapshot_agrees_with_the_three_sweep_reads(self):
        for run_id in (self.merged, self.live):
            with self.subTest(run=run_id):
                row = read.run_snapshot(self.conn, run_id)
                self.assertEqual(
                    (row.endedAt, row.phase, row.lastHeartbeat),
                    self.conn.execute(
                        "SELECT endedAt, phase, lastHeartbeat FROM runs"
                        " WHERE id = ?", (run_id,)).fetchone())
                self.assertEqual(
                    (row.ticketId,),
                    self.conn.execute("SELECT ticketId FROM runs WHERE id = ?",
                                      (run_id,)).fetchone())
        self.assertIsNone(read.run_snapshot(self.conn, 999))

    def test_failure_history_reads_agree_with_the_escalation_selects(self):
        since = read.latest_human_intervention_at(self.conn, self.t2)
        self.assertEqual(since, self.conn.execute(
            "SELECT COALESCE(MAX(i.at), 0) FROM interventions i"
            " JOIN runs r ON r.id = i.runId"
            " WHERE r.ticketId = ? AND i.source = 'human'",
            (self.t2,)).fetchone()[0])
        self.assertEqual(since, T0 + 25 * MIN)
        # A ticket nobody intervened on: the sentinel, not NULL.
        self.assertEqual(read.latest_human_intervention_at(self.conn, self.t1),
                         0)

        attempts = read.failed_attempts_since(self.conn, self.t2, since)
        oracle = self.conn.execute(
            "SELECT attempt, outcomeReason FROM runs r"
            " WHERE ticketId = ? AND outcome = 'failed' AND endedAt > ?"
            " AND outcomeClass = 'work'"
            " AND NOT EXISTS (SELECT 1 FROM interventions i"
            "                 WHERE i.runId = r.id AND i.source = 'human'"
            "                 AND i.\"action\" = 'close_out')"
            " ORDER BY attempt", (self.t2, since)).fetchall()
        self.assertEqual([(a.attempt, a.outcomeReason) for a in attempts],
                         oracle)
        # Only the untouched work failure counts: not the closed-out one,
        # not the infra one.
        self.assertEqual(oracle, [(2, "verify failed")])

    def test_newest_ended_rounds_agree_with_the_overlap_select(self):
        rows = read.newest_ended_rounds(self.conn, self.merged)
        oracle = self.conn.execute(
            "SELECT round, findings FROM reviewRounds"
            " WHERE runId = ? AND endedAt IS NOT NULL"
            " ORDER BY round DESC LIMIT 2", (self.merged,)).fetchall()
        self.assertEqual([(r.round, r.findings) for r in rows], oracle)
        # Round 2 has no endedAt, so only round 1 qualifies.
        self.assertEqual([r.round for r in rows], [1])
        self.assertEqual(read.newest_ended_rounds(self.conn, self.live), [])

    def test_strike_agrees_with_the_sweep_select(self):
        row = read.strike(self.conn, self.live)
        self.assertEqual(
            (row.strikes, row.lastSeen),
            self.conn.execute(
                "SELECT strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
                (self.live,)).fetchone())
        self.assertEqual(row.runId, self.live)
        self.assertIsNone(read.strike(self.conn, self.live2))


class ReadonlyTests(PopulatedStore):
    def test_a_write_through_open_readonly_is_refused_and_changes_nothing(self):
        before = self.conn.execute(
            "SELECT id, status FROM tickets ORDER BY id").fetchall()
        ro = read.open_readonly(self.path)
        self.addCleanup(ro.close)
        # Reads work over the live WAL store while the writer holds it open,
        # and see what the writer sees.
        self.assertEqual(read.ticket_by_id(ro, self.t1),
                         read.ticket_by_id(self.conn, self.t1))
        self.assertEqual(read.ended_runs(ro), read.ended_runs(self.conn))

        with self.assertRaises(sqlite3.OperationalError) as refused:
            ro.execute("UPDATE tickets SET status = 'abandoned'")
            ro.commit()
        self.assertIn("readonly", str(refused.exception))
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO sweepStrikes (runId, strikes, lastSeen)"
                       " VALUES (?, 1, 1)", (self.live2,))
        self.assertEqual(
            self.conn.execute(
                "SELECT id, status FROM tickets ORDER BY id").fetchall(),
            before)
        self.assertIsNone(read.strike(self.conn, self.live2))

    def test_open_readonly_never_creates_a_store(self):
        missing = self.root / "nowhere" / "holophyte.db"
        with self.assertRaises(sqlite3.OperationalError):
            read.open_readonly(missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()

"""Budget, PR wait and wire contracts for measured working time."""

from unittest.mock import patch

import store
from holophyte import babysitter, loop, pr, report, serve, serve_runs, supervisor
from store.working import settle_work, working
from tests.phase_fixture import finish_run
from tests.sweep_fixture import MINUTE, T0, SweepTestCase


class WorkingConsumers(SweepTestCase):
    def test_time_box_recheck_counts_earned_review_rounds(self):
        run = self.a_run(budget_min=30, active_work=True)
        for number in (1, 2):
            store.record_review_round(
                self.conn, run, number, "changes_requested", "reviewer",
                started_at=T0, ended_at=T0 + MINUTE,
            )
        now = T0 + 100 * MINUTE
        self.heartbeat_at(run, now)
        trip, = supervisor.sweep(self.tgt, self.conn, now).trips
        self.assertEqual(trip.condition, supervisor.TIME_BOX)
        with patch("store.working.time", return_value=now / 1000):
            self.assertTrue(supervisor.still_tripped(self.tgt, self.conn, trip))
        # Settlement leaves 50 minutes: over one turn's 45-minute allowance,
        # but below the 90 minutes earned by the recorded review rounds.
        settle_work(self.conn, run, now=T0 + 50 * MINUTE)
        outcome = supervisor.act_on_trip(self.tgt, self.conn, trip)
        self.assertFalse(outcome.acted)
        snapshot = store.read.run_snapshot(self.conn, run)
        self.assertIsNone(snapshot.endedAt)
        self.assertEqual(snapshot.phase, "working")
        self.assertEqual(self.conn.execute(
            "SELECT activeRunId FROM tickets WHERE id = ?",
            (self.ticket_of[run],),
        ).fetchone(), (run,))

    def test_budget_consumers_use_working_clock(self):
        run = self.a_run(budget_min=10)
        with patch("store.working.time", return_value=T0 / 1000):
            with working(self.conn, run):
                now = T0 + 2 * MINUTE
                settle_work(self.conn, run, now=now)
                self.heartbeat_at(run, now)
                with patch.object(loop, "time", return_value=now / 1000):
                    loop._check_run_cap(self.tgt, self.conn, run, 10, "abc")
        for wait in (0, 200 * MINUTE):
            now = T0 + 2 * MINUTE + wait
            self.heartbeat_at(run, now)
            with patch.object(loop, "time", return_value=now / 1000):
                loop._check_run_cap(self.tgt, self.conn, run, 10, "abc")
            self.assertFalse(supervisor.sweep(self.tgt, self.conn, now).trips)
        # A hung call is visible while its context is still open.
        with patch("store.working.time", return_value=T0 / 1000):
            with working(self.conn, run):
                now = T0 + 200 * MINUTE
                self.heartbeat_at(run, now)
                trips = supervisor.sweep(self.tgt, self.conn, now).trips
                self.assertEqual(trips[0].condition, supervisor.TIME_BOX)
                with patch.object(loop, "time", return_value=now / 1000):
                    with self.assertRaisesRegex(loop.RunFailure, "out of time"):
                        loop._check_run_cap(self.tgt, self.conn, run, 10, "abc")
            # Settlement can invalidate previously observed in-flight evidence.
            self.assertFalse(supervisor.still_tripped(self.tgt, self.conn, trips[0]))
        supervisor.sweep(self.tgt, self.conn, now + 100 * MINUTE)
        trips = supervisor.sweep(self.tgt, self.conn, now + 200 * MINUTE).trips
        self.assertEqual(trips[0].condition, supervisor.STALE_HEARTBEAT)
        finish_run(self.conn, run, "merged", now=now)
        other = self.a_run(budget_min=10)
        with patch("store.working.time", return_value=T0 / 1000):
            with working(self.conn, other):
                settle_work(self.conn, other, now=T0 + 2 * MINUTE)
        finish_run(self.conn, other, "merged", now=T0 + 3 * MINUTE)
        self.assertEqual(
            [row[1:4] for row in report.report_rows(self.conn)],
            [(2, 10, 0.2), (2, 10, 0.2)],
        )

    def test_time_box_judges_agent_work_only(self):
        # A 10-minute box allows 15 minutes of one turn and 30 of the run.
        run = self.a_run(budget_min=10)
        with patch("store.working.time", return_value=T0 / 1000):
            with working(self.conn, run, verify=True):
                settle_work(self.conn, run, now=T0 + 30 * MINUTE)
        start = T0 + 30 * MINUTE
        with patch("store.working.time", return_value=start / 1000):
            with working(self.conn, run):
                settle_work(self.conn, run, now=start + 2 * MINUTE)
            now = start + 2 * MINUTE
            self.heartbeat_at(run, now)
            self.assertFalse(supervisor.sweep(self.tgt, self.conn, now).trips)
            with patch.object(loop, "time", return_value=now / 1000):
                loop._check_run_cap(self.tgt, self.conn, run, 10, "abc")
            with working(self.conn, run):
                now = start + 25 * MINUTE
                self.heartbeat_at(run, now)
                trip, = supervisor.sweep(self.tgt, self.conn, now).trips
                self.assertEqual(trip.condition, supervisor.TIME_BOX)
                self.assertIn("27.0 min of agent work", trip.evidence)
                with patch("store.working.time", return_value=now / 1000):
                    self.assertTrue(
                        supervisor.still_tripped(self.tgt, self.conn, trip))
                with patch.object(loop, "time", return_value=now / 1000):
                    with self.assertRaisesRegex(loop.RunFailure, "out of time"):
                        loop._check_run_cap(self.tgt, self.conn, run, 10, "abc")

    def test_pending_and_quiet_waits_are_bounded(self):
        self.configure(
            "[merge]\npr_quiet_sec = 3600\npr_poll_sec = 13\npr_rounds = 1\n"
        )
        pull = pr.PullRequest(
            "github.com", "owner", "repo", 1, "https://github.com/owner/repo/pull/1"
        )
        for checks, final_fix in (
            ("pending", False),
            ("success", False),
            ("alternating", False),
            ("pending", True),
            ("success", True),
        ):
            clock = [0]
            first = [final_fix]

            def state(*args):
                if first[0]:
                    first[0] = False
                    return pr.PrState((pr.Thread("1", "app.py", 1, "bot",
                                                "Fix this", "url"),), "success", "sha")
                check = (
                    ("pending" if clock[0] < 10 else "success")
                    if checks == "alternating"
                    else checks
                )
                return pr.PrState((), check, "sha", updated_at=T0 + clock[0] * 1000)

            def nap(seconds):
                clock[0] += seconds

            with (
                patch.object(babysitter, "monotonic", side_effect=lambda: clock[0]),
                patch.object(
                    babysitter, "time", side_effect=lambda: T0 / 1000 + clock[0]
                ),
                patch.object(pr, "CHECK_WAIT_S", 20),
                patch.object(pr, "SLEEP", side_effect=nap),
                patch.object(babysitter.pr_status, "pr_state", side_effect=state),
                patch.object(babysitter, "_answer_threads", return_value="sha") as fix,
                patch(
                    "holophyte.pullrequest._park_on_pr",
                    side_effect=loop.MergeParked("parked"),
                ) as park,
            ):
                with self.assertRaises(loop.MergeParked):
                    babysitter._babysit(
                        self.tgt,
                        None,
                        None,
                        None,
                        "KO-1",
                        None,
                        None,
                        "branch",
                        None,
                        "sha",
                        10,
                        pull.url,
                        None,
                        None,
                        None,
                        10,
                    )
                reason = park.call_args.args[8]
                self.assertRegex(reason, "pending checks|quiet wait")
                self.assertNotIn("out of time", reason)
                self.assertEqual(fix.call_count, int(final_fix))
            self.assertEqual(clock[0], 20)
        thread = pr.PrState(("thread",), "pending", "sha")
        with (
            patch.object(
                babysitter.pr_status,
                "pr_state",
                side_effect=[pr.PrState((), "pending", "sha"), thread],
            ),
            patch.object(pr, "SLEEP") as sleep,
        ):
            self.assertIs(
                babysitter._settled_state(self.tgt, None, None, 10, pull), thread
            )
            sleep.assert_called_once_with(pr.CHECK_POLL_S)

    def test_clock_api_projection(self):
        run = self.a_run(budget_min=10)
        self.conn.execute(
            "UPDATE runs SET workingMs = ?, workStartedAt = ? WHERE id = ?",
            (2 * MINUTE, T0 + 8 * MINUTE, run),
        )
        self.conn.commit()
        now = T0 + 10 * MINUTE
        live = serve.status(self.tgt, now=now)[1]["runs"][0]
        self.assertEqual(
            (live["working_ms"], live["elapsed_ms"]), (4 * MINUTE, 10 * MINUTE)
        )
        detail = serve_runs.run_detail(self.tgt, str(run), now=now)[1]["run"]
        self.assertEqual(detail["working_ms"], live["working_ms"])
        store.working.settle_work(self.conn, run, now=now)
        waiting = serve.status(self.tgt, now=now + 20 * MINUTE)[1]["runs"][0]
        self.assertEqual(
            (waiting["working_ms"], waiting["elapsed_ms"], waiting["work_started_ms"]),
            (4 * MINUTE, 30 * MINUTE, None),
        )
        finish_run(self.conn, run, "merged", now=now + 20 * MINUTE)
        shipped = serve_runs.shipped(self.tgt)[1]["rows"][0]
        self.assertEqual((shipped["actual_min"], shipped["wall_min"]), (4, 30))
        self.assertEqual(report.report_rows(self.conn)[0][1:4], (4, 10, 0.4))
        self.conn.execute("UPDATE runs SET workingMs = NULL WHERE id = ?", (run,))
        self.conn.commit()
        self.assertIsNone(serve_runs.shipped(self.tgt)[1]["rows"][0]["actual_min"])
        self.assertIn("n/a", "\n".join(report.report_lines(self.conn)))

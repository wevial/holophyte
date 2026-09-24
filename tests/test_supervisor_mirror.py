"""The supervisor's pass walks tickets the board closed while no loop ran
(KO-723): the mirror reconcile, throttled per project to `board_ask_sec`."""
import io
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from loop_fixture import StubProvider  # noqa: E402
from sweep_fixture import MINUTE, T0, SweepTestCase  # noqa: E402

import holophyte.supervisor  # noqa: E402
import store  # noqa: E402
from holophyte.config_tables import sweep_config  # noqa: E402
from holophyte.supervisor import reconcile_parked_pull_requests  # noqa: E402


class CountingProvider(StubProvider):
    """The loop fixture's board, counting its closed-issue asks."""

    def __init__(self):
        super().__init__()
        self.closed_asks = 0

    def closed_identifiers(self, identifiers):
        self.closed_asks += 1
        return super().closed_identifiers(identifiers)


class RefusingProvider(StubProvider):
    def closed_identifiers(self, identifiers):
        raise RuntimeError("Linear is unreachable")


class SupervisorMirrorTests(SweepTestCase):
    def setUp(self):
        super().setUp()
        self.configure("[supervisor]\nboard_ask_sec = 600\n")
        self.forget_asks()
        self.addCleanup(self.forget_asks)
        # The loop is gone: the ticket's last run ended `failed` and the
        # ticket stayed `in_flight`, the REL-138 shape.
        run = self.a_run()
        store.release(self.conn, run, "failed", "crashed")
        self.run_id = run
        self.ticket = self.ticket_of[run]

    @staticmethod
    def forget_asks():
        """The supervisor process's throttle, fresh for each store."""
        vars(holophyte.supervisor).get("_MIRROR_ASKED", {}).clear()

    def reconcile(self, provider, at):
        provider.team = "team-1"  # the project `setUp` ensures
        out = io.StringIO()
        with patch("holophyte.reconcile._reconcile_pull_requests"), \
                patch("holophyte.supervisor.linear_budget_low",
                      return_value=False), \
                patch("holophyte.supervisor.start_loop_for"):
            asked = reconcile_parked_pull_requests(
                self.project, self.conn, at, provider, out,
                knobs=sweep_config(self.project))
        return asked, out.getvalue()

    def status(self):
        return self.conn.execute("SELECT status FROM tickets WHERE id = ?",
                                 (self.ticket,)).fetchone()[0]

    def test_a_board_done_walks_the_ticket_merged_with_a_reconcile_row(self):
        self.assertEqual(self.status(), "in_flight")
        provider = StubProvider()
        provider.closed = {"KO-1": "completed"}

        _, out = self.reconcile(provider, T0 + 20 * MINUTE)

        self.assertEqual(self.status(), "merged")
        self.assertEqual(self.conn.execute(
            "SELECT runId FROM interventions WHERE action = 'reconcile'"
        ).fetchall(), [(self.run_id,)])
        self.assertIn("reconciled KO-1: in_flight -> merged", out)

    def test_the_board_is_asked_once_per_board_ask_sec_and_never_under_a_live_loop(
            self):
        provider = CountingProvider()
        self.reconcile(provider, T0 + 20 * MINUTE)
        self.reconcile(provider, T0 + 21 * MINUTE)
        self.assertEqual(provider.closed_asks, 1)

        self.forget_asks()
        live = CountingProvider()
        self.a_run(claimed_at=T0 + 30 * MINUTE)  # a fresh beat: the loop is live
        self.reconcile(live, T0 + 30 * MINUTE)
        self.assertEqual(live.closed_asks, 0)

    def test_a_provider_that_raises_is_one_line_and_the_pass_goes_on(self):
        with patch("holophyte.reconcile._reconcile_mirror",
                   side_effect=RuntimeError("board exploded")):
            asked, out = self.reconcile(RefusingProvider(), T0 + 20 * MINUTE)
        self.assertEqual(asked, [self.project_id])
        lines = [line for line in out.splitlines() if "board exploded" in line]
        self.assertEqual(len(lines), 1)

        asked, out = self.reconcile(RefusingProvider(), T0 + 40 * MINUTE)
        self.assertEqual(asked, [self.project_id])
        lines = [line for line in out.splitlines()
                 if "Linear is unreachable" in line]
        self.assertEqual(len(lines), 1)
        self.assertEqual(self.status(), "in_flight")

"""KO-705: human interventions per merged run over 24 hours and 7 days, as
`--report` prints them and `/status` carries them."""
from time import time

import holophyte.report as report
import holophyte.serve
import store
import store.read
import store.tickets
from holophyte.project import Project
from tests.phase_fixture import finish_run
from tests.serve_fixture import MIN, ServeTestCase

HOUR = 60 * MIN
DAY = 24 * HOUR


class ToilTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project = store.tickets.ensure_project(
            self.conn, "team-1", self.target)

    def run_of(self, n, started, merged=None):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}")
        run = store.claim(self.conn, self.project, ticket, now=started)
        if merged is not None:
            finish_run(self.conn, run, "merged", now=merged)
        return run

    def intervene(self, run, action, at, source="human"):
        store.record_intervention(self.conn, run, action, f"{action} it",
                                  source=source, now=at)

    def seed_two_windows(self):
        """Inside 24 h: requeue x2, babysit, a supervisor kill, two merges.
        Inside 7 d only: an approve and one more merge."""
        now = self.now
        first = self.run_of(1, now - 3 * HOUR, merged=now - HOUR)
        self.run_of(2, now - 2 * HOUR, merged=now - 30 * MIN)
        old = self.run_of(3, now - 3 * DAY - HOUR, merged=now - 3 * DAY)
        self.intervene(first, "requeue", now - 150 * MIN)
        self.intervene(first, "requeue", now - 140 * MIN)
        self.intervene(first, "babysit", now - 130 * MIN)
        self.intervene(first, "kill", now - 120 * MIN, source="supervisor")
        self.intervene(old, "approve", now - 3 * DAY)
        self.conn.commit()

    def test_reader_counts_each_window(self):
        self.seed_two_windows()
        day = store.read.toil_since(self.conn, self.now - DAY)
        week = store.read.toil_since(self.conn, self.now - 7 * DAY)
        self.assertEqual((day.by_action, day.merged),
                         ({"requeue": 2, "babysit": 1}, 2))
        self.assertEqual(list(day.by_action), ["requeue", "babysit"])
        self.assertEqual((sum(week.by_action.values()), week.merged), (4, 3))
        # A project-level hold names no run and is human work all the same.
        store.record_project_intervention(self.conn, "hold", "hold it",
                                          now=self.now - MIN)
        self.conn.commit()
        day = store.read.toil_since(self.conn, self.now - DAY)
        self.assertEqual(day.by_action.get("hold"), 1)

    def test_report_prints_both_windows(self):
        self.seed_two_windows()
        lines = report.report_lines(self.conn)
        self.assertIn("toil 24h: 3 human interventions, 2 merged, 1.50 per"
                      " merge (requeue 2, babysit 1)", lines)
        self.assertIn("toil 7d: 4 human interventions, 3 merged, 1.33 per"
                      " merge (requeue 2, approve 1, babysit 1)", lines)

    def test_no_merge_leaves_the_rate_out(self):
        run = self.run_of(1, self.now - 2 * HOUR)
        for minutes in (90, 80, 70):
            self.intervene(run, "requeue", self.now - minutes * MIN)
        self.conn.commit()
        self.assertIn("toil 24h: 3 human interventions, 0 merged (requeue 3)",
                      report.report_lines(self.conn))
        code, body = holophyte.serve.status(Project.locate(self.target),
                                            now=self.now)
        self.assertEqual(code, 200)
        self.assertEqual(body["toil"]["24h"], {
            "interventions": 3, "merged": 0, "per_merge": None,
            "by_action": {"requeue": 3}})

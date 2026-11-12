"""KO-705: human interventions per merged run over 24 hours and 7 days, as
`--report` prints them and `/status` carries them."""
import os
import subprocess
from time import time

import holophyte.cli.report as report
import holophyte.serve.server
import store
import store.read
import store.tickets
from holophyte.config.project import Project
from tests.phase_fixture import finish_run
from tests.serve_fixture import MIN, ServeTestCase

HOUR = 60 * MIN
DAY = 24 * HOUR


class StoreTestCase(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.now = int(time() * 1000)
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", self.target)

    def run_of(self, n, started, merged=None, merge_sha=None):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}")
        run = store.claim(self.conn, self.project_id, ticket, now=started)
        if merged is not None:
            finish_run(self.conn, run, "merged", now=merged,
                       merge_sha=merge_sha)
        return run


class ToilTests(StoreTestCase):
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
        code, body = holophyte.serve.server.status(Project.locate(self.target),
                                            now=self.now)
        self.assertEqual(code, 200)
        self.assertEqual(body["toil"]["24h"], {
            "interventions": 3, "merged": 0, "per_merge": None,
            "by_action": {"requeue": 3},
            "by_actor": {"maintainer": 0, "operator": 3, "supervisor": 0,
                         "unknown": 0}})


class RevertTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.locate(self.target)
        self.git("init", "-q", "-b", "main")
        self.commit_at(self.now - 60 * DAY, "base")

    def git(self, *args, at=None):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        if at is not None:
            stamp = f"@{at // 1000} +0000"
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        return subprocess.run(["git", *args], cwd=self.target, env=env,
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit_at(self, at, name):
        (self.target / name).write_text(name)
        self.git("add", name)
        self.git("commit", "-q", "-m", name, at=at)
        return self.git("rev-parse", "HEAD")

    def merge(self, n, at):
        sha = self.commit_at(at, f"ticket-{n}")
        self.run_of(n, at - HOUR, merged=at, merge_sha=sha)
        return sha

    def revert(self, sha, at):
        self.git("revert", "--no-edit", sha, at=at)

    def report(self):
        self.conn.commit()
        return report.report_lines(self.conn, self.project)

    def test_a_revert_five_days_after_its_merge_counts(self):
        self.merge(1, self.now - 20 * DAY)
        second = self.merge(2, self.now - 12 * DAY)
        self.merge(3, self.now - 3 * DAY)
        self.revert(second, self.now - 7 * DAY)
        lines = self.report()
        self.assertIn("reverts 30d: 1 of 3 factory merges reverted within"
                      " 14 days (33.3%): KO-2", lines)
        self.assertIn("reverts 7d: 0 of 1 factory merges reverted within"
                      " 14 days (0.0%)", lines)

    def test_a_revert_twenty_days_after_its_merge_does_not_count(self):
        sha = self.merge(1, self.now - 25 * DAY)
        self.revert(sha, self.now - 5 * DAY)
        self.assertIn("reverts 30d: 0 of 1 factory merges reverted within"
                      " 14 days (0.0%)", self.report())

    def test_no_merge_in_the_window(self):
        self.merge(1, self.now - 40 * DAY)
        lines = self.report()
        self.assertIn("reverts 30d: no factory merges", lines)
        self.assertIn("reverts 7d: no factory merges", lines)

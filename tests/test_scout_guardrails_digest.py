"""A guardrails scout's findings digest: the last 30 days of review findings,
guardrail follow-ups and ticket defects clustered by path in the lead's plan
goal, its `scout_digest` event, and the end on an empty digest, run by
`factory.py --scout ID PROJECT` against stub lead CLIs.

Run: python3 -m unittest discover -s tests -p 'test_scout_guardrails_digest.py' -v
"""
import json
import re
import time
import unittest

import store
import store.follow_ups
import store.tickets
from store.scouts import create_scout, move_scout, scout
from tests.host_fixture import git
from tests.phase_fixture import finish_run
from tests.test_scout_lead import CLAUDE_LEAD
from tests.test_scout_process import ScoutFixture

MIN = 60 * 1000
DAY = 24 * 60 * MIN
SECRET = "sk-guardrails-digest-0123456789abcdef"


def finding(path, line, message="the save is not atomic", severity="p1"):
    return {"severity": severity, "message": message, "path": path,
            "line": line}


def cluster(goal, label):
    """The cluster's block in the plan goal: its line and the rows under it."""
    found = re.search(rf"^Cluster \d+ · {re.escape(label)} · .*?(?=\n\n|\Z)",
                      goal, re.M | re.S)
    return None if found is None else found.group(0)


class DigestCase(ScoutFixture):
    def setUp(self):
        super().setUp()
        self.now = int(time.time() * 1000)

    def store_for(self, config=CLAUDE_LEAD):
        self.path = self.project("nat", config)
        self.db = self.conn(self.path)
        (self.project_id,) = self.db.execute(
            "SELECT id FROM projects").fetchone()

    def ticket(self, n, now=None):
        ticket = store.tickets.mirror_ticket(
            self.db, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"NAT-{n}", title=f"ticket {n}",
            acceptance_criteria=[f"Given ticket {n}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MIN, now=now)
        store.tickets.transition(self.db, ticket, "in_flight")
        return ticket

    def run_of(self, n, rounds=(), started=None, finished=True):
        """A run of NAT-n with `rounds` as `(findings, ended_at)`."""
        started = self.now - 40 * DAY if started is None else started
        run = store.claim(self.db, self.project_id,
                          self.ticket(n, now=started), now=started)
        for number, (findings, ended_at) in enumerate(rounds, 1):
            store.record_review_round(
                self.db, run, number, "changes_requested", "reviewer-model",
                findings=findings, started_at=ended_at - MIN,
                ended_at=ended_at)
        if finished:
            last = rounds[-1][1] if rounds else started
            finish_run(self.db, run, "merged", now=last + MIN)
        return run

    def recurring(self):
        """Findings on app/save.py in NAT-3 and NAT-4, one on app/once.py
        in NAT-5; the F ids of the app/save.py ones."""
        three = self.run_of(3, [([finding("app/save.py", 4),
                                  finding("app/save.py", 9)],
                                 self.now - 2 * DAY)])
        four = self.run_of(4, [([finding("app/save.py", 4)],
                                self.now - DAY)])
        self.run_of(5, [([finding("app/once.py", 1)], self.now - DAY)])
        return [f"F{three}.1.1", f"F{three}.1.2", f"F{four}.1.1"]

    def guardrail_follow_ups(self):
        """A pending one on app/form.py and a ledger one with no path, of
        NAT-6; their ids."""
        run = self.run_of(6, finished=False)
        pending = store.follow_ups.record_follow_up(
            self.db, run, "f" * 40, "guardrail", True,
            "pin the form's field order", "form", path="app/form.py", line=7,
            now=self.now - 3 * DAY)
        ledger = store.follow_ups.record_follow_up(
            self.db, run, "f" * 40, "guardrail", True,
            "check every route is probed", "routes", now=self.now - 3 * DAY)
        store.follow_ups.settle_ledger(self.db, ledger, now=self.now - DAY)
        return pending, ledger

    def defect(self):
        """Run D of NAT-7, stopped on a contradiction naming C2 and C4,
        its ticket revised after it; D's id."""
        run = self.run_of(7, finished=False)
        store.record_event(
            self.db, run, "contradiction",
            "implementer: criteria C2, C4 cannot be met as written",
            level="detail", now=self.now - 2 * DAY, payload=json.dumps({
                "criteria": [{"criterion": 2, "id": "C2", "reason": "no field"},
                             {"criterion": 4, "id": "C4",
                              "reason": "contradicts C2"}]}))
        store.tickets.mirror_ticket(
            self.db, self.project_id, linear_issue_id="issue-7",
            linear_identifier="NAT-7", title="ticket 7",
            acceptance_criteria=["Given ticket 7, then it is worked"],
            verification_commands=["echo ok"], body="revised",
            now=self.now - DAY)
        return run

    def a_scout(self, kind="guardrails", helper_turns=None):
        scout_id = create_scout(self.db, self.project_id, "Mine the findings",
                                kind, 30, helper_turns=helper_turns)
        move_scout(self.db, scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main"))
        return scout_id

    def ran(self, scout_id):
        code, printed = self.cli("--scout", str(scout_id), str(self.path))
        self.assertEqual(code, 0, printed)

    def plan_goal(self):
        return self.stubs.turns("plan")[-1]["argv"][-1]

    def scout_events(self, scout_id):
        return self.db.execute(
            "SELECT kind, summary, payload FROM runEvents WHERE scoutId = ?"
            " ORDER BY seq", (scout_id,)).fetchall()


class DigestGoalTests(DigestCase):
    def test_a_path_in_two_tickets_is_a_cluster_and_a_path_in_one_is_not(self):
        self.store_for()
        ids = self.recurring()
        self.ran(self.a_scout())
        goal = self.plan_goal()
        self.assertIn("Findings digest", goal)
        block = cluster(goal, "app/save.py")
        self.assertIsNotNone(block, goal)
        self.assertIn("· 2 tickets ·", block.splitlines()[0])
        for row_id in ids:
            self.assertIn(f"- {row_id} · p1 · app/save.py:", block)
        self.assertNotIn("app/once.py", goal)

    def test_guardrail_follow_ups_cluster_by_path_or_as_having_none(self):
        self.store_for()
        self.recurring()
        pending, ledger = self.guardrail_follow_ups()
        self.ran(self.a_scout())
        goal = self.plan_goal()
        self.assertIn(f"- U{pending} · guardrail · app/form.py:7 · NAT-6",
                      cluster(goal, "app/form.py") or goal)
        self.assertIn(f"- U{ledger} · guardrail · NAT-6",
                      cluster(goal, "follow-ups with no path") or goal)

    def test_a_ticket_defect_is_a_row_of_its_stop_kinds_cluster(self):
        self.store_for()
        self.recurring()
        self.guardrail_follow_ups()
        run = self.defect()
        self.ran(self.a_scout())
        goal = self.plan_goal()
        self.assertIn(f"- D{run} · contradiction · NAT-7 · C2, C4",
                      cluster(goal, "ticket wording: contradiction") or goal)

    def test_rounds_ended_31_days_ago_are_out_of_the_window(self):
        self.store_for()
        self.recurring()
        old = self.now - 31 * DAY
        eight = self.run_of(8, [([finding("app/save.py", 1),
                                  finding("app/old.py", 2)], old),
                                ([], self.now - DAY)],
                            started=old - DAY)
        nine = self.run_of(9, [([finding("app/old.py", 3)], old)],
                           started=old - DAY)
        self.ran(self.a_scout())
        goal = self.plan_goal()
        self.assertIsNotNone(cluster(goal, "app/save.py"), goal)
        self.assertNotIn("app/old.py", goal)
        self.assertNotIn(f"F{eight}.1.", goal)
        self.assertNotIn(f"F{nine}.", goal)

    def test_clusters_past_helper_turns_are_cut_and_a_cluster_lists_its_newest_8(self):
        self.store_for()
        crowded = self.run_of(11, [
            ([finding("app/a.py", n) for n in range(1, 4)], self.now - 6 * DAY),
            ([finding("app/a.py", n) for n in range(4, 9)], self.now - 5 * DAY)])
        for n in (12, 13, 14):
            self.run_of(n, [([finding("app/a.py", n)], self.now - DAY)])
        for n in (15, 16, 17):
            self.run_of(n, [([finding("app/b.py", n)], self.now - DAY)])
        for n in (18, 19):
            self.run_of(n, [([finding("app/c.py", n)], self.now - DAY)])
        self.ran(self.a_scout(helper_turns=2))
        goal = self.plan_goal()
        self.assertIn("Cluster 1 · app/a.py · 4 tickets · 11 rows", goal)
        self.assertIn("Cluster 2 · app/b.py · 3 tickets · 3 rows", goal)
        self.assertNotIn("app/c.py", goal)
        self.assertIn("\n(1 cluster cut)", goal)
        block = cluster(goal, "app/a.py")
        self.assertEqual(len(re.findall(r"^- F", block, re.M)), 8)
        self.assertTrue(block.endswith("\n(+3 more)"), block)
        for n in range(1, 6):
            self.assertIn(f"F{crowded}.2.{n} ", block)
        for n in range(1, 4):
            self.assertNotIn(f"F{crowded}.1.{n} ", block)


class DigestRecordTests(DigestCase):
    def test_the_digest_event_comes_before_the_plan_turn_with_its_row_ids(self):
        self.store_for()
        saved = self.recurring()
        pending, ledger = self.guardrail_follow_ups()
        run = self.defect()
        scout_id = self.a_scout()
        self.ran(scout_id)
        events = self.scout_events(scout_id)
        kinds = [kind for kind, _, _ in events]
        self.assertEqual(kinds.count("scout_digest"), 1)
        self.assertLess(kinds.index("scout_digest"), kinds.index("agent_turn"))
        _, summary, payload = events[kinds.index("scout_digest")]
        self.assertEqual(summary, "digest: 4 clusters · 3 findings · "
                                  "2 follow-ups · 1 defect")
        self.assertEqual(
            [(each["label"], sorted(each["rows"]))
             for each in json.loads(payload)["clusters"]],
            [("app/save.py", sorted(saved)),
             ("app/form.py", [f"U{pending}"]),
             ("follow-ups with no path", [f"U{ledger}"]),
             ("ticket wording: contradiction", [f"D{run}"])])

    def test_an_empty_digest_ends_the_scout_done_with_no_turn_or_report(self):
        self.store_for()
        scout_id = self.a_scout()
        self.ran(scout_id)
        row = scout(self.db, scout_id)
        self.assertEqual((row.phase, row.reason),
                         ("done", "the findings digest is empty"))
        self.assertIn(("scout_digest", "digest: empty"),
                      [(kind, summary) for kind, summary, _ in
                       self.scout_events(scout_id)])
        self.assertEqual(self.stubs.calls(), [])
        self.assertEqual(self.db.execute(
            "SELECT count(*) FROM scoutReports WHERE scoutId = ?",
            (scout_id,)).fetchone(), (0,))

    def test_no_argv_carries_a_secret_and_a_recon_scout_gets_no_digest(self):
        self.store_for(CLAUDE_LEAD + f'[linear]\napi_key = "{SECRET}"\n')
        three = self.run_of(3, [([finding("app/save.py", 4,
                                          f"the key {SECRET} is logged")],
                                 self.now - DAY)])
        self.run_of(4, [([finding("app/save.py", 5)], self.now - DAY)])
        self.ran(self.a_scout())
        self.assertIn(f"F{three}.1.1", self.plan_goal())
        self.assertFalse([arg for call in self.stubs.calls()
                          for arg in call["argv"] if SECRET in arg])
        recon = self.a_scout(kind="recon")
        self.ran(recon)
        self.assertNotIn("Findings digest", self.plan_goal())
        self.assertNotIn("scout_digest",
                         [kind for kind, _, _ in self.scout_events(recon)])


if __name__ == "__main__":
    unittest.main()

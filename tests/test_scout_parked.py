"""A scout about a ticket parked in `needs_spec`: the park's reason in its
context pack, `revise` offered on its report goal and checked against the
park, and the stored item stamped with the ticket and revision it revises,
run by `factory.py --scout ID PROJECT` against stub lead CLIs over a native
board in a real git repository; the reads serving it and `holo` refusing to
file it.

Run: python3 -m unittest discover -s tests -p 'test_scout_parked.py' -v
"""
import unittest

import store
import store.board
import store.tickets
from holophyte.config.project import Project
from holophyte.review.freshness import stale_comment
from holophyte.scout.report import report_problems
from store.scout_thread import post_message, take_message
from store.scouts import create_scout, move_scout, scout
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_lead import CLAUDE_LEAD
from tests.test_scout_process import ScoutFixture
from tests.test_scout_revise import EMPTY_PLAN, NATIVE, REVISE, ReviseCase
from tests.test_serve_board import ServeBoardCase
from tests.test_serve_ticket_check import body

CRITIC = "critic: stale — the save path moved to app/forms/save.py"
LANDMARK = "`app/save.py` (named in What / Why / How) is not on main"
REPORT = {"summary": "the save path moved", "findings": [],
          "actions": [dict(REVISE,
                           revises={"ticket": "NAT-9", "revision": 7})]}
T0 = 1_700_000_000_000


class ParkedCase(ScoutFixture):
    """`native`, a native board `NAT` whose `NAT-3` is at revision 2."""

    start_host = ServeBoardCase.start_host
    request = ServeBoardCase.request
    goal = ReviseCase.goal
    items = ReviseCase.items
    written = ReviseCase.written
    holo = ReviseCase.holo

    def setUp(self):
        super().setUp()
        self.path = self.repo("native")
        Project.locate(self.path, adopt=False).config_path.write_text(
            NATIVE + CLAUDE_LEAD)
        git(self.path, "checkout", "-q", "-b", "main")
        (self.path / "notes.txt").write_text("notes\n")
        git(self.path, "add", "notes.txt")
        git(self.path, "commit", "-q", "-m", "first")
        code, printed = self.cli("project", "add", str(self.path))
        self.assertIsNone(code, printed)
        self.db = self.conn(self.path)
        self.project_id = store.tickets.ensure_project(self.db, "native:NAT",
                                                       self.path)
        for number in (1, 2, 3):
            store.board.file_ticket(self.db, self.project_id, "NAT",
                                    body(f"Ticket {number}"))
        store.board.edit_ticket(self.db, self.project_id, "NAT-3",
                                body("Save keeps the name"), 1)
        (self.ticket,) = self.db.execute("SELECT id FROM tickets WHERE"
                                         " linearIdentifier = 'NAT-3'"
                                         ).fetchone()
        self.stubs.answer(plan=EMPTY_PLAN, report=fenced(REPORT),
                          repair=fenced(REPORT))

    def park(self, kind, text, at=T0):
        store.record_note(self.db, self.ticket, kind, text, f"{kind}:{at}",
                          now=at)
        with self.db:
            self.db.execute("UPDATE tickets SET status = 'needs_spec'"
                            " WHERE id = ?", (self.ticket,))

    def run_scout(self):
        scout_id = create_scout(self.db, self.project_id, "why is it parked?",
                                "research", 30, subject=("ticket", "NAT-3"))
        move_scout(self.db, scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main"))
        code, printed = self.cli("--scout", str(scout_id), str(self.path))
        return scout_id, code, printed

    def context_summary(self, scout_id):
        (summary,) = self.db.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_context'", (scout_id,)).fetchone()
        return summary


class ReportCheckTests(unittest.TestCase):
    def checked(self, subject, parked):
        return report_problems({"summary": "found", "findings": [],
                                "actions": [REVISE]}, "research", subject,
                               parked=parked)

    def test_a_revise_item_on_a_ticket_subject_passes_only_when_parked(self):
        self.assertTrue(REVISE["body"].startswith("# Save keeps the title"))
        self.assertEqual(self.checked(("ticket", "NAT-3"), True), [])
        problems = [problem for problem
                    in self.checked(("ticket", "NAT-3"), False)
                    if problem.startswith("actions[0].kind:")]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("parked ticket", problems[0])
        for parked in (True, False):
            with self.subTest(parked=parked):
                self.assertEqual(self.checked(("defect", "12"), parked), [])


class ParkedContextTests(ParkedCase):
    def test_a_critic_park_reaches_the_plan_and_offers_revise(self):
        self.park("critic", CRITIC)
        scout_id, code, printed = self.run_scout()
        self.assertEqual(code, 0, printed)
        plan = self.goal("plan")
        self.assertIn("Parked: critic at revision 2", plan)
        self.assertIn(CRITIC, plan)
        self.assertIn("parked critic", self.context_summary(scout_id))
        self.assertIn("revise", self.goal("report"))

    def test_a_landmark_park_and_a_critic_park_kept_as_stale_name_their_kind(
            self):
        self.park("validation", "C2 has no verify command", at=T0)
        self.park("stale", stale_comment([LANDMARK], "NAT-3"), at=T0 + 1)
        _, code, printed = self.run_scout()
        self.assertEqual(code, 0, printed)
        plan = self.goal("plan")
        self.assertIn("Parked: stale", plan)
        self.assertIn("app/save.py", plan)
        self.assertNotIn("Parked: validation", plan)
        self.park("stale", stale_comment([CRITIC], "NAT-3"), at=T0 + 2)
        _, code, printed = self.run_scout()
        self.assertEqual(code, 0, printed)
        self.assertIn("Parked: critic", self.stubs.turns("plan")[-1]["argv"][-1])


class ParkedReviseTests(ParkedCase):
    def test_the_revise_item_is_stamped_served_and_shown_with_the_ticket(self):
        self.park("critic", CRITIC)
        scout_id, code, printed = self.run_scout()
        self.assertEqual(code, 0, printed)
        self.assertEqual(scout(self.db, scout_id).phase, "done")
        (item,) = self.items(scout_id)
        self.assertEqual(item["revises"], {"ticket": "NAT-3", "revision": 2})
        self.start_host()
        status, answer, _ = self.request(
            "GET", f"/projects/native/scouts/{scout_id}", revision=None)
        self.assertEqual(status, 200, answer)
        (served,) = answer["actions"]
        self.assertEqual(served["revises"], {"ticket": "NAT-3", "revision": 2})
        shown = self.holo("scout", "show", str(scout_id), "-p", str(self.path))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertTrue(any(line.startswith("A1 revise NAT-3 r2:")
                            for line in shown.stdout.splitlines()),
                        shown.stdout)

    def test_a_ready_tickets_scout_gets_no_park_and_its_revise_item_fails(self):
        scout_id, code, printed = self.run_scout()
        self.assertEqual(code, 1, printed)
        self.assertNotIn("Parked:", self.goal("plan"))
        self.assertNotIn("revise", self.goal("report"))
        row = scout(self.db, scout_id)
        self.assertEqual(row.phase, "failed")
        self.assertTrue(row.reason.startswith(
            "the lead's report is malformed: actions[0].kind:"), row.reason)

    def test_a_steered_pass_on_the_still_parked_ticket_keeps_its_revise_item(
            self):
        self.park("critic", CRITIC)
        scout_id, code, printed = self.run_scout()
        self.assertEqual(code, 0, printed)
        self.stubs.answer(pass_plan=EMPTY_PLAN, report=fenced(REPORT),
                          repair=fenced(REPORT))
        take_message(self.db, post_message(self.db, scout_id, "maintainer",
                                           "steer", "Check the form too."))
        code, printed = self.cli("--scout", str(scout_id), str(self.path))
        self.assertEqual(code, 0, printed)
        self.assertIn("revise", self.stubs.turns("report")[-1]["argv"][-1])
        first, second = self.items(scout_id)
        self.assertEqual(second["revises"], {"ticket": "NAT-3", "revision": 2})

    def test_holo_scout_file_refuses_the_item_naming_the_ticket(self):
        self.park("critic", CRITIC)
        scout_id, code, printed = self.run_scout()
        self.assertEqual(code, 0, printed)
        before = self.written(scout_id)
        filed = self.holo("scout", "file", str(scout_id), "1",
                          "-p", str(self.path))
        self.assertEqual(filed.returncode, 1, filed.stderr)
        self.assertIn("revises NAT-3", filed.stdout + filed.stderr)
        self.assertEqual(self.written(scout_id)[0], before[0])


if __name__ == "__main__":
    unittest.main()

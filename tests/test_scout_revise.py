"""A ticket-defect scout's `revise` action item: the report check's rules
for it, the lead's report goal offering it and the stored item stamped with
the ticket and revision it revises, run by `factory.py --scout ID PROJECT`
against stub lead CLIs over a native board in a real git repository; the
reads serving it, and its filing refused by a host daemon and by `holo`.

Run: python3 -m unittest discover -s tests -p 'test_scout_revise.py' -v
"""
import json
import os
import subprocess
import sys
import unittest

import store
import store.board
import store.tickets
from holophyte.config.project import Project
from holophyte.scout.report import report_problems
from store.scouts import create_scout, move_scout, scout
from tests.host_fixture import REPO, git
from tests.scout_stubs import fenced
from tests.test_scout_lead import CLAUDE_LEAD
from tests.test_scout_process import ScoutFixture
from tests.test_serve_board import ServeBoardCase
from tests.test_serve_ticket_check import body

REVISION = body("Save keeps the title")
FIX = {"title": "Back off between retries", "why": "the loop spins",
       "kind": "fix", "body": body("Back off between retries")}
REVISE = {"title": "Name the saved field", "why": "C2 says 'the name'",
          "kind": "revise", "body": REVISION}
REPORT = {"summary": "the ticket names the wrong field", "findings": [],
          "actions": [FIX, dict(REVISE,
                                revises={"ticket": "NAT-9", "revision": 7})]}
EMPTY_PLAN = fenced({"tasks": []})
NATIVE = '[board]\nkind = "native"\nprefix = "NAT"\n'


def checked(actions, subject):
    return report_problems({"summary": "found", "findings": [],
                            "actions": actions}, "research", subject)


class ReportCheckTests(unittest.TestCase):
    def test_a_revise_item_passes_only_on_a_ticket_defect_subject(self):
        self.assertEqual(checked([REVISE], ("defect", "12")), [])
        for subject in (("run", "12"), ("ticket", "NAT-3"), None):
            with self.subTest(subject=subject):
                problems = [problem for problem in checked([REVISE], subject)
                            if problem.startswith("actions[0].kind:")]
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("ticket-defect", problems[0])

    def test_a_second_revise_item_and_an_untitled_revision_are_problems(self):
        twice = checked([REVISE, REVISE], ("defect", "12"))
        self.assertTrue(any(problem.startswith("actions[1].kind:")
                            for problem in twice), twice)
        untitled = checked([dict(REVISE, body=REVISION.removeprefix("# "))],
                           ("defect", "12"))
        self.assertTrue(any(problem.startswith("actions[0].body:")
                            for problem in untitled), untitled)


class ReviseCase(ScoutFixture):
    """`native`, a native board `NAT` whose `NAT-3` is at revision 2 and
    has run D, failed on a `contradiction`."""

    start_host = ServeBoardCase.start_host
    request = ServeBoardCase.request

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
        (ticket,) = self.db.execute("SELECT id FROM tickets WHERE"
                                    " linearIdentifier = 'NAT-3'").fetchone()
        store.tickets.transition(self.db, ticket, "in_flight")
        self.run_id = store.claim(self.db, self.project_id, ticket)
        store.record_event(self.db, self.run_id, "contradiction",
                           "C2 contradicts C4", level="detail",
                           payload=json.dumps({"criteria": [{"id": "C2"}]}))
        store.release(self.db, self.run_id, "failed", reason="contradiction")
        self.stubs.answer(plan=EMPTY_PLAN, report=fenced(REPORT),
                          repair=fenced(REPORT))

    def run_scout(self, subject_kind):
        scout_id = create_scout(self.db, self.project_id, "why did it stop?",
                                "research", 30,
                                subject=(subject_kind, str(self.run_id)))
        move_scout(self.db, scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main"))
        code, printed = self.cli("--scout", str(scout_id), str(self.path))
        return scout_id, code, printed

    def goal(self, turn):
        (call,) = self.stubs.turns(turn)
        return call["argv"][-1]

    def items(self, scout_id):
        return [json.loads(item) for (item,) in self.db.execute(
            "SELECT item FROM scoutActions WHERE scoutId = ?"
            " ORDER BY position", (scout_id,))]

    def written(self, scout_id):
        (seq,) = self.db.execute("SELECT ticketSeq FROM projects WHERE id = ?",
                                 (self.project_id,)).fetchone()
        return seq, self.db.execute(
            "SELECT position, ticketKey, filedAt FROM scoutActions"
            " WHERE scoutId = ? ORDER BY position", (scout_id,)).fetchall()

    def holo(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "holophyte.holo", *argv], cwd=REPO,
            env={**os.environ, "HOLO_TRANSPORT": "local"},
            capture_output=True, text=True, timeout=60)


class DefectScoutTests(ReviseCase):
    def test_the_defect_scouts_goal_offers_revise_and_the_item_is_stamped(self):
        scout_id, code, printed = self.run_scout("defect")
        self.assertEqual(code, 0, printed)
        self.assertIn("revise", self.goal("report"))
        fix, revise = self.items(scout_id)
        self.assertEqual(revise["revises"], {"ticket": "NAT-3", "revision": 2})
        self.assertNotIn("revises", fix)

    def test_a_run_scouts_revise_item_fails_its_report_after_the_repair(self):
        scout_id, code, printed = self.run_scout("run")
        self.assertEqual(code, 1, printed)
        self.assertNotIn("revise", self.goal("report"))
        self.assertIn("actions[1].kind:", self.goal("repair"))
        row = scout(self.db, scout_id)
        self.assertEqual(row.phase, "failed")
        self.assertTrue(row.reason.startswith(
            "the lead's report is malformed: actions[1].kind:"), row.reason)
        self.assertEqual(self.items(scout_id), [])

    def test_the_reads_serve_the_revise_item_with_what_it_revises(self):
        scout_id, code, printed = self.run_scout("defect")
        self.assertEqual(code, 0, printed)
        self.start_host()
        status, answer, _ = self.request(
            "GET", f"/projects/native/scouts/{scout_id}", revision=None)
        self.assertEqual(status, 200, answer)
        fix, revise = answer["actions"]
        self.assertEqual((revise["kind"], revise["revises"]),
                         ("revise", {"ticket": "NAT-3", "revision": 2}))
        self.assertIsNone(fix["revises"])
        shown = self.holo("scout", "show", str(scout_id), "-p", str(self.path))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertTrue(any(line.startswith("A2 revise NAT-3 r2:")
                            for line in shown.stdout.splitlines()),
                        shown.stdout)


class ReviseFilingTests(ReviseCase):
    def setUp(self):
        super().setUp()
        self.scout_id, code, printed = self.run_scout("defect")
        self.assertEqual(code, 0, printed)

    def file(self, item):
        status, answer, _ = self.request(
            "POST", "/projects/native/actions/scout-file",
            {"scout": self.scout_id, "item": item}, revision=None)
        return status, answer

    def test_the_daemon_refuses_a_revise_item_and_still_files_a_fix(self):
        self.start_host()
        before = self.written(self.scout_id)
        status, answer = self.file(2)
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["ok"], answer["ticket"]), (False, None))
        self.assertIn("revises NAT-3 at revision 2", answer["detail"])
        self.assertEqual(self.written(self.scout_id), before)
        self.assertEqual(before[1][1], (2, None, None))
        status, answer = self.file(1)
        self.assertEqual((status, answer["ok"], answer["ticket"]),
                         (200, True, "NAT-4"), answer)
        (column,) = self.db.execute("SELECT boardColumn FROM tickets WHERE"
                                    " linearIdentifier = 'NAT-4'").fetchone()
        self.assertEqual(column, "backlog")

    def test_holo_scout_file_exits_1_naming_the_ticket_it_revises(self):
        before = self.written(self.scout_id)
        filed = self.holo("scout", "file", str(self.scout_id), "2",
                          "-p", str(self.path))
        self.assertEqual(filed.returncode, 1, filed.stderr)
        self.assertIn("revises NAT-3", filed.stdout + filed.stderr)
        self.assertEqual(self.written(self.scout_id), before)


if __name__ == "__main__":
    unittest.main()

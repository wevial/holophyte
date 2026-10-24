"""A guardrails scout's `guardrail` action items: the report check's rules
for their kind, layer and covered digest rows; the lead's report goal
offering them and the stored item, run by `factory.py --scout ID PROJECT`
against stub lead CLIs; the reads serving them, their filing by a host
daemon, and the rows a filed item covers left out of a later digest.

Run: python3 -m unittest discover -s tests -p 'test_scout_guardrail_items.py' -v
"""
import json
import os
import subprocess
import sys
import unittest

from holophyte.scout.report import report_problems
from store.scouts import scout
from tests.host_fixture import REPO
from tests.scout_stubs import fenced
from tests.test_scout_guardrails_digest import DAY, DigestCase, finding
from tests.test_serve_board import ServeBoardCase
from tests.test_serve_ticket_check import body

OFFERED = {"F812.2.1", "U41"}
GUARD = {"title": "Write saves through a temporary file",
         "why": "three tickets shipped a torn save",
         "kind": "guardrail", "body": body("Write saves atomically"),
         "layer": "static", "covers": ["F812.2.1", "U41"]}
FIX = {"title": "Fix the save", "why": "it tears", "kind": "fix",
       "body": body("Fix the save")}
NOTHING = {"summary": "nothing found", "findings": [], "actions": []}


def checked(action, kind="guardrails", digest=OFFERED):
    return report_problems({"summary": "found", "findings": [],
                            "actions": [action]}, kind, digest=digest)


def starting(problems, prefix):
    return [problem for problem in problems if problem.startswith(prefix)]


class ReportCheckTests(unittest.TestCase):
    def test_a_guardrail_item_with_a_layer_and_offered_rows_passes(self):
        self.assertEqual(checked(GUARD), [])

    def test_a_layer_off_the_ladder_and_covers_not_in_the_digest_are_problems(
            self):
        for change, prefix in ((dict(layer="none"), "actions[0].layer:"),
                               (dict(layer="lint"), "actions[0].layer:"),
                               (dict(covers=[]), "actions[0].covers:"),
                               (dict(covers=["F999.1.1"]),
                                "actions[0].covers:")):
            with self.subTest(change=change):
                problems = checked(dict(GUARD, **change))
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(prefix), problems)
        self.assertIn("F999.1.1", checked(dict(GUARD, covers=["F999.1.1"]))[0])

    def test_the_check_holds_guardrails_scouts_to_guardrail_and_decide_items(
            self):
        fixed = starting(checked(FIX), "actions[0].kind:")
        self.assertEqual(len(fixed), 1, fixed)
        self.assertIn("guardrail or decide", fixed[0])
        recon = starting(checked(GUARD, kind="recon", digest=None),
                         "actions[0].kind:")
        self.assertEqual(len(recon), 1, recon)
        self.assertIn("guardrails scout", recon[0])


class GuardrailItemCase(DigestCase):
    start_host = ServeBoardCase.start_host
    request = ServeBoardCase.request

    def setUp(self):
        super().setUp()
        self.store_for()
        self.ids = [f"F{self.run_of(n, rounds)}.1.1" for n, rounds in (
            (n, [([finding("app/save.py", n)], self.now - DAY)])
            for n in (3, 4, 5))]

    def first_scout(self, covers):
        self.stubs.answer(report=fenced(dict(NOTHING, actions=[
            dict(GUARD, covers=covers)])))
        scout_id = self.a_scout()
        self.ran(scout_id)
        return scout_id

    def file(self, scout_id):
        status, answer, _ = self.request(
            "POST", "/projects/nat/actions/scout-file",
            {"scout": scout_id, "item": 1}, revision=None)
        self.assertEqual(status, 200, answer)
        return answer

    def second_goal(self):
        self.stubs.answer(report=fenced(NOTHING))
        scout_id = self.a_scout()
        self.ran(scout_id)
        return scout_id, self.plan_goal()

    def holo(self, *argv):
        return subprocess.run(
            [sys.executable, "-m", "holophyte.holo", *argv], cwd=REPO,
            env={**os.environ, "HOLO_TRANSPORT": "local"},
            capture_output=True, text=True, timeout=60)


class GuardrailItemTests(GuardrailItemCase):
    def test_the_report_goal_offers_guardrail_items_and_the_item_is_stored(
            self):
        scout_id = self.first_scout(self.ids[:1])
        goal = self.stubs.turns("report")[-1]["argv"][-1]
        for word in ('"guardrail"', '"layer"', '"covers"'):
            self.assertIn(word, goal)
        (item,) = self.db.execute("SELECT item FROM scoutActions"
                                  " WHERE scoutId = ?", (scout_id,)).fetchone()
        self.assertEqual((json.loads(item)["layer"], json.loads(item)["covers"]),
                         ("static", self.ids[:1]))

    def test_the_reads_serve_the_items_layer_and_covered_rows(self):
        scout_id = self.first_scout(self.ids[:1])
        self.start_host()
        status, answer, _ = self.request(
            "GET", f"/projects/nat/scouts/{scout_id}", revision=None)
        self.assertEqual(status, 200, answer)
        (action,) = answer["actions"]
        self.assertEqual((action["kind"], action["layer"], action["covers"]),
                         ("guardrail", "static", self.ids[:1]))
        shown = self.holo("scout", "show", str(scout_id), "-p", str(self.path))
        self.assertEqual(shown.returncode, 0, shown.stderr)
        lines = [line for line in shown.stdout.splitlines()
                 if line.startswith("A1 guardrail static:")]
        self.assertEqual(len(lines), 1, shown.stdout)
        self.assertTrue(lines[0].endswith("(covers 1)"), lines[0])

    def test_a_host_daemon_files_a_guardrail_item_to_backlog(self):
        scout_id = self.first_scout(self.ids[:1])
        self.start_host()
        answer = self.file(scout_id)
        self.assertIs(answer["ok"], True, answer)
        (column,) = self.db.execute(
            "SELECT boardColumn FROM tickets WHERE linearIdentifier = ?",
            (answer["ticket"],)).fetchone()
        self.assertEqual(column, "backlog")


class CoverageTests(GuardrailItemCase):
    def test_a_filed_items_rows_leave_the_next_digest_and_an_unfiled_ones_stay(
            self):
        first = self.first_scout(self.ids[:1])
        _, unfiled = self.second_goal()
        self.assertIn(f"- {self.ids[0]} ·", unfiled)
        self.start_host()
        self.assertIs(self.file(first)["ok"], True)
        _, goal = self.second_goal()
        self.assertNotIn(f"- {self.ids[0]} ·", goal)
        for row_id in self.ids[1:]:
            self.assertIn(f"- {row_id} ·", goal)

    def test_a_filed_item_covering_the_only_cluster_empties_the_digest(self):
        first = self.first_scout(self.ids)
        self.start_host()
        self.assertIs(self.file(first)["ok"], True)
        self.stubs.answer(report=fenced(NOTHING))
        later = self.a_scout()
        self.ran(later)
        row = scout(self.db, later)
        self.assertEqual((row.phase, row.reason),
                         ("done", "the findings digest is empty"))


if __name__ == "__main__":
    unittest.main()

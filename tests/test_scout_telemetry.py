"""A report's cited helper tasks, checked and stored, and the scout section of
`factory.py --report`: the ended scouts' time, tokens and cost per lead and
helper route, and how many done helper tasks a finding cited.

Run: python3 -m unittest discover -s tests -p 'test_scout_telemetry.py' -v
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

import holophyte.cli.report as report
import store
from holophyte.agents.harness import SCOUT_HELPER, SCOUT_LEAD
from holophyte.scout.report import report_problems
from holophyte.scout.telemetry import scout_report_lines
from store.scouts import (
    create_scout,
    end_scout,
    end_scout_task,
    move_scout,
    record_scout_event,
    record_scout_report,
    record_scout_tasks,
    start_scout_task,
)
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_process import ScoutFixture
from tests.test_wiring_telemetry import ReportStoreCase

T0 = 1_700_000_000_000
MINUTE = 60_000
SCOUTS = ("scouts: 2 ended (done 1 · failed 1 · stopped 0) · 15.0 min ·"
          " $0.5030 (1 unpriced)")
OPUS = "scout lead opus: 1 scout · 2 turns · 3.0 min · 15000 tokens · $0.5000"
SOL = ("scout lead sol: 1 scout · 1 turn · 5.0 min · 40000 tokens ·"
       " cost not reported")
LUNA = ("scout helper luna: 3 tasks · done 2 · cited 2 · 4.0 min ·"
        " 6000 tokens · $0.0030")
SECTION = ("scouts:", "scout lead", "scout helper")
FINDING = {"title": "the retry loop never backs off", "status": "confirmed",
           "evidence": [{"url": "https://example.test/retry"}]}


def a_report(*cited):
    return {"summary": "found", "actions": [],
            "findings": [dict(FINDING, tasks=tasks) for tasks in cited]}


def section(lines):
    return [line for line in lines if line.startswith(SECTION)]


class CitedTaskCheckTests(unittest.TestCase):
    def problems(self, **cited):
        report = dict(a_report(), findings=[dict(FINDING, **cited)])
        return report_problems(report, "research", scout=7, tasks={41, 42})

    def test_a_finding_citing_its_scouts_task_or_none_passes(self):
        self.assertEqual(self.problems(tasks=[41]), [])
        self.assertEqual(self.problems(), [])

    def test_an_unknown_task_id_is_named_on_the_findings_tasks(self):
        problems = [problem for problem in self.problems(tasks=[99])
                    if problem.startswith("findings[0].tasks:")]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("99", problems[0])

    def test_tasks_that_are_no_list_of_ids_are_a_problem(self):
        for tasks in ("41", None):
            with self.subTest(tasks=tasks):
                self.assertTrue(any(problem.startswith("findings[0].tasks:")
                                    for problem in self.problems(tasks=tasks)))


class CitedTaskRunTests(ScoutFixture):
    def test_the_report_turn_labels_each_task_and_the_stored_finding_cites_one(self):
        plan = {"tasks": [{"question": question, "paths": ["notes.txt"],
                           "mode": "read"}
                          for question in ("Where is the retry loop?",
                                           "Who calls the retry loop?")]}
        self.stubs.answer(plan=fenced(plan), report=fenced(a_report([1])))
        path = self.project("alpha")
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "why does the retry spin?",
                                "research", 30)
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 0, printed)
        ids = [task_id for (task_id,) in conn.execute(
            "SELECT id FROM scoutTasks WHERE scoutId = ? ORDER BY id",
            (scout_id,))]
        self.assertEqual(ids, [1, 2])
        (turn,) = self.stubs.turns("report")
        for task_id in ids:
            self.assertRegex(turn["argv"][-1], rf"\bT{task_id}\b")
        (stored,) = conn.execute("SELECT report FROM scoutReports"
                                 " WHERE scoutId = ?", (scout_id,)).fetchone()
        self.assertEqual(json.loads(stored)["findings"][0]["tasks"], [1])


class ScoutReportLinesTests(ReportStoreCase):
    def a_scout(self, lead, phase="planning"):
        scout_id = create_scout(self.conn, self.project_id, "why?", "research",
                                30, lead_route=lead, helper_route="luna",
                                now=T0)
        if phase != "queued":
            move_scout(self.conn, scout_id, "planning", base_sha="abc123",
                       now=T0)
        return scout_id

    def turn(self, scout_id, role, seconds, usage):
        record_scout_event(self.conn, scout_id, "agent_turn",
                           f"{role} turn ended", level="detail",
                           payload=json.dumps({"role": role, "route": "primary",
                                               "seconds": seconds,
                                               "usage": usage}))

    def tasks(self, scout_id, number, ended):
        ids = record_scout_tasks(self.conn, scout_id, number, 1, [
            {"question": f"task {at}", "mode": "read", "route": "luna"}
            for at in range(len(ended))])
        for task_id, (phase, seconds, tokens, cost) in zip(ids, ended):
            start_scout_task(self.conn, task_id, now=T0)
            end_scout_task(self.conn, task_id, phase, tokens=tokens,
                           cost_usd=cost, now=T0 + seconds * 1000)
            self.turn(scout_id, SCOUT_HELPER, seconds,
                      {"total_tokens": tokens, "cost_usd": cost})
        return ids

    def seed(self, second_pass=False):
        a = self.a_scout("opus")
        self.turn(a, SCOUT_LEAD, 120, {"input_tokens": 8000,
                                       "output_tokens": 2000, "cost_usd": 0.30})
        self.turn(a, SCOUT_LEAD, 60, {"input_tokens": 4000,
                                      "output_tokens": 1000, "cost_usd": 0.20})
        first, second, third = self.tasks(a, 1, [
            ("done", 60, 1000, 0.0005), ("done", 120, 3000, 0.0015),
            ("failed", 60, 2000, 0.001)])
        record_scout_report(self.conn, a, 1, a_report([first],
                                                      [first, second]))
        if second_pass:
            (later,) = self.tasks(a, 2, [("done", 60, 500, 0.0002)])
            record_scout_report(self.conn, a, 2, a_report([later, third]))
        end_scout(self.conn, a, "done", "the lead wrote its report",
                  now=T0 + 10 * MINUTE)
        b = self.a_scout("sol")
        self.turn(b, SCOUT_LEAD, 300, {"total_tokens": 40000,
                                       "cost_usd": None})
        end_scout(self.conn, b, "failed", "the lead's report is malformed",
                  now=T0 + 5 * MINUTE)

    def test_each_lead_and_helper_route_has_its_line_in_name_order(self):
        self.seed()
        self.assertEqual(scout_report_lines(self.conn), [SCOUTS, OPUS, SOL, LUNA])

    def test_a_later_pass_citation_counts_and_a_failed_task_is_never_cited(self):
        self.seed(second_pass=True)
        (luna,) = [line for line in scout_report_lines(self.conn)
                   if line.startswith("scout helper luna:")]
        self.assertIn(": 4 tasks · done 3 · cited 3 ·", luna)

    def test_no_section_without_an_ended_scout(self):
        self.assertEqual(section(report.report_lines(self.conn)), [])
        self.a_scout("opus", phase="queued")
        self.a_scout("opus")
        self.assertEqual(section(report.report_lines(self.conn)), [])

    def test_factory_report_prints_the_section_after_its_questions_line(self):
        self.seed()
        self.completed_run(1, 5, 25, 0, "merged")
        (run_id,) = self.conn.execute("SELECT id FROM runs").fetchone()
        store.record_event(self.conn, run_id, "question", "asked",
                           level="detail", payload=json.dumps({
                               "backend": "claude", "input_tokens": 10,
                               "output_tokens": 2, "cost_usd": 0.001}))
        result = subprocess.run(
            [sys.executable, "factory.py", "--report", str(self.target)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        questions = [at for at, line in enumerate(lines)
                     if line.startswith("questions:")]
        self.assertEqual(len(questions), 1, result.stdout)
        self.assertGreater(lines.index(SCOUTS), questions[0])
        self.assertGreater(lines.index(LUNA), questions[0])


if __name__ == "__main__":
    unittest.main()

"""A scout's lead: its probe, plan turn and report turn under the scout's
time budget, run by `factory.py --scout ID PROJECT` against stub lead CLIs
over a real git repository.

Run: python3 -m unittest discover -s tests -p 'test_scout_lead.py' -v
"""
import json
import os
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import holophyte.scout.lead as lead
import holophyte.scout.scout_process as scout_process
from holophyte.config.project import Project
from store.scouts import create_scout, move_scout, scout
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_process import ScoutFixture

CLAUDE_LEAD = ('[scout]\nlead = "opus"\n[scout.leads.opus]\nharness = "claude"\n'
               'model = "opus"\neffort = "high"\n')
CODEX_LEAD = ('[scout]\nlead = "sol"\n[scout.leads.sol]\nharness = "codex"\n'
              'model = "gpt-6.1-sol"\neffort = "high"\n')
PLAN = {"tasks": [
    {"question": "Where is the retry loop?", "paths": ["notes.txt"],
     "mode": "read"},
    {"question": "Does the retry test fail on main?", "paths": [],
     "mode": "run"}]}
REPORT = {"summary": "the retry loop never backs off",
          "findings": [{"title": "no backoff", "status": "shown",
                        "evidence": [{"url": "https://example.test/retry"}]}],
          "actions": [],
          "reproduction": {"level": "test", "hypotheses": [
              {"hypothesis": "no sleep", "separates": "the loop's timing",
               "verdict": "supported"}]}}
UNSHOWN = dict(REPORT, reproduction=dict(
    REPORT["reproduction"], level="not_reproduced", claimed_level="test",
    why="no run helper finished"))
SECRET = "sk-scout-lead-0123456789abcdef"


def running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split(") ")[-1][0] == "Z")


class ScoutLeadTests(ScoutFixture):
    def setUp(self):
        super().setUp()
        self.stubs.answer(plan=fenced(PLAN), report=fenced(REPORT))

    def bug_scout(self, config=CLAUDE_LEAD, ask="why does the retry spin?"):
        path = self.project("alpha", config)
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, ask, "bug", 30)
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        return path, conn, scout_id

    def run_scout(self, path, scout_id):
        return self.cli("--scout", str(scout_id), str(path))

    def report(self, conn, scout_id):
        rows = conn.execute("SELECT report FROM scoutReports WHERE scoutId = ?"
                            " AND pass = 1", (scout_id,)).fetchall()
        return [json.loads(report) for (report,) in rows]

    def tasks(self, conn, scout_id):
        return conn.execute(
            "SELECT pass, wave, question, paths, mode, phase FROM scoutTasks"
            " WHERE scoutId = ? ORDER BY id", (scout_id,)).fetchall()

    def turn_events(self, conn, scout_id):
        return [json.loads(payload) for (payload,) in conn.execute(
            "SELECT payload FROM runEvents WHERE scoutId = ?"
            " AND kind = 'agent_turn' ORDER BY seq", (scout_id,))]

    def test_a_planned_scout_stores_its_tasks_and_report_and_ends_done(self):
        path, conn, scout_id = self.bug_scout()
        code, printed = self.run_scout(path, scout_id)
        self.assertEqual(code, 0, printed)
        self.assertEqual(self.tasks(conn, scout_id), [
            (1, 1, "Where is the retry loop?", "[]", "read", "done"),
            (1, 1, "Does the retry test fail on main?", "[]", "run", "done")])
        self.assertEqual(self.report(conn, scout_id), [dict(REPORT, cut=[])])
        self.assertEqual(scout(conn, scout_id).phase, "done")

    def test_a_claude_lead_resumes_the_session_it_recorded_at_dispatch(self):
        path, conn, scout_id = self.bug_scout()
        self.stubs.answer(plan=fenced(PLAN), report=fenced(REPORT),
                          store=str(Project.locate(path, adopt=False).store_path))
        self.run_scout(path, scout_id)
        (plan,), (report,) = self.stubs.turns("plan"), self.stubs.turns("report")
        session = plan["argv"][plan["argv"].index("--session-id") + 1]
        self.assertEqual(plan["sessions"], [session])
        self.assertEqual(scout(conn, scout_id).lead_session, session)
        self.assertEqual(report["argv"][report["argv"].index("--resume") + 1],
                         session)
        worktree = str(self.lead(path, scout_id))
        self.assertEqual([plan["cwd"], report["cwd"]], [worktree, worktree])

    def test_a_codex_lead_resumes_the_session_its_banner_reported(self):
        path, conn, scout_id = self.bug_scout(CODEX_LEAD)
        session = str(uuid.uuid4())
        self.stubs.answer(plan=f"session id: {session}\n{fenced(PLAN)}",
                          report=fenced(REPORT))
        code, printed = self.run_scout(path, scout_id)
        self.assertEqual(code, 0, printed)
        self.assertEqual(scout(conn, scout_id).lead_session, session)
        (report,) = self.stubs.turns("report")
        self.assertEqual(Path(report["argv"][0]).name, "codex")
        self.assertEqual(report["argv"][1:5],
                         ["exec", "--sandbox", "read-only", "resume"])
        self.assertIn(session, report["argv"])

    def test_a_plan_turn_past_the_budget_is_killed_and_the_report_still_runs(self):
        path, conn, scout_id = self.bug_scout()
        pid_file = self.root / "plan-child.pid"
        self.stubs.answer(plan=fenced(PLAN), plan_sleep=60,
                          plan_pid_file=str(pid_file), report=fenced(REPORT))
        budget_s, real = 3, lead.run_lead
        started = time.monotonic()
        with patch.object(scout_process, "run_lead",
                          lambda conn, project, row, worktree, _budget_s:
                          real(conn, project, row, worktree, budget_s)):
            code, printed = self.run_scout(path, scout_id)
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, printed)
        plan_turn, report_turn = self.turn_events(conn, scout_id)
        self.assertIs(plan_turn["timed_out"], True)
        self.assertIs(report_turn["timed_out"], False)
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while running(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(running(pid))
        self.assertEqual(self.report(conn, scout_id), [dict(UNSHOWN, cut=[])])
        self.assertEqual(scout(conn, scout_id).phase, "done")
        self.assertLess(elapsed, 30)

    def test_a_task_with_an_empty_question_stores_no_plan_but_a_report(self):
        path, conn, scout_id = self.bug_scout()
        empty = {"tasks": [dict(PLAN["tasks"][0], question="")]}
        self.stubs.answer(plan=fenced(PLAN) + fenced(empty),
                          report=fenced(REPORT))
        code, printed = self.run_scout(path, scout_id)
        self.assertEqual(code, 0, printed)
        (summary,) = [summary for (summary,) in conn.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_plan'", (scout_id,))]
        self.assertIn("question", summary)
        self.assertEqual(self.tasks(conn, scout_id), [])
        self.assertEqual(len(self.stubs.turns("report")), 1)
        self.assertEqual(self.report(conn, scout_id), [dict(UNSHOWN, cut=[])])

    def test_a_report_turn_with_no_json_block_ends_the_scout_failed(self):
        path, conn, scout_id = self.bug_scout()
        self.stubs.answer(plan=fenced(PLAN), report="I found the loop.")
        code, printed = self.run_scout(path, scout_id)
        self.assertEqual(code, 1, printed)
        row = scout(conn, scout_id)
        self.assertEqual((row.phase, row.reason),
                         ("failed", "the lead's report turn returned no JSON object"))
        self.assertEqual(self.report(conn, scout_id), [])

    def test_a_lead_route_that_fails_its_probe_runs_no_turn(self):
        path, conn, scout_id = self.bug_scout()
        self.stubs.answer(probe="not today", plan=fenced(PLAN),
                          report=fenced(REPORT))
        code, printed = self.run_scout(path, scout_id)
        self.assertEqual(code, 1, printed)
        row = scout(conn, scout_id)
        self.assertEqual(row.phase, "failed")
        self.assertRegex(row.reason, r"^lead route opus is down: \S")
        self.assertEqual(len(self.stubs.turns("probe")), 1)
        self.assertEqual(self.stubs.turns("plan"), [])
        self.assertEqual(self.stubs.turns("report"), [])

    def test_each_lead_turn_is_recorded_and_no_argv_carries_a_secret(self):
        path, conn, scout_id = self.bug_scout(
            CLAUDE_LEAD + f'[linear]\napi_key = "{SECRET}"\n',
            ask=f"why does the retry spin with key {SECRET}?")
        code, printed = self.run_scout(path, scout_id)
        self.assertEqual(code, 0, printed)
        turns = self.turn_events(conn, scout_id)
        self.assertEqual([turn["role"] for turn in turns],
                         ["scout-lead", "scout-helper", "scout-helper",
                          "scout-lead", "scout-lead"])
        for turn in turns:
            self.assertLessEqual({"label", "route", "exit_status",
                                  "timed_out", "seconds"}, set(turn))
        calls = self.stubs.calls()
        self.assertEqual([call["turn"] for call in calls],
                         ["probe", "plan", "probe", "helper", "helper",
                          "synthesis", "report"])
        self.assertFalse([arg for call in calls for arg in call["argv"]
                          if SECRET in arg])


class AnswerReaderTests(unittest.TestCase):
    def test_the_last_json_block_wins_over_the_echoed_goal_shape(self):
        echoed = lead.PLAN_SHAPE
        output = (f"user\n```json\n{echoed}\n```\ncodex\n{fenced(PLAN)}"
                  "tokens used\n1,234\n")
        self.assertEqual(lead.fenced_json(output), PLAN)
        self.assertIsNone(lead.fenced_json(f"user\n```json\n{echoed}\n```\n"))

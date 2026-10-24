"""A scout's prior reports: the earlier done scouts of its project whose
reports or sub-tasks named the paths its ask, links or context pack name,
shown in the lead's plan goal with what changed since their base, and the
lead's `prior` judgements checked and served, run by
`factory.py --scout ID PROJECT` against stub lead CLIs over a real git
repository with two commits.

Run: python3 -m unittest discover -s tests -p 'test_scout_prior.py' -v
"""
import re
import unittest

from holophyte.config.project import Project
from holophyte.scout.report import report_problems
from store.scouts import (
    create_scout,
    end_scout,
    move_scout,
    record_scout_report,
    record_scout_tasks,
)
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_lead import CLAUDE_LEAD
from tests.test_scout_process import ScoutFixture
from tests.test_serve_board import ServeBoardCase

EMPTY = {"summary": "nothing new", "findings": [], "actions": []}
JUDGED = [{"scout": 1, "finding": 1, "verdict": "holds",
           "why": "still unchecked at line 3"}]
SECTION = "## Prior reports\n\n"
AT = 1_750_000_000_000


def finding(title, path, line):
    return {"title": title, "status": "shown", "evidence": [
        {"file": path, "line": line, "verified": True}]}


class PriorCase(ScoutFixture):
    start_host = ServeBoardCase.start_host
    request = ServeBoardCase.request

    def setUp(self):
        super().setUp()
        self.path = self.repo("alpha")
        Project.locate(self.path, adopt=False).config_path.write_text(
            '[board]\nkind = "native"\nprefix = "ALPHA"\n' + CLAUDE_LEAD)
        git(self.path, "checkout", "-q", "-b", "main")
        (self.path / "app").mkdir()
        (self.path / "app/save.py").write_text("a\nb\nc\n")
        (self.path / "app/form.py").write_text("form\n")
        git(self.path, "add", "app")
        git(self.path, "commit", "-q", "-m", "first")
        self.first = git(self.path, "rev-parse", "HEAD")
        (self.path / "app/save.py").write_text("a\nb\nc\nd\n")
        git(self.path, "commit", "-q", "-am", "second")
        code, printed = self.cli("project", "add", str(self.path))
        self.assertIsNone(code, printed)
        self.db = self.conn(self.path)
        (self.project_id,) = self.db.execute(
            "SELECT id FROM projects").fetchone()
        self.reported = 0

    def earlier(self, findings, *, task_paths=(), phase="done", base=None):
        scout_id = create_scout(self.db, self.project_id, f"ask {len(findings)}",
                                "bug", 30)
        move_scout(self.db, scout_id, "planning", base_sha=base or self.first)
        if task_paths:
            record_scout_tasks(self.db, scout_id, 1, 1, [
                {"question": "where?", "paths": list(task_paths),
                 "mode": "read"}])
        self.reported += 1
        record_scout_report(self.db, scout_id, 1, {
            "summary": f"summary of scout {scout_id}", "findings": findings,
            "actions": []}, now=AT + self.reported * 1000)
        end_scout(self.db, scout_id, phase, "ended")
        return scout_id

    def save_and_form(self):
        self.earlier([finding("save drops the title", "app/save.py", 3),
                      finding("save never logs", "app/save.py", 2)])
        self.earlier([finding("form loses focus", "app/form.py", 1)])

    def run_scout(self, ask, report=EMPTY):
        self.stubs.answer(plan=fenced({"tasks": []}), report=fenced(report))
        scout_id = create_scout(self.db, self.project_id, ask, "research", 30)
        move_scout(self.db, scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main"))
        code, printed = self.cli("--scout", str(scout_id), str(self.path))
        self.assertEqual(code, 0, printed)
        return scout_id

    def plan_goal(self):
        return self.stubs.turns("plan")[-1]["argv"][-1]

    def listed(self, goal):
        return [int(n) for n in re.findall(r"(?m)^Scout (\d+) \(", goal)]

    def entry(self, goal, scout_id):
        return goal.split(f"Scout {scout_id} (", 1)[1].split("\n\n", 1)[0]

    def events(self, scout_id):
        return self.db.execute("SELECT kind, summary FROM runEvents"
                               " WHERE scoutId = ? ORDER BY seq",
                               (scout_id,)).fetchall()


class PlanGoalTests(PriorCase):
    def test_a_report_on_the_asked_path_is_shown_with_what_changed(self):
        self.save_and_form()
        self.run_scout("Why does app/save.py drop the title?")
        goal = self.plan_goal()
        self.assertEqual(self.listed(goal), [1])
        entry = self.entry(goal, 1)
        self.assertIn(f"base {self.first[:12]},", entry)
        self.assertIn("1. [shown] save drops the title at app/save.py:3", entry)
        self.assertRegex(entry, r"(?m)^Changed since its base: app/save\.py$")
        self.assertNotIn("Scout 2 (", goal)

    def test_a_report_on_an_unchanged_path_says_none_changed(self):
        self.save_and_form()
        self.run_scout("Why does app/form.py lose focus?")
        goal = self.plan_goal()
        self.assertEqual(self.listed(goal), [2])
        self.assertIn("Changed since its base: none of its paths changed",
                      self.entry(goal, 2))

    def test_a_directory_ask_and_a_task_path_each_match(self):
        cited = self.earlier([finding("save drops the title", "app/save.py",
                                      3)])
        self.run_scout("What does app/ hold?")
        self.assertEqual(self.listed(self.plan_goal()), [cited])
        tasked = self.earlier([], task_paths=["app/form.py"])
        self.run_scout("Why does app/form.py lose focus?")
        self.assertEqual(self.listed(self.plan_goal()), [tasked])

    def test_only_the_three_newest_done_scouts_are_listed_newest_first(self):
        cites = [finding("save drops the title", "app/save.py", 3)]
        done = [self.earlier(cites) for _ in range(5)]
        self.earlier(cites, phase="failed")
        self.earlier(cites, phase="stopped")
        self.run_scout("Why does app/save.py drop the title?")
        self.assertEqual(self.listed(self.plan_goal()), done[:1:-1])

    def test_a_base_missing_from_the_repository_is_named_and_the_plan_runs(self):
        gone = "f00d" * 10
        self.earlier([finding("save drops the title", "app/save.py", 3)],
                     base=gone)
        scout_id = self.run_scout("Why does app/save.py drop the title?")
        self.assertIn(f"its base {gone[:12]} is not in this repository",
                      self.entry(self.plan_goal(), 1))
        self.assertIn(("scout_prior", "prior reports: scouts 1"),
                      self.events(scout_id))


class PriorEventTests(PriorCase):
    def test_the_prior_event_precedes_the_plan_turn(self):
        self.save_and_form()
        scout_id = self.run_scout("Why does app/save.py drop the title?")
        events = self.events(scout_id)
        kinds = [kind for kind, _ in events]
        self.assertIn(("scout_prior", "prior reports: scouts 1"), events)
        self.assertLess(kinds.index("scout_prior"), kinds.index("agent_turn"))

    def test_an_ask_naming_no_path_gets_no_section_and_no_event(self):
        self.save_and_form()
        scout_id = self.run_scout("Why does saving drop the title? e.g. "
                                  "app/gone.py")
        self.assertNotIn(SECTION, self.plan_goal())
        self.assertNotIn("scout_prior",
                         [kind for kind, _ in self.events(scout_id)])

    def test_paths_that_match_no_report_get_the_none_event(self):
        scout_id = self.run_scout("Why does app/form.py lose focus?")
        self.assertNotIn(SECTION, self.plan_goal())
        self.assertIn(("scout_prior", "prior reports: none"),
                      self.events(scout_id))


class PriorJudgementTests(PriorCase):
    def test_a_judged_report_passes_and_is_served_and_an_unjudged_one_is_null(
            self):
        self.save_and_form()
        scout_id = self.run_scout("Why does app/save.py drop the title?",
                                  dict(EMPTY, prior=JUDGED))
        self.start_host()
        for served, prior in ((scout_id, JUDGED), (1, None)):
            status, answer, _ = self.request(
                "GET", f"/projects/alpha/scouts/{served}", revision=None)
            self.assertEqual(status, 200, answer)
            self.assertEqual(answer["scout"]["phase"], "done")
            self.assertEqual(answer["reports"][0]["report"]["prior"], prior)


class PriorCheckTests(unittest.TestCase):
    def test_each_judgement_problem_names_its_field(self):
        cases = (({"scout": 2}, "prior[0].scout:"),
                 ({"finding": 3}, "prior[0].finding:"),
                 ({"verdict": "maybe"}, "prior[0].verdict:"),
                 ({"why": " "}, "prior[0].why:"))
        for change, field in cases:
            with self.subTest(field=field):
                problems = report_problems(
                    dict(EMPTY, prior=[dict(JUDGED[0], **change)]), "research",
                    offered={1: 2})
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(field), problems)


if __name__ == "__main__":
    unittest.main()

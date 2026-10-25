"""A scout report's shape checked field by field, its file evidence resolved
against the base commit in a real git repository, and the scout process's
repair turn for a malformed report.

Run: python3 -m unittest discover -s tests -p 'test_scout_report.py' -v
"""
import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ticket_template
from holophyte.scout.follow_up import pass_plan_goal
from holophyte.scout.lead import (
    fenced_json,
    plan_goal,
    repair_goal,
    report_goal,
    synthesis_goal,
)
from holophyte.scout.report import (
    FIELDS,
    REPRODUCTION,
    Rule,
    report_problems,
    resolve_evidence,
)
from store.scouts import create_scout, move_scout, scout
from tests.host_fixture import git
from tests.scout_stubs import BRIEF, fenced
from tests.test_scout_lead import CLAUDE_LEAD
from tests.test_scout_process import ScoutFixture

BUG = {
    **BRIEF,
    "summary": "the retry loop never backs off",
    "findings": [{"title": "no backoff", "status": "confirmed", "evidence": [
        {"file": "notes.txt", "line": 1},
        {"command": "python3 -m unittest", "output": "FAILED"},
        {"url": "https://example.test/retry"}]}],
    "actions": [
        {"title": "add backoff", "why": "the loop spins", "kind": "fix",
         "body": "sleep between attempts"},
        {"title": "pin the spin", "why": "no test fails today",
         "kind": "test", "body": "a test that counts attempts"}],
    "reproduction": {"level": "not_reproduced", "hypotheses": [
        {"hypothesis": "no sleep", "separates": "attempt timestamps",
         "verdict": "open"}]},
    "extra": {"kept": True},
}
RESEARCH = {key: value for key, value in BUG.items() if key != "reproduction"}


def changed(path, value):
    """BUG with the field at `path`, a tuple of keys and indexes, set to
    `value`, or removed when `value` is None."""
    report = copy.deepcopy(BUG)
    *parents, last = path
    holder = report
    for key in parents:
        holder = holder[key]
    if value is None:
        del holder[last]
    else:
        holder[last] = value
    return report


def enumerated(rule):
    if isinstance(rule, Rule):
        return list(rule.choices)
    parts = rule.values() if isinstance(rule, dict) else rule
    if isinstance(parts, str) or not hasattr(parts, "__iter__"):
        return []
    return [choice for part in parts for choice in enumerated(part)]


def starts(problems):
    return [problem.split(": ", 1)[0] for problem in problems]


class ReportProblemsTests(unittest.TestCase):
    def test_a_full_bug_report_and_a_research_report_pass(self):
        self.assertEqual(report_problems(BUG, "bug"), [])
        self.assertEqual(report_problems(RESEARCH, "research"), [])

    def test_a_missing_findings_list_and_an_empty_summary_are_named(self):
        self.assertIn("findings", starts(report_problems(
            changed(("findings",), None), "bug")))
        self.assertIn("summary", starts(report_problems(
            changed(("summary",), ""), "bug")))

    def test_a_bad_evidence_item_is_named_by_its_path(self):
        for item, field in (({"file": "notes.txt", "line": 0},
                             "findings[0].evidence[0].line"),
                            ({"file": "notes.txt", "line": "88"},
                             "findings[0].evidence[0].line"),
                            ({"note": "trust me"}, "findings[0].evidence[0]")):
            with self.subTest(item=item):
                report = changed(("findings", 0, "evidence"), [item])
                self.assertEqual(starts(report_problems(report, "bug")),
                                 [field])

    def test_a_finding_status_is_one_of_the_three_words(self):
        report = copy.deepcopy(BUG)
        report["findings"] = [{**BUG["findings"][0], "status": status}
                              for status in ("confirmed", "partial",
                                             "unconfirmed")]
        self.assertEqual(report_problems(report, "bug"), [])
        for word in ("shown", "likely"):
            with self.subTest(status=word):
                report["findings"][0]["status"] = word
                problems = report_problems(report, "bug")
                self.assertEqual(starts(problems), ["findings[0].status"])
                for status in ("confirmed", "partial", "unconfirmed"):
                    self.assertIn(status, problems[0])

    def test_a_bad_action_kind_and_an_empty_body_are_named(self):
        self.assertEqual(starts(report_problems(
            changed(("actions", 0, "kind"), "refactor"), "bug")),
            ["actions[0].kind"])
        self.assertEqual(starts(report_problems(
            changed(("actions", 0, "body"), ""), "bug")),
            ["actions[0].body"])

    def test_the_title_headline_and_bullets_are_bounded_one_line_texts(self):
        bullets = BRIEF["bullets"]
        for path, value, field in (
                (("title",), "t" * 71, "title"),
                (("headline",), "h" * 121, "headline"),
                (("headline",), "Not reproduced:\nrun alone", "headline"),
                (("bullets",), bullets[:2], "bullets"),
                (("bullets",), bullets * 2, "bullets"),
                (("bullets", 2), "b" * 121, "bullets[2]")):
            with self.subTest(field=field, value=value):
                self.assertEqual(starts(report_problems(
                    changed(path, value), "bug")), [field])
        at_limits = changed(("title",), "t" * 70)
        at_limits.update(headline="h" * 120, bullets=["b" * 120] * 5)
        self.assertEqual(report_problems(at_limits, "bug"), [])

    def test_a_report_missing_its_title_headline_or_bullets_names_it(self):
        for field in ("title", "headline", "bullets"):
            with self.subTest(field=field):
                self.assertEqual(starts(report_problems(
                    changed((field,), None), "bug")), [field])

    def test_a_bug_report_needs_a_reproduction_in_its_shape(self):
        for path, value, field in (
                (("reproduction",), None, "reproduction"),
                (("reproduction", "level"), "browser", "reproduction.level"),
                (("reproduction", "hypotheses"), [],
                 "reproduction.hypotheses"),
                (("reproduction", "hypotheses", 0, "verdict"), "maybe",
                 "reproduction.hypotheses[0].verdict")):
            with self.subTest(field=field):
                self.assertEqual(starts(report_problems(
                    changed(path, value), "bug")), [field])


class ReportGoalTests(unittest.TestCase):
    def test_the_report_goal_names_every_value_the_check_allows(self):
        choices = enumerated([FIELDS, REPRODUCTION])
        self.assertEqual(sorted(choices), sorted([
            "confirmed", "partial", "unconfirmed", "fix", "test", "investigate",
            "decide", "revise", "guardrail", "test", "real_tool",
            "not_reproduced", "supported", "ruled_out", "open"]))
        goal = report_goal(SimpleNamespace(kind="bug", subject_kind="defect",
                                           subject_ref="12"),
                           SimpleNamespace(tasks=[]), None, False)
        self.assertEqual([choice for choice in choices
                          if f'"{choice}"' not in goal], ["guardrail"])
        self.assertNotIn('"shown"', goal)
        self.assertNotIn('"likely"', goal)
        for form in ('{"file": PATH, "line": N}',
                     '{"command": TEXT, "output": TEXT}', '{"url": URL}'):
            self.assertIn(form, goal)

    def test_each_plan_goal_shows_a_titled_task_and_each_report_goal_the_brief(self):
        row = SimpleNamespace(kind="recon", subject_kind=None,
                              subject_ref=None, ask="Map the merge gate",
                              links=[], base_sha="a" * 40)
        swarm = SimpleNamespace(tasks=[], total=4, launched=2)
        plans = {"plan": plan_goal(row, 4, ""),
                 "synthesis": synthesis_goal(row, swarm, True, None, ""),
                 "pass plan": pass_plan_goal(row, "look further", "", 2, [],
                                             [], 3, True)}
        for name, goal in plans.items():
            with self.subTest(goal=name):
                shape = goal[goal.index("```json"):]
                self.assertIn('{"title": TEXT, "question": TEXT', shape)
                self.assertIn("at most 8 words and 60 characters", goal)
        reports = {"report": report_goal(row, swarm, None, True),
                   "repair": repair_goal(row, ["title: must be"], True)}
        for name, goal in reports.items():
            with self.subTest(goal=name):
                shape = goal[goal.index("```json"):]
                for field in ('"title": TEXT', '"headline": TEXT',
                              '"bullets": [TEXT, ...]'):
                    self.assertIn(field, shape)
                prose = goal[:goal.index("```json")]
                for limit in ("at most 70 characters", "at most 120",
                              "3 to 5 lines of at most 120 characters"):
                    self.assertIn(limit, prose)

    def goals(self):
        for kind in ("bug", "recon"):
            row = SimpleNamespace(kind=kind, subject_kind=None,
                                  subject_ref=None)
            yield kind, "report", report_goal(row, SimpleNamespace(tasks=[]),
                                              None, False)
            yield kind, "repair", repair_goal(row, ["summary: must be"
                                                    " non-empty text"], False)

    def test_each_goal_shows_a_ticket_skeleton_in_template_order_before_its_shape(self):
        structural = ("missing H1", "missing section",
                      "unknown section", "duplicate section",
                      "sections out of template order")
        for kind, name, goal in self.goals():
            with self.subTest(kind=kind, goal=name):
                shape = goal.index("```json")
                skeleton = goal[goal.index("+++\n"):shape]
                ticket = ticket_template.parse(skeleton)
                self.assertEqual(ticket.header_problems, [])
                self.assertEqual([problem for problem in
                                  ticket_template.validate(ticket)
                                  if problem.startswith(structural)
                                  or "header" in problem], [])
                self.assertEqual(ticket.order, [
                    section for section in ticket_template.SECTION_ORDER
                    if section != ticket_template.ESTIMATE])
                report = BUG if kind == "bug" else RESEARCH
                self.assertEqual(fenced_json(goal + fenced(report)), report)


class ResolveEvidenceTests(unittest.TestCase):
    def test_each_file_item_is_resolved_against_the_base_commit(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "tracked").write_text("one\ntwo\nthree")
            git(repo, "add", "tracked")
            git(repo, "commit", "-q", "-m", "base")
            base = git(repo, "rev-parse", "HEAD")
            (repo / "added").write_text("new\n")
            git(repo, "add", "added")
            git(repo, "commit", "-q", "-m", "later")
            cited = [{"file": "tracked", "line": 3},
                     {"file": "tracked", "line": 4},
                     {"file": "added", "line": 1},
                     {"file": "../repo/tracked", "line": 1},
                     {"file": "tracked", "line": 2, "why": "a lead's own"},
                     {"file": "tracked\x00", "line": 1}]
            report = changed(("findings", 0, "evidence"), cited)
            resolved = resolve_evidence(repo, base, report)
        (verified, past, absent, outside, stale,
         nul) = resolved["findings"][0]["evidence"]
        self.assertEqual(verified, {"file": "tracked", "line": 3,
                                    "verified": True})
        self.assertEqual((past["verified"], past["why"]),
                         (False, "past the end of the file (3 lines)"))
        self.assertIs(absent["verified"], False)
        self.assertTrue(absent["why"].startswith("not in the base commit"))
        self.assertEqual((outside["verified"], outside["why"]),
                         (False, "outside the repository"))
        self.assertEqual(stale, {"file": "tracked", "line": 2,
                                 "verified": True})
        self.assertIs(nul["verified"], False)
        self.assertTrue(nul["why"].startswith("not in the base commit"))
        self.assertEqual(report["findings"][0]["evidence"], cited)


class ReportRepairTests(ScoutFixture):
    def run_bug_scout(self, report, repair):
        path = self.project("alpha", CLAUDE_LEAD)
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "why does the retry spin?",
                                "bug", 30)
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        self.stubs.answer(plan=fenced({"tasks": []}), report=fenced(report),
                          repair=fenced(repair))
        self.cli("--scout", str(scout_id), str(path))
        return conn, scout_id

    def stored(self, conn, scout_id):
        reports = [json.loads(report) for (report,) in conn.execute(
            "SELECT report FROM scoutReports WHERE scoutId = ?", (scout_id,))]
        actions = [(position, json.loads(item)) for position, item in
                   conn.execute("SELECT position, item FROM scoutActions"
                                " WHERE scoutId = ? ORDER BY position",
                                (scout_id,))]
        return reports, actions

    def test_a_report_missing_its_summary_is_repaired_in_the_lead_session(self):
        conn, scout_id = self.run_bug_scout(changed(("summary",), None), BUG)
        (repair,) = self.stubs.turns("repair")
        session = scout(conn, scout_id).lead_session
        self.assertEqual(repair["argv"][repair["argv"].index("--resume") + 1],
                         session)
        self.assertTrue(repair["argv"][-1].startswith(
            "Scout report repair turn"))
        self.assertIn("summary:", repair["argv"][-1])
        (report,), actions = self.stored(conn, scout_id)
        file_item = report["findings"][0]["evidence"][0]
        self.assertEqual((file_item["file"], file_item["verified"]),
                         ("notes.txt", True))
        self.assertEqual(actions, list(enumerate(BUG["actions"], 1)))
        self.assertEqual(scout(conn, scout_id).phase, "done")

    def test_a_report_still_malformed_after_its_repair_fails_the_scout(self):
        missing = changed(("summary",), None)
        conn, scout_id = self.run_bug_scout(missing, missing)
        self.assertEqual(len(self.stubs.turns("repair")), 1)
        row = scout(conn, scout_id)
        self.assertEqual(row.phase, "failed")
        self.assertTrue(row.reason.startswith(
            "the lead's report is malformed: summary:"), row.reason)
        self.assertEqual(self.stored(conn, scout_id), ([], []))

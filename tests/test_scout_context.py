"""A scout's context pack: its subject's run, defect, ticket or pull request
read from the store and the repository into the lead's plan goal, run by
`factory.py --scout ID PROJECT` against stub lead CLIs over a real git
repository.

Run: python3 -m unittest discover -s tests -p 'test_scout_context.py' -v
"""
import json
import unittest

import store
import store.tickets
from holophyte.config.project import Project
from holophyte.scout.context import context_pack
from store.scouts import create_scout, move_scout, scout
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_lead import CLAUDE_LEAD, CODEX_LEAD, PLAN, REPORT, SECRET
from tests.test_scout_process import ScoutFixture

CUT_LINE = "(context cut at 24000 characters)"
SECTION = "## Context\n\n"
PR = "https://github.com/acme/demo/pull/7"
VERIFY = "python3 -m unittest tests.test_save"
FINDING = {"path": "app/save.py", "line": 88, "severity": "p1",
           "message": "save drops the title"}


class ContextCase(ScoutFixture):
    def setUp(self):
        super().setUp()
        self.stubs.answer(plan=fenced(PLAN), report=fenced(REPORT))

    def start(self, config=CLAUDE_LEAD):
        self.path = self.project("alpha", config)
        self.db = self.conn(self.path)
        (self.project_id,) = self.db.execute(
            "SELECT id FROM projects").fetchone()

    def mirror(self, key, body="", title=None):
        return store.tickets.mirror_ticket(
            self.db, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=title or f"ticket {key}",
            acceptance_criteria=[f"Given {key}, it works"],
            verification_commands=["echo ok"], board_column="ready",
            body=body)

    def failed_run(self, ticket, ledger=(), stop=None, files=()):
        (status,) = self.db.execute("SELECT status FROM tickets WHERE id = ?",
                                    (ticket,)).fetchone()
        if status != "in_flight":
            store.tickets.transition(self.db, ticket, "in_flight")
        run = store.claim(self.db, self.project_id, ticket)
        if files:
            self.branch(run, *files)
        for text in ledger:
            store.record_ledger(self.db, run, "note", text)
        if stop is not None:
            store.record_event(self.db, run, "contradiction", stop,
                               level="detail", payload=json.dumps(
                                   {"criteria": [{"id": "C2"}, {"id": "C4"}]}))
        store.release(self.db, run, "failed", reason="verify went red")
        return run

    def red_round(self, run, output):
        store.record_review_round(
            self.db, run, 1, "changes_requested", "reviewer",
            findings=[FINDING], verification_results=[
                {"source": "ticket", "command": VERIFY, "exitCode": 1,
                 "output": output}])

    def branch(self, run, *paths):
        name = f"task/run-{run}"
        git(self.path, "checkout", "-q", "-b", name)
        for path in paths:
            (self.path / path).parent.mkdir(parents=True, exist_ok=True)
            (self.path / path).write_text(f"{path}\n")
            git(self.path, "add", path)
        git(self.path, "commit", "-q", "-m", "change")
        git(self.path, "checkout", "-q", "main")
        store.set_branch(self.db, run, name)

    def a_scout(self, subject=None):
        scout_id = create_scout(self.db, self.project_id, "why did it fail?",
                                "bug", 30, subject=subject)
        move_scout(self.db, scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main"))
        return scout_id

    def run_scout(self, subject=None):
        scout_id = self.a_scout(subject)
        code, printed = self.cli("--scout", str(scout_id), str(self.path))
        self.assertEqual(code, 0, printed)
        return scout_id

    def plan_goal(self):
        (plan,) = self.stubs.turns("plan")
        return plan["argv"][-1]

    def section(self, goal):
        return goal.split(SECTION, 1)[1].split("\n\nPlan the work", 1)[0]

    def pack(self, subject):
        return context_pack(Project.locate(self.path, adopt=False),
                            scout(self.db, self.a_scout(subject)))

    def events(self, scout_id):
        return self.db.execute(
            "SELECT seq, kind, summary, payload FROM runEvents"
            " WHERE scoutId = ? ORDER BY seq", (scout_id,)).fetchall()


class RunContextTests(ContextCase):
    def test_a_run_scouts_plan_goal_holds_its_ledger_verify_tail_and_findings(self):
        self.start()
        ticket = self.mirror("NAT-3")
        run = self.failed_run(ticket, ledger=["the save handler was rewritten"])
        self.red_round(run, "Traceback\nAssertionError\nMARKER-TAIL")
        self.run_scout(("run", str(run)))
        goal = self.plan_goal()
        self.assertRegex(goal, rf"(?m)^Context: run {run} of NAT-3\b")
        for text in ("the save handler was rewritten", VERIFY, "MARKER-TAIL",
                     "app/save.py:88 [p1] save drops the title"):
            self.assertIn(text, goal)

    def test_the_pack_keeps_the_newest_20_ledger_entries_and_the_output_tail(self):
        self.start()
        run = self.failed_run(self.mirror("NAT-3"), ledger=[
            f"ledger entry #{n:02d}" for n in range(1, 32)])
        self.red_round(run, "MARKER-HEAD\n" + "x" * 4_976 + "\nMARKER-TAIL")
        text = self.pack(("run", str(run))).text
        self.assertIn("Ledger (20 of 31):\n", text)
        for n in range(12, 32):
            self.assertIn(f"ledger entry #{n:02d}", text)
        for n in range(1, 12):
            self.assertNotIn(f"ledger entry #{n:02d}", text)
        self.assertIn("MARKER-TAIL", text)
        self.assertNotIn("MARKER-HEAD", text)

    def test_the_context_event_precedes_the_plan_turn_and_names_the_files(self):
        self.start()
        run = self.failed_run(self.mirror("NAT-3"), ledger=["entry"],
                              files=("app/save.py", "app/form.py"))
        scout_id = self.run_scout(("run", str(run)))
        kinds = [kind for _, kind, _, _ in self.events(scout_id)]
        self.assertEqual(kinds.count("scout_context"), 1)
        self.assertLess(kinds.index("scout_context"), kinds.index("agent_turn"))
        (_, _, summary, payload), = [e for e in self.events(scout_id)
                                     if e[1] == "scout_context"]
        for part in (f"run {run}", "ledger", "2 files"):
            self.assertIn(part, summary)
        self.assertEqual(sorted(json.loads(payload)["paths"]),
                         ["app/form.py", "app/save.py"])

    def test_a_deleted_branch_leaves_the_files_out_and_the_plan_still_runs(self):
        self.start()
        run = self.failed_run(self.mirror("NAT-3"), ledger=["entry"],
                              files=("app/save.py", "app/form.py"))
        git(self.path, "branch", "-q", "-D", f"task/run-{run}")
        scout_id = self.run_scout(("run", str(run)))
        (summary,) = [s for _, kind, s, _ in self.events(scout_id)
                      if kind == "scout_context"]
        self.assertIn("files unreadable", summary)
        self.assertNotIn("app/form.py", self.plan_goal())
        self.assertEqual(len(self.stubs.turns("plan")), 1)

    def test_a_secret_in_the_runs_ledger_reaches_no_lead_argv(self):
        self.start(CLAUDE_LEAD + f'[linear]\napi_key = "{SECRET}"\n')
        run = self.failed_run(self.mirror("NAT-3"),
                              ledger=[f"the token {SECRET} was rejected"])
        (stored,) = self.db.execute("SELECT text FROM ledger WHERE runId = ?",
                                    (run,)).fetchone()
        self.assertIn(SECRET, stored)
        self.run_scout(("run", str(run)))
        self.assertIn("the token", self.plan_goal())
        for call in self.stubs.calls():
            self.assertFalse(any(SECRET in arg for arg in call["argv"]))

    def test_a_fresh_report_turn_carries_the_context_too(self):
        self.start(CODEX_LEAD)
        run = self.failed_run(self.mirror("NAT-3"), ledger=["entry"])
        self.run_scout(("run", str(run)))
        (report,) = self.stubs.turns("report")
        self.assertIn(f"Context: run {run} of NAT-3", report["argv"][-1])


class SubjectContextTests(ContextCase):
    def test_a_defect_scout_reads_the_stop_and_both_ticket_bodies(self):
        self.start()
        ticket = self.mirror("NAT-3", body="Revision one: export the rows.")
        run = self.failed_run(ticket, stop="C2 contradicts C4")
        self.mirror("NAT-3", body="Revision two: export rows with a header.")
        self.run_scout(("defect", str(run)))
        goal = self.plan_goal()
        self.assertIn("Ticket defect: contradiction on C2, C4", goal)
        self.assertIn("Revision one: export the rows.", goal)
        self.assertRegex(goal, r"revision 2:\nRevision two: export rows with")

    def test_a_ticket_scout_lists_its_runs_and_packs_only_the_newer(self):
        self.start()
        ticket = self.mirror("NAT-3", body="The form keeps its title.",
                             title="Saving keeps the title")
        first = self.failed_run(ticket, ledger=["first attempt alpha"])
        second = self.failed_run(ticket, ledger=["second attempt beta"])
        self.run_scout(("ticket", "NAT-3"))
        goal = self.plan_goal()
        for text in ("Saving keeps the title", "The form keeps its title.",
                     f"run {first}: failed", f"run {second}: failed",
                     "second attempt beta"):
            self.assertIn(text, goal)
        self.assertNotIn("first attempt alpha", goal)

    def test_a_pr_scout_names_the_url_and_packs_the_run_that_opened_it(self):
        self.start()
        run = self.failed_run(self.mirror("NAT-3"))
        store.writes.set_pull_request(self.db, run, PR)
        self.run_scout(("pr", PR))
        goal = self.plan_goal()
        self.assertIn(PR, goal)
        self.assertIn(f"Context: run {run} of", goal)

    def test_a_long_ticket_body_is_cut_to_the_cap(self):
        self.start()
        self.mirror("NAT-3", body="body line\n" * 3_000)
        self.run_scout(("ticket", "NAT-3"))
        section = self.section(self.plan_goal())
        self.assertLessEqual(len(section), 24_000)
        self.assertTrue(section.endswith(f"\n{CUT_LINE}"))

    def redacted_body_section(self, secret):
        self.start(CLAUDE_LEAD + f'[linear]\napi_key = "{secret}"\n')
        body = f"{secret}\n" * (30_000 // (len(secret) + 1))
        self.mirror("NAT-3", body=body + "x" * (30_000 - len(body)))
        self.run_scout(("ticket", "NAT-3"))
        section = self.section(self.plan_goal())
        self.assertNotIn(f"\n{secret}\n", section)
        self.assertLessEqual(len(section), 24_000)
        self.assertTrue(section.endswith(f"\n{CUT_LINE}"))

    def test_a_body_whose_secrets_redact_longer_is_still_cut_to_the_cap(self):
        self.redacted_body_section("shortkey")

    def test_a_secret_inside_the_redaction_mark_is_still_cut_to_the_cap(self):
        self.redacted_body_section("red")

    def test_a_body_whose_secrets_redact_shorter_is_still_cut_to_the_cap(self):
        self.redacted_body_section("sk-long-scout-secret-0123456789abcdef")

    def test_a_scout_with_no_subject_gets_no_context(self):
        self.start()
        scout_id = self.run_scout()
        self.assertNotIn(SECTION, self.plan_goal())
        self.assertNotIn("scout_context",
                         [kind for _, kind, _, _ in self.events(scout_id)])


if __name__ == "__main__":
    unittest.main()

"""A near-miss witness citation resolves only to the one test it means, and an
approval citation stands while the cited test is unchanged.

Run: python3 -m unittest discover -s tests -p 'test_witness_citations.py' -v
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import holophyte.loop.runs
import store
import store.tickets
from holophyte.review import citations
from holophyte.review.reply_parsing import criteria_findings, criteria_statuses

CRITERIA = ["Given a reader, then it reads"]
CITED = "tests/test_alpha.py::Wrong::test_reads"
REPLY = f"CRITERION 1: met — {CITED}\nVERDICT: APPROVE"
OTHER_CLASS = [{"cited": CITED, "as": "tests/test_alpha.py::Reads::test_reads",
                "rule": "other class"}]
UNIMPORTABLE = 'raise ImportError("no")\n'


def alpha(*classes, prelude=""):
    body = "".join(f"class {name}(unittest.TestCase):\n{methods}\n\n"
                   for name, methods in classes)
    return f"import unittest\n{prelude}\n\n{body}"


READS = "    def test_reads(self):\n        pass\n"
OTHER = "    def test_other(self):\n        pass\n"


class Worktree(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def status(self, reply=REPLY):
        (entry,) = criteria_statuses(reply, CRITERIA, self.root)
        return entry


class OtherClassTests(Worktree):
    def test_record_round_resolves_a_unique_other_class_and_passes(self):
        self.write("tests/test_alpha.py", alpha(("Reads", READS)))
        conn = store.open(str(self.root / "store.db"))
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team-1", self.root)
        ticket = store.tickets.mirror_ticket(
            conn, project, linear_issue_id="issue-1", linear_identifier="KO-1",
            title="ticket 1", acceptance_criteria=CRITERIA,
            verification_commands=["echo ok"], time_box_ms=60_000)
        store.tickets.transition(conn, ticket, "in_flight")
        run_id = store.claim(conn, project, ticket)
        holophyte.loop.runs.record_round(
            None, conn, run_id, 1, "review", REPLY, "echo ok", True, "ok",
            criteria=CRITERIA, root=self.root, route="reviewer")
        ((verdict,),) = conn.execute(
            "SELECT verdict FROM reviewRounds WHERE runId = ?", (run_id,))
        ((payload,),) = conn.execute(
            "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'criteria'",
            (run_id,))
        (entry,) = json.loads(payload)["criteria"]
        self.assertEqual(verdict, "pass")
        self.assertEqual((entry["status"], entry["note"], entry["resolved"]),
                         ("met", CITED, OTHER_CLASS))

    def test_unimportable_module_resolves_by_its_source(self):
        self.write("tests/test_alpha.py",
                   alpha(("Reads", READS), prelude=UNIMPORTABLE))
        entry = self.status()
        self.assertEqual((entry["status"], entry["resolved"]),
                         ("met", OTHER_CLASS))

    def test_two_candidate_classes_stay_unwitnessed_and_name_both(self):
        for prelude in ("", UNIMPORTABLE):
            with self.subTest(importable=not prelude):
                self.write("tests/test_alpha.py", alpha(
                    ("Reads", READS), ("Writes", READS), prelude=prelude))
                entry = self.status()
                self.assertEqual(entry["status"], "unwitnessed")
                self.assertTrue(entry["note"].startswith(
                    f"named test not found: {CITED}"), entry["note"])
                self.assertTrue(entry["note"].endswith(
                    "; defined on classes Reads, Writes)"), entry["note"])
                self.assertNotIn("resolved", entry)

    def test_no_candidate_class_keeps_the_import_note(self):
        self.write("tests/test_alpha.py", alpha(("Reads", OTHER)))
        self.assertEqual(self.status(), {
            "n": 1, "status": "unwitnessed",
            "note": f"named test not found: {CITED} "
                    "(import: no callable test_reads on class Wrong)"})

    def test_a_def_inside_a_string_is_not_a_candidate(self):
        quoted = '    """\n    def test_reads(self):\n    """\n' + OTHER
        self.write("tests/test_alpha.py",
                   alpha(("Reads", quoted), prelude=UNIMPORTABLE))
        entry = self.status()
        self.assertEqual(entry["status"], "unwitnessed")
        self.assertNotIn("resolved", entry)


class DottedModuleTests(Worktree):
    REPLY = ("CRITERION 1: met — python3 -m ci.check tests.core.test_sources"
             " exits 0")

    def test_dotted_citation_resolves_to_an_existing_module_file(self):
        self.write("tests/core/test_sources.py", "def test_x():\n    pass\n")
        entry = self.status(self.REPLY)
        self.assertEqual((entry["status"], entry["resolved"]), ("met", [
            {"cited": "tests/core.py::test_sources",
             "as": "tests/core/test_sources.py", "rule": "dotted module"}]))

    def test_dotted_citation_without_a_module_file_keeps_its_note(self):
        (self.root / "tests").mkdir()
        self.assertEqual(self.status(self.REPLY), {
            "n": 1, "status": "unwitnessed",
            "note": "named test not found: tests/core.py::test_sources "
                    "(no file tests/core.py)"})


LOAD = """\
import json
import unittest


def seed():
    return json.loads("[1]")


class LoadCase(unittest.TestCase):
    def setUp(self):
        self.rows = seed()


class LoadTests(LoadCase):
    def test_missing(self):
        self.assertTrue(self.rows)

    def test_broken(self):
        self.assertEqual(self.rows, [1])
"""


class ApprovalTests(Worktree):
    CITED = "tests/test_load.py::LoadTests::test_broken"

    def setUp(self):
        super().setUp()
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test reviewer"),
                     ("config", "user.email", "reviewer@example.test")):
            self.git(*args)
        self.write(".gitignore", "__pycache__/\n")
        self.approved = self.commit("tests/test_load.py", LOAD)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, path, text):
        self.write(path, text)
        self.git("add", ".")
        self.git("commit", "-qm", "change")
        return self.git("rev-parse", "HEAD")

    def judged(self, candidate, cited=None):
        reply = (f"CRITERION 1: met — approval at {self.approved}; "
                 f"{cited or self.CITED}")
        approved_range = (self.approved, candidate)
        return (criteria_findings(reply, CRITERIA, self.root,
                                  approved_range=approved_range),
                criteria_statuses(reply, CRITERIA, self.root,
                                  approved_range=approved_range)[0])

    def test_other_tests_and_new_imports_leave_the_citation_standing(self):
        candidate = self.commit("tests/test_load.py", LOAD.replace(
            "import unittest\n", "import unittest\nfrom unittest import mock\n"
        ).replace("self.assertTrue(self.rows)", "self.assertEqual(len(self.rows), 1)"
        ) + ("\n    def test_new(self):\n        self.assertIsNotNone(mock)\n"
             "\n\nclass MoreTests(unittest.TestCase):\n"
             "    def test_more(self):\n        pass\n"))
        findings, entry = self.judged(candidate)
        self.assertEqual(findings, [])
        self.assertEqual((entry["status"], entry["resolved"]), ("met", [
            {"cited": self.CITED, "as": self.CITED,
             "rule": "unchanged since approval"}]))

    def test_a_submodule_import_sharing_an_imported_root_is_not_a_rebinding(self):
        candidate = self.commit("tests/test_load.py", LOAD.replace(
            "import json\n", "import json\nimport json.decoder\n"))
        findings, _ = self.judged(candidate)
        self.assertEqual(findings, [])

    def test_a_new_class_shadowing_the_cited_class_is_a_change(self):
        shadowed = LOAD + ("\n\nclass LoadTests(unittest.TestCase):\n"
                           "    def test_more(self):\n        pass\n")
        self.assertFalse(citations.test_unchanged(LOAD, shadowed, "LoadTests",
                                                  "test_broken"))

    def test_a_change_the_cited_test_depends_on_makes_it_stale(self):
        cases = {
            "cited body": ("self.assertEqual(self.rows, [1])",
                           "self.assertEqual(self.rows, [2])"),
            "base setUp": ("self.rows = seed()", "self.rows = seed() * 2"),
            "helper": ('json.loads("[1]")', 'json.loads("[1, 1]")'),
            "removed import": ("import json\n", ""),
            "changed import": ("import json\n", "import simplejson as json\n"),
            "import rebinding seed": ("import unittest\n",
                                      "import unittest\nfrom random import seed\n"),
            "unparseable": ("def seed():", "def seed(:"),
        }
        for case, (old, new) in cases.items():
            with self.subTest(case=case):
                self.git("checkout", "-q", self.approved)
                candidate = self.commit("tests/test_load.py",
                                        LOAD.replace(old, new))
                findings, entry = self.judged(candidate)
                self.assertEqual(entry["status"], "unwitnessed")
                self.assertIn(f"tests/test_load.py (changed since approval at "
                              f"{self.approved})", entry["note"])
                self.assertNotIn("resolved", entry)
                self.assertEqual(len(findings), 1)

    def test_a_changed_script_test_file_stays_stale(self):
        self.approved = self.commit("console/tests/a.test.ts",
                                    'test("reads", () => {});\n')
        candidate = self.commit("console/tests/a.test.ts",
                                'test("reads", () => {});\n'
                                'test("writes", () => {});\n')
        findings, entry = self.judged(candidate, 'console/tests/a.test.ts::"reads"')
        self.assertEqual(entry["status"], "unwitnessed")
        self.assertIn(f"changed since approval at {self.approved}", entry["note"])
        self.assertEqual(len(findings), 1)


if __name__ == "__main__":
    unittest.main()

"""`holo` write verbs: one JSON result with the interventions row they wrote,
or one line for a person, the exit code unchanged.

Run: python3 -m unittest discover -s tests -p 'test_holo_results.py' -v
"""
import getpass
import json
import os
import sqlite3
import unittest

import store
import store.tickets
from holophyte.config.project import Project
from holophyte.holo import cli as holo_cli
from holophyte.holo.grammar import COMMANDS, parse
from store.operator_notes import notes, send_back
from tests.test_holo_grammar import T0, Home, holo
from tests.test_provider import ticket_body

PR_URL = "https://example.invalid/org/repo/pull/1"


class WriteVerbTableTests(unittest.TestCase):
    def test_every_verb_that_records_a_note_or_files_a_ticket_accepts_json(self):
        writers = [command for command in COMMANDS
                   if command.note is not None or command.words == ("file",)]
        self.assertIn(("send-back",), [command.words for command in writers])
        for command in writers:
            with self.subTest(words=command.words):
                taken = [name for name in command.takes if not name.startswith("[")]
                landed = [command.landed] if command.landed else []
                note = ["why"] if command.note else []
                argv = [*command.words, *taken, *landed, *note, "--json"]
                args = parse(holo_cli.build_parser(), argv)
                self.assertIs(args.json, True)


class ResultTests(Home):
    def setUp(self):
        super().setUp()
        self.path = self.repo("repo", "HOLO")
        self.conn = store.open(str(Project.locate(self.path).store_path))
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "native:HOLO", self.path)
        self.ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-1",
            linear_identifier="HOLO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60_000)
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run = store.claim(self.conn, self.project_id, self.ticket, now=T0)

    def holo(self, *words):
        return holo(*words, "-p", str(self.path), home=self.home)

    def interventions(self):
        return self.conn.execute(
            "SELECT * FROM interventions WHERE action != 'migrate' ORDER BY id"
        ).fetchall()

    def intervention_id(self, action):
        (row,) = self.conn.execute(
            "SELECT id FROM interventions WHERE action = ?", (action,)).fetchall()
        return row[0]

    def result(self, completed, code):
        self.assertEqual(completed.returncode, code, completed.stderr)
        self.assertEqual(len(completed.stdout.splitlines()), 1, completed.stdout)
        return json.loads(completed.stdout)

    def fail_the_run(self):
        store.release(self.conn, self.run, "failed", "verify failed", now=T0 + 1)

    def park_on_the_pull_request(self):
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, self.run, phase)
        store.park(self.conn, self.run, "awaiting_merge_approval", pr_url=PR_URL)
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")

    def requeue_refusal(self):
        with self.assertRaises(store.RequeueRefused) as refused:
            store.requeue(self.conn, self.ticket, "rerun")
        return str(refused.exception)


class RequeueResultTests(ResultTests):
    def test_requeue_json_cites_the_requeue_row_it_wrote(self):
        self.fail_the_run()
        result = self.result(self.holo("requeue", "HOLO-1", "rerun", "--json"), 0)
        self.assertEqual(result, {
            "action": "requeue", "ok": True, "ticket": "HOLO-1", "run": self.run,
            "recorded": self.intervention_id("requeue"),
            "detail": f"HOLO-1 requeued after run {self.run}"})

    def test_a_refused_requeue_json_is_not_ok_and_records_nothing(self):
        before = self.interventions()
        result = self.result(self.holo("requeue", "HOLO-1", "rerun", "--json"), 1)
        self.assertEqual(self.interventions(), before)
        self.assertEqual(result, {"action": "requeue", "ok": False, "ticket": "HOLO-1",
                                  "recorded": None, "detail": self.requeue_refusal()})

    def test_a_repeated_pause_writes_no_row_and_cites_none(self):
        first = self.result(self.holo("pause", "HOLO-1", "lunch", "--json"), 0)
        self.assertEqual(first["recorded"], self.intervention_id("pause"))
        again = self.result(self.holo("pause", "HOLO-1", "lunch", "--json"), 0)
        self.assertEqual((again["ok"], again["recorded"]), (True, None))

    def test_without_json_a_requeue_prints_one_line_naming_its_intervention(self):
        self.fail_the_run()
        completed = self.holo("requeue", "HOLO-1", "rerun")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (line,) = completed.stdout.splitlines()
        self.assertTrue(line.startswith("✓ "), line)
        self.assertIn("HOLO-1", line)
        self.assertIn(f"intervention {self.intervention_id('requeue')} recorded", line)

    def test_without_json_a_refusal_prints_one_line_with_the_refusal(self):
        completed = self.holo("requeue", "HOLO-1", "rerun")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, f"✗ {self.requeue_refusal()}\n")


class SendBackResultTests(ResultTests):
    def test_send_back_attaches_the_note_by_the_callers_login(self):
        self.park_on_the_pull_request()
        result = self.result(self.holo("send-back", str(self.run), "address the nit",
                                       "--json"), 0)
        self.assertEqual((result["action"], result["ok"], result["run"]),
                         ("send-back", True, self.run))
        self.assertEqual(result["recorded"], self.intervention_id("operator_note"))
        (note,) = notes(self.conn, self.run)
        self.assertEqual((note["note"], note["author"]),
                         ("address the nit", getpass.getuser()))

    def test_send_back_of_a_run_not_parked_is_the_stores_refusal(self):
        before = list(self.conn.iterdump())
        completed = self.holo("send-back", str(self.run), "note")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(list(self.conn.iterdump()), before)
        with self.assertRaises((store.ApproveRefused, ValueError)) as refused:
            send_back(self.conn, self.run, "note", getpass.getuser())
        self.assertIn(str(refused.exception), completed.stderr)

    def test_send_back_of_a_run_id_beyond_sqlite_integers_is_a_usage_error(self):
        before = list(self.conn.iterdump())
        completed = self.holo("send-back", str(2**63), "note", "--json")
        self.assertNotIn("Traceback", completed.stderr)
        self.assertEqual(self.result(completed, 2)["ok"], False)
        self.assertEqual(list(self.conn.iterdump()), before)


class ProjectAndGapResultTests(ResultTests):
    def test_hold_json_cites_the_projects_hold_row(self):
        result = self.result(self.holo("hold", "maintenance", "--json"), 0)
        (row,) = self.conn.execute(
            "SELECT id FROM interventions WHERE action = 'hold' AND projectId = ?",
            (self.project_id,)).fetchall()
        self.assertEqual((result["action"], result["ok"], result["recorded"]),
                         ("hold", True, row[0]))

    def test_gap_json_records_no_intervention(self):
        before = self.interventions()
        result = self.result(self.holo("gap", "HOLO-1", "witness", "note", "--json"), 0)
        self.assertEqual((result["action"], result["ok"], result["recorded"]),
                         ("gap", True, None))
        self.assertEqual(self.interventions(), before)
        self.assertEqual(self.conn.execute(
            "SELECT layer, note FROM gapLayers WHERE ticketId = ?",
            (self.ticket,)).fetchall(), [("witness", "note")])


class StoreLocationResultTests(ResultTests):
    def test_a_repeated_pause_from_an_adopted_legacy_store_cites_no_row(self):
        store.pause(self.conn, self.run, "lunch")
        self.conn.close()
        current = Project.locate(self.path, adopt=False).store_path
        legacy = self.path.parent / f"{self.path.name}.holophyte.db"
        os.replace(current, legacy)
        result = self.result(self.holo("pause", "HOLO-1", "lunch", "--json"), 0)
        self.assertFalse(legacy.exists())
        self.assertEqual((result["ok"], result["recorded"]), (True, None))


class EmptyStoreResultTests(Home):
    def test_hold_json_on_an_empty_store_file_initializes_it_and_cites_the_row(self):
        path = self.repo("repo", "HOLO")
        sqlite3.connect(Project.locate(path, adopt=False).store_path).close()
        completed = holo("hold", "maintenance", "--json", "-p", str(path),
                         home=self.home)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        conn = store.open(str(Project.locate(path, adopt=False).store_path))
        self.addCleanup(conn.close)
        (row,) = conn.execute(
            "SELECT id FROM interventions WHERE action = 'hold'").fetchall()
        self.assertEqual((result["ok"], result["recorded"]), (True, row[0]))


class UsageAndFileResultTests(Home):
    def test_a_usage_error_with_json_prints_a_result_and_exits_two(self):
        completed = holo("requeue", "--json", home=self.home)
        self.assertEqual(completed.returncode, 2)
        (line,) = completed.stdout.splitlines()
        result = json.loads(line)
        self.assertEqual((result["action"], result["ok"], result["recorded"]),
                         ("requeue", False, None))
        self.assertEqual(result["detail"], completed.stderr.splitlines()[-1])

    def test_a_file_update_json_names_the_ticket_it_updates(self):
        path = self.repo("repo", "HOLO")
        (path / "tests").mkdir()
        (path / "tests" / "test_thing.py").write_text("")
        ticket = path.parent / "T.md"
        ticket.write_text(ticket_body(
            verify="python3 -m unittest discover -s tests -p 'test_thing.py'"))
        filed = self.result(holo("file", str(ticket), "--json", "-p", str(path),
                                 home=self.home))
        self.assertNotIn("ticket", filed)
        updated = self.result(holo("file", str(ticket), "--update", "HOLO-1",
                                   "--revision", "1", "--json", "-p", str(path),
                                   home=self.home))
        self.assertEqual((updated["ok"], updated["ticket"]), (True, "HOLO-1"))

    def result(self, completed):
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import store
import store.criteria
import store.tickets
from holophyte.agents import fix_session
from holophyte.babysit.covering_fix import fix_note_of
from holophyte.loop import adjudicate, contradiction, runs
from holophyte.loop.gates import MergeParked, RunFailure
from holophyte.review import criterion_ids, witness_precheck
from holophyte.review.briefs import criteria_brief
from holophyte.review.reply_parsing import criteria_findings, criteria_statuses

TEXTS = [
    "The result is covered by `tests/test_result.py`",
    "The output names its owner",
    "The missing field is covered by `tests/test_missing.py`",
]


class CriterionIdsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test reviewer")
        self.git("config", "user.email", "reviewer@example.test")
        self.git("commit", "-qm", "base", "--allow-empty")
        self.base = self.git("rev-parse", "HEAD")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_result.py").write_text(
            "import unittest\n\nclass Result(unittest.TestCase):\n"
            "    def test_result(self):\n        self.assertEqual(2 + 2, 4)\n")
        self.git("add", "tests")
        self.git("commit", "-qm", "witness")
        self.head = self.git("rev-parse", "HEAD")
        self.conn = store.open(str(self.root / "store.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team", self.root)
        self.ticket = self.mirror(TEXTS[:2] + ["Retired criterion", TEXTS[2]])
        self.mirror(TEXTS)
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        self.run_id = store.claim(self.conn, self.project_id, self.ticket)
        store.set_phase(self.conn, self.run_id, "working")

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True).strip()

    def mirror(self, criteria):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue",
            linear_identifier="KO-1",
            title="Stable criteria", acceptance_criteria=criteria,
            verification_commands=["echo ok"], time_box_ms=60_000)

    def pairs(self):
        return criterion_ids.numbered(self.conn, self.run_id, TEXTS)

    def record(self, reply):
        runs.record_round(
            None, self.conn, self.run_id, 1, "review", reply, "echo ok", True,
            "ok", criteria=self.pairs(), root=self.root, route="reviewer")

    def test_retired_number_stays_absent_from_brief_and_no_store_is_positional(self):
        pairs = self.pairs()
        self.assertEqual(pairs, [(1, TEXTS[0]), (2, TEXTS[1]), (4, TEXTS[2])])
        brief = criteria_brief(pairs)
        self.assertIn(f"C1. {TEXTS[0]}\nC2. {TEXTS[1]}\nC4. {TEXTS[2]}", brief)
        self.assertIn("CRITERION C4:", brief)
        positional = criterion_ids.numbered(None, None, TEXTS)
        self.assertEqual(positional, [(1, TEXTS[0]), (2, TEXTS[1]), (3, TEXTS[2])])
        self.assertIn(f"C3. {TEXTS[2]}", criteria_brief(positional))

    def test_old_text_keeps_its_id_and_unstored_text_does_not_take_a_held_id(self):
        self.mirror([TEXTS[0], TEXTS[1], "**C4** Revised fourth criterion"])
        pairs = criterion_ids.numbered(
            self.conn, self.run_id, ["New text", *TEXTS])
        self.assertEqual(pairs, [(5, "New text"), (1, TEXTS[0]),
                                 (2, TEXTS[1]), (4, TEXTS[2])])
        self.assertEqual(criterion_ids.numbered(
            self.conn, self.run_id, ["**C4** Revised fourth criterion"]),
            [(4, "Revised fourth criterion")])

    def test_mixed_reply_forms_use_ids_for_statuses_and_findings(self):
        reply = ("CRITERION c4: not met — no test\n"
                 "CRITERION 1: met — tests/test_result.py::Result::test_result\n"
                 "VERDICT: REQUEST_CHANGES")
        statuses = criteria_statuses(reply, self.pairs(), self.root)
        self.assertEqual([(entry["n"], entry["status"]) for entry in statuses],
                         [(1, "met"), (2, "unwitnessed"), (4, "not_met")])
        findings = criteria_findings(reply, self.pairs(), self.root)
        self.assertEqual([finding["line"] for finding in findings], [2, 4])
        self.assertTrue(findings[1]["message"].startswith("CRITERION C4: not met"))
        self.assertIn(TEXTS[2], findings[1]["message"])

    def test_automatic_babysit_fix_note_preserves_criterion_id(self):
        findings = criteria_findings(
            "CRITERION C4: not met — no test", self.pairs(), self.root)
        self.assertIn(
            "Meet acceptance criterion C4 and name the test that witnesses it: "
            f"{TEXTS[2]}",
            fix_note_of(findings))

    def test_review_event_carries_stable_number_and_id_for_each_text(self):
        self.record("CRITERION C4: not met — no test\nVERDICT: REQUEST_CHANGES")
        ((payload,),) = self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'criteria'",
            (self.run_id,)).fetchall()
        self.assertEqual([(entry["n"], entry["id"], entry["text"])
                          for entry in json.loads(payload)["criteria"]],
                         [(1, "C1", TEXTS[0]), (2, "C2", TEXTS[1]),
                          (4, "C4", TEXTS[2])])

    def test_terminal_prompt_resolves_stored_finding_number_to_third_text(self):
        self.record("CRITERION 1: met — tests/test_result.py::Result::test_result\n"
                    "CRITERION 2: met — checked the output\n"
                    "CRITERION C4: not met — no test\nVERDICT: REQUEST_CHANGES")
        with patch.object(adjudicate, "agent", return_value="VERDICT: FAIL") as agent, \
                patch.object(runs, "agent_route", return_value="adjudicator"):
            with self.assertRaises(RunFailure):
                adjudicate._terminal_adjudication(
                    None, self.conn, self.run_id, None, "KO-1", {}, "main",
                    self.root, 60, self.base, self.head, "Ticket", "echo ok", [], 1,
                    criteria=TEXTS, resume={"terminal": True, "ok": True, "out": "ok"})
        prompt = agent.call_args.args[2]
        self.assertIn("The newest review round left these criteria unmet or "
                      f"unwitnessed:\nC4. {TEXTS[2]}\n", prompt)

    def assert_parked_with_canonical_id(self, token):
        reason = "the payload has no such field"
        with self.assertRaises(MergeParked):
            contradiction.park(
                self.conn, self.run_id, SimpleNamespace(native=True),
                "KO-1", self.root, "implement",
                f"CONTRADICTION: criterion {token} — {reason}", False)
        ((question,),) = self.conn.execute(
            "SELECT blockedQuestion FROM tickets WHERE id = ?", (self.ticket,)
        ).fetchall()
        self.assertEqual(question.splitlines()[0],
                         "implementer: criterion C4 cannot be met as written: "
                         f"{reason}")
        ((payload,),) = self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'contradiction'",
            (self.run_id,)).fetchall()
        self.assertEqual(json.loads(payload)["criteria"],
                         [{"criterion": 4, "id": "C4", "reason": reason}])
        self.assertEqual(self.conn.execute(
            "SELECT phase, parkKind FROM runs WHERE id = ?", (self.run_id,)
        ).fetchone(), ("blocked_on_operator", "contradiction"))

    def test_contradiction_id_form_parks_with_canonical_id(self):
        self.assert_parked_with_canonical_id("C4")

    def test_contradiction_bare_form_parks_with_canonical_id(self):
        self.assert_parked_with_canonical_id("4")

    def test_fix_prompt_keeps_claimed_criteria_when_body_has_no_checklist(self):
        with patch.object(fix_session, "_session_turn") as turn:
            fix_session.fix_turn(
                None, self.conn, self.run_id, 60, self.root, 5, "Ticket title",
                "CRITERION C4: not met — no test", self.head,
                timed=None, check_cap=None)
        resumed, fresh = turn.call_args.args[7:9]
        for prompt in (resumed, fresh):
            self.assertIn(f"C1. {TEXTS[0]}\nC2. {TEXTS[1]}\nC4. {TEXTS[2]}", prompt)
            self.assertIn("CONTRADICTION: criterion C<id>", prompt)

    def test_storeless_fix_prompt_keeps_supplied_run_criteria(self):
        with patch.object(fix_session, "_session_turn") as turn:
            fix_session.fix_turn(
                None, None, None, 60, self.root, 5, "Ticket title",
                "CRITERION C3: not met — no test", self.head,
                criteria=criterion_ids.numbered(None, None, TEXTS),
                timed=None, check_cap=None)
        resumed, fresh = turn.call_args.args[7:9]
        for prompt in (resumed, fresh):
            self.assertIn(f"C1. {TEXTS[0]}\nC2. {TEXTS[1]}\nC3. {TEXTS[2]}", prompt)
            self.assertIn("CONTRADICTION: criterion C<id>", prompt)

    def test_empty_criteria_have_no_contradiction_brief(self):
        self.assertEqual(contradiction.brief([]), "")

    def test_real_candidate_witness_gaps_name_stable_ids(self):
        entries = witness_precheck.check(
            self.root, self.base, self.head, self.pairs(), "echo ok", lambda _: "")
        self.assertEqual([entry["criterion"] for entry in entries], [1, 2, 4])
        self.assertEqual(entries[2]["gaps"], [
            "criterion C4 names `tests/test_missing.py`; it does not exist "
            f"at {self.head[:12]}"])
        summary = witness_precheck.summary(self.head, entries)
        self.assertIn("criterion C4 names `tests/test_missing.py`", summary)
        self.assertIn("criterion C2 names no test file", summary)


if __name__ == "__main__":
    unittest.main()

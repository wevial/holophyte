"""`--report` compares the adversary families and sums what consolidation did."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_adversary  # noqa: E402
from fake_agent import APPROVE  # noqa: E402
from test_wiring_telemetry import ReportStoreCase  # noqa: E402

import holophyte.cli.report as report  # noqa: E402
import store  # noqa: E402

Change, attack, finding = (test_adversary.Change, test_adversary.attack,
                           test_adversary.finding)
THREE = ("SUBAGENT: luna — CLI\n", "SUBAGENT: sol — data and migrations\n",
         "SUBAGENT: sol — HTTP\n")


class SubagentLinesTests(test_adversary.AdversaryFixture):
    def recorded(self, *lines):
        self.configure(test_adversary.ON)
        self.loop(Change("poetry.lock"), APPROVE, attack(*lines))
        [event] = self.events()
        return event

    def test_three_lines_are_recorded_in_order_under_the_cap(self):
        event = self.recorded(finding("src/app.py", 3, "slow path", "concern"),
                              *THREE)
        self.assertEqual(event["subagents"], [
            {"model": "luna", "surface": "CLI"},
            {"model": "sol", "surface": "data and migrations"},
            {"model": "sol", "surface": "HTTP"}])
        self.assertIs(event["over_cap"], False)
        [concern] = event["concerns"]
        self.assertNotIn("SUBAGENT", concern["message"])

    def test_six_lines_are_all_recorded_and_flag_the_cap(self):
        event = self.recorded(*THREE, *THREE)
        self.assertEqual(len(event["subagents"]), 6)
        self.assertIs(event["over_cap"], True)

    def test_a_reply_without_subagent_lines_records_an_empty_list(self):
        event = self.recorded()
        self.assertEqual(event["subagents"], [])
        self.assertIs(event["over_cap"], False)


def subagents(*models):
    return [{"model": model, "surface": "CLI"} for model in models]


def evidence(level):
    return {"path": "src/app.py", "line": 3, "severity": "p1",
            "message": f"- src/app.py:3 [p1] breaks\nEVIDENCE: {level}",
            "evidence": level}


def adversary_pass(family, depth, seconds, models=(), findings=(), concerns=0):
    return {"round": 1, "tier": "high" if depth == "full" else "medium",
            "depth": depth, "scope": "candidate", "range": ["a" * 40, "b" * 40],
            "family": family, "model": None, "effort": None, "outcome":
            "blocked" if findings else "clear", "seconds": seconds,
            "subagents": subagents(*models), "over_cap": len(models) > 5,
            "findings": [evidence(level) for level in findings],
            "concerns": [evidence("concern")] * concerns}


def consolidation(rnd, pass2, merges=0, held=0):
    return {"round": rnd, "pass1_in": 3, "pass1_out": 3, "pass2": pass2,
            "merges": [{"from": "F2", "into": "F1", "reason": "same bug"}] * merges,
            "ignored": [], "sent_concerns": [],
            "held_concerns": [dict(evidence("concern"), found_by=["adversary"],
                                   notes=[], round=rnd)] * held}


class ReportLinesTests(ReportStoreCase):
    def setUp(self):
        super().setUp()
        self.completed_run(1, 5, 25, 1, "merged")
        [(self.run_id,)] = self.conn.execute("SELECT id FROM runs").fetchall()

    def record(self, kind, payload):
        store.record_event(self.conn, self.run_id, kind, kind, level="detail",
                           payload=json.dumps(payload))
        self.conn.commit()

    def lines(self, prefix):
        return [line for line in report.report_lines(self.conn)
                if line.startswith(prefix)]

    def test_each_family_gets_one_line_claude_before_codex(self):
        self.record("adversary_round", adversary_pass(
            "codex", "light", 90, ("sol", "luna", "sol"), ("traced",)))
        self.record("adversary_round", adversary_pass(
            "claude", "full", 60, ("opus", "haiku"), ("reproduced",), 1))
        self.record("adversary_round", adversary_pass(
            "claude", "light", 30, ("opus",), concerns=1))
        lines = report.report_lines(self.conn)
        claude = lines.index(
            "adversary claude: 2 passes (full 1 · light 1) · 3 subagents"
            " (opus 2 · haiku 1) · reproduced 1 · traced 0 · concerns 2"
            " · blocked 1 · 1.5 min")
        self.assertEqual(lines[claude + 1],
                         "adversary codex: 1 passes (full 0 · light 1)"
                         " · 3 subagents (sol 2 · luna 1) · reproduced 0"
                         " · traced 1 · concerns 0 · blocked 1 · 1.5 min")
        self.assertEqual(len(self.lines("adversary ")), 2)

    def test_consolidation_sums_rounds_merges_held_and_unavailable(self):
        for rnd, (pass2, merges, held) in enumerate(
                (("merged", 2, 1), ("unavailable", 0, 2), ("skipped", 0, 0)), 1):
            self.record("consolidation", consolidation(rnd, pass2, merges, held))
        self.assertEqual(self.lines("consolidation"), [
            "consolidation: 3 rounds · 2 merges · 3 held concerns · 1 unavailable"])

    def test_no_events_print_neither_line_and_leave_the_rest_alone(self):
        before = report.report_lines(self.conn)
        self.assertEqual([line for line in before if line.startswith(
            ("adversary ", "consolidation"))], [])
        self.record("adversary_round", adversary_pass("claude", "full", 60))
        self.record("consolidation", consolidation(1, "skipped"))
        after = report.report_lines(self.conn)
        added = [line for line in after if line not in before]
        self.assertEqual([line.split(":")[0] for line in added],
                         ["adversary claude", "consolidation"])
        self.assertEqual([line for line in after if line not in added], before)

    def test_a_pass_with_no_subagents_shows_zero_and_no_model_list(self):
        self.record("adversary_round", adversary_pass("codex", "light", 30))
        self.assertEqual(self.lines("adversary "), [
            "adversary codex: 1 passes (full 0 · light 1) · 0 subagents"
            " · reproduced 0 · traced 0 · concerns 0 · blocked 0 · 0.5 min"])

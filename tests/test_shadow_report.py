"""`--report`'s shadow implementer section: the primary's and the shadow's
first implementation of each shadowed run, side by side, read from the store."""
import io
import json
import re
import sys
from unittest.mock import patch

import holophyte.cli.entry
import holophyte.cli.report as report
import store
import store.tickets
from tests.phase_fixture import finish_run
from tests.test_wiring_telemetry import ReportStoreCase, no_network

AT = 1_700_000_000_000


def usage(tokens_in, tokens_out, cost):
    return {"input_tokens": tokens_in, "output_tokens": tokens_out,
            "cost_usd": cost, "num_turns": 3}


def turn(role, seconds, used=None):
    payload = {"role": role, "label": "claude opus", "route": "primary",
               "exit_status": 0, "timed_out": False, "seconds": seconds}
    if used is not None:
        payload["usage"] = used
    return payload


def shadow(seconds, used, ok, verdict, findings=None):
    return {"route": "claude sonnet high", "branch": "shadow/x", "base_sha": "a",
            "head_sha": "b", "commits": 1, "lines_changed": 10,
            "seconds": seconds, "exit_status": 0, "timed_out": False,
            "usage": used, "verify": {"ok": ok, "failed_command": None if ok else 0},
            "outcome": "verified" if ok else "verify_failed", "detail": None,
            "review": {"verdict": verdict, "findings": findings or {},
                       "unwitnessed": 0, "reviewer": "codex", "seconds": 30}}


def finding(severity):
    return {"path": "holophyte/x.py", "line": 3, "severity": severity,
            "message": f"a {severity} finding"}


def table_rows(lines):
    start = lines.index("shadow implementer:")
    header = re.split(r"\s{2,}", lines[start + 1])
    return [dict(zip(header, re.split(r"\s{2,}", line)))
            for line in lines[start + 2:]
            if line.split()[1:2] in (["primary"], ["shadow"])]


class ShadowReportTests(ReportStoreCase):

    def run_with(self, n, events, rounds=(), outcome="merged"):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"KO-{n}", title=f"ticket {n}")
        run_id = store.claim(self.conn, self.project_id, ticket, now=AT + n)
        for kind, payload in events:
            if kind == "review_round":
                store.record_review_round(self.conn, run_id, **payload)
            else:
                store.record_event(self.conn, run_id, kind, f"{kind} event",
                                   level="detail", payload=json.dumps(payload))
        self.conn.commit()
        if outcome is not None:
            finish_run(self.conn, run_id, outcome, now=AT + n + 60_000)
        return run_id

    def report(self):
        out = io.StringIO()
        with no_network(), patch.object(sys, "stdout", out):
            holophyte.cli.entry.cli(["--report", str(self.target)])
        return out.getvalue().splitlines()

    def section(self):
        return report.report_lines(self.conn)

    def test_report_compares_the_first_implementations_and_the_primary_run(self):
        passing = [{"command": "make test", "exitCode": 0, "output": "ok"}]
        self.run_with(1, [
            ("agent_turn", turn("implement", 600, usage(1000, 200, 1.50))),
            ("agent_turn", turn("implement", 60, usage(100, 20, 0.10))),
            ("agent_turn", turn("trim", 30, usage(50, 10, 0.20))),
            ("agent_turn", turn("review", 120)),
            ("review_round", {"round_number": 1, "verdict": "changes_requested",
                              "reviewer_model": "codex",
                              "findings": [finding("p1"), finding("p2")],
                              "verification_results": passing}),
            ("agent_turn", turn("implement", 200, usage(400, 80, 0.40))),
            ("agent_turn", turn("review", 120)),
            ("review_round", {"round_number": 2, "verdict": "pass",
                              "reviewer_model": "codex",
                              "verification_results": passing}),
            ("shadow_result", shadow(300, usage(900, 150, 0.30), True, "APPROVE")),
        ])
        primary, mirror = table_rows(self.report())
        self.assertEqual(
            {key: primary[key] for key in (
                "ticket", "route", "min", "in", "out", "cost", "verify",
                "round 1", "findings", "rounds", "all turns", "outcome")},
            {"ticket": "KO-1", "route": "claude opus", "min": "11.0",
             "in": "1100", "out": "220", "cost": "$1.60", "verify": "verified",
             "round 1": "REQUEST_CHANGES", "findings": "p1 1, p2 1",
             "rounds": "2", "all turns": "$2.20", "outcome": "merged"})
        self.assertEqual(
            {key: mirror[key] for key in (
                "side", "route", "min", "in", "out", "cost", "verify",
                "round 1", "findings")},
            {"side": "shadow", "route": "claude sonnet high", "min": "5.0",
             "in": "900", "out": "150", "cost": "$0.30", "verify": "verified",
             "round 1": "APPROVE", "findings": "none"})

    def test_a_turn_without_usage_reads_na_but_its_minutes_still_count(self):
        self.run_with(1, [
            ("agent_turn", turn("implement", 600, usage(1000, 200, 1.50))),
            ("agent_turn", turn("implement", 120)),
            ("shadow_result", shadow(300, usage(900, 150, 0.30), True, "APPROVE")),
        ], outcome=None)
        primary, _ = table_rows(self.section())
        self.assertEqual((primary["min"], primary["in"], primary["out"],
                          primary["cost"], primary["all turns"],
                          primary["outcome"]),
                         ("12.0", "n/a", "n/a", "n/a", "n/a", "in flight"))

    def test_the_summary_counts_verified_sides_and_gives_both_medians(self):
        for n, (minutes, shadow_minutes, shadow_ok) in enumerate(
                ((10, 5, True), (30, 7, False), (20, 6, True)), 1):
            self.run_with(n, [
                ("agent_turn", turn("implement", minutes * 60, usage(1, 1, 1.0))),
                ("agent_turn", turn("review", 60)),
                ("review_round", {
                    "round_number": 1, "verdict": "pass",
                    "reviewer_model": "codex", "verification_results": [
                        {"command": "make", "exitCode": 0, "output": ""}]}),
                ("shadow_result", shadow(shadow_minutes * 60, usage(1, 1, 0.5),
                                         shadow_ok, "REQUEST_CHANGES",
                                         {"p2": 1})),
            ])
        lines = self.section()
        summary = lines[lines.index("shadow implementer:") + 8]
        self.assertIn("shadows 3 · verified primary 3/3, shadow 2/3", summary)
        self.assertIn("round-1 approvals primary 3/3, shadow 0/3", summary)
        self.assertIn("median first-implementation min primary 20.0,"
                      " shadow 6.0", summary)
        self.assertIn("first-implementation cost primary $3.00, shadow $1.50",
                      summary)

    def test_a_store_without_shadow_results_prints_no_section(self):
        with patch("socket.gethostname", return_value="writer"):
            self.run_with(1, [
                ("agent_turn", turn("implement", 600, usage(1000, 200, 1.50))),
                ("agent_turn", turn("review", 60)),
                ("review_round", {"round_number": 1, "verdict": "pass",
                                  "reviewer_model": "codex"}),
            ])
        self.assertEqual(self.report()[1:], [
            "in flight: none",
            "",
            "toil 24h: 0 human interventions, 0 merged",
            "toil 7d: 0 human interventions, 0 merged",
            "gap layers: impossible 0, static 0, witness 0, guidance 0,"
            " review 0, none 0",
            "gaps found: witness 0, operator 0",
            "ticket  actual  agent  verify  estimate  ratio  rounds  outcome"
            "  rejected  host",
            "KO-1       0.0    0.0     0.0       n/a    n/a       1  merged "
            "         0  writer",
            "1 runs · no estimates to compare against",
            "findings: none",
            "supervisor: none recorded",
        ])

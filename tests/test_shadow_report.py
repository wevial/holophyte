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

    def run_with(self, n, events, outcome="merged"):
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
        self.assertEqual(primary, {
            "ticket": "KO-1", "side": "primary", "route": "claude opus",
            "min": "11.0", "in": "1100", "out": "220", "cost": "$1.60",
            "verify": "verified", "round 1": "REQUEST_CHANGES",
            "findings": "p1 1, p2 1", "rounds": "2", "all turns": "$2.20",
            "outcome": "merged"})
        self.assertEqual(mirror, {
            "ticket": "KO-1", "side": "shadow", "route": "claude sonnet high",
            "min": "5.0", "in": "900", "out": "150", "cost": "$0.30",
            "verify": "verified", "round 1": "APPROVE", "findings": "none"})

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


def complexity(verdict, confidence):
    return {"verdict": verdict, "confidence": confidence, "reason": "scope",
            "backend": "claude", "model": "haiku", "why": None}


def profile(name, source):
    return {"profile": name, "source": source, "requested": None, "why": None,
            "label": "claude sonnet"}


def complexity_rows(lines):
    start = lines.index("complexity classifier:")
    header = re.split(r"\s{2,}", lines[start + 1])
    rows = []
    for line in lines[start + 2:]:
        if line.startswith("complexity "):
            break
        rows.append(dict(zip(header, re.split(r"\s{2,}", line.strip()))))
    return rows


def round_events(count, verdict="changes_requested"):
    return [("review_round", {"round_number": n, "verdict": verdict,
                              "reviewer_model": "codex"})
            for n in range(1, count + 1)]


class ComplexityReportTests(ReportStoreCase):

    run_with = ShadowReportTests.run_with
    report = ShadowReportTests.report
    section = ShadowReportTests.section

    def worked(self, run_id, minutes):
        self.conn.execute("UPDATE runs SET workingMs = ? WHERE id = ?",
                          (minutes * 60_000, run_id))
        self.conn.commit()

    def classified_store(self):
        small = self.run_with(1, [
            ("complexity", complexity("small", 0.9)),
            ("implementer_profile", profile("cheap", "classifier")),
            *round_events(1, "pass"),
            ("shadow_result", shadow(300, usage(1, 1, 0.1), True, "APPROVE")),
        ])
        large = self.run_with(2, [
            ("complexity", complexity("large", 0.7)),
            ("implementer_profile", profile(None, "default")),
            *round_events(3),
        ], outcome="failed")
        first = self.run_with(3, [], outcome="failed")
        (ticket,) = self.conn.execute("SELECT ticketId FROM runs WHERE id = ?",
                                      (first,)).fetchone()
        retry = store.claim(self.conn, self.project_id, ticket, now=AT + 100)
        for kind, payload in [("complexity", complexity("small", 0.8)),
                              ("implementer_profile", profile(None, "default"))]:
            store.record_event(self.conn, retry, kind, kind, level="detail",
                               payload=json.dumps(payload))
        for round_event in round_events(2):
            store.record_review_round(self.conn, retry, **round_event[1])
        self.conn.commit()
        finish_run(self.conn, retry, "merged", now=AT + 100 + 240_000)
        for run_id, minutes in ((small, 1), (large, 2), (retry, 4)):
            self.worked(run_id, minutes)

    def test_each_classified_run_shows_its_verdict_profile_and_first_attempt(self):
        self.classified_store()
        rows = complexity_rows(self.report())
        self.assertEqual([{key: row.get(key, "") for key in (
            "ticket", "verdict", "conf", "profile", "source", "first",
            "outcome", "rounds", "shadow")} for row in rows], [
            {"ticket": "KO-1", "verdict": "small", "conf": "0.90",
             "profile": "cheap", "source": "classifier", "first": "pass",
             "outcome": "merged", "rounds": "1", "shadow": "verified APPROVE"},
            {"ticket": "KO-2", "verdict": "large", "conf": "0.70",
             "profile": "default", "source": "default", "first": "fail",
             "outcome": "failed", "rounds": "3", "shadow": ""},
            {"ticket": "KO-3", "verdict": "small", "conf": "0.80",
             "profile": "default", "source": "default", "first": "-",
             "outcome": "merged", "rounds": "2", "shadow": ""}])
        self.assertEqual([row["min"] for row in rows], ["1.0", "2.0", "4.0"])

    def test_one_summary_line_per_verdict_counts_first_attempts_and_shadows(self):
        self.classified_store()
        lines = self.section()
        summaries = [line for line in lines if line.startswith("complexity ")]
        self.assertEqual(summaries, [
            "complexity classifier:",
            "complexity small: 2 runs · first-attempt pass 1/1 · median rounds"
            " 1.5 · median min 2.5 · shadowed 1 (verified 1,"
            " round-1 approvals 1)",
            "complexity large: 1 runs · first-attempt pass 0/1 · median rounds"
            " 3 · median min 2.0"])

    def test_an_unanswered_question_reads_none_and_summarises_last(self):
        self.run_with(1, [("complexity", complexity(None, None))])
        self.run_with(2, [("complexity", complexity("medium", 0.6))])
        lines = self.section()
        self.assertEqual([(row["ticket"], row["verdict"], row["conf"])
                          for row in complexity_rows(lines)],
                         [("KO-1", "none", "-"), ("KO-2", "medium", "0.60")])
        self.assertEqual(
            [line.split(":")[0] for line in lines
             if line.startswith("complexity ")][1:],
            ["complexity medium", "complexity none"])

    def test_a_store_without_complexity_events_prints_no_section(self):
        self.run_with(1, [
            ("shadow_result", shadow(300, usage(1, 1, 0.1), True, "APPROVE"))])
        lines = self.report()
        self.assertFalse([line for line in lines if "complexity" in line])
        start = lines.index("shadow implementer:")
        self.assertEqual(lines[start:start + 5],
                         report.shadow_lines(self.conn))

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_agent import APPROVE, REQUEST_CHANGES, Commit, Idle  # noqa: E402
from loop_fixture import UNTRIMMED, StubProvider, a_task  # noqa: E402
from test_pipeline_checks import CheckRuns, check_table  # noqa: E402
from test_pipeline_notes import WITNESS, TrimTurn, lines  # noqa: E402


class RequiredCheckTests(CheckRuns):
    def rounds(self):
        return [(rnd, verdict, json.loads(findings)) for rnd, verdict, findings
                in self.read("SELECT round, verdict, findings FROM reviewRounds"
                             " ORDER BY round")]

    def test_a_lint_failure_reaches_the_fix_and_the_fixed_candidate_merges(self):
        self.configure(UNTRIMMED
                       + check_table("lint", "implement", ["sh lint.sh"], "required"))
        broken = ("printf 'old output%01200d\\n' 0\n"
                  "echo 'trailing whitespace'\nexit 23\n")
        fake, _ = self.loop(Commit("work", "lint.sh", broken), APPROVE,
                            Commit("fix", "lint.sh", "exit 0\n"), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review", "implement", "review"])
        first, second = self.rounds()
        self.assertEqual(first[:2], (1, "changes_requested"))
        finding, = first[2]
        self.assertEqual((finding["path"], finding["severity"]), ("lint", "p1"))
        for fragment in ("sh lint.sh", "exit 23", "trailing whitespace"):
            self.assertIn(fragment, finding["message"])
        self.assertNotIn("old output", finding["message"])
        self.assertIn("trailing whitespace", fake.turns[2].goal)
        self.assertEqual(second, (2, "pass", []))
        self.assertEqual([(detail["round"], stage, outcome, edge)
                          for stage, _, outcome, edge, detail in self.attempts()
                          if stage in ("lint", "review")],
                         [(1, "lint", "fail", "on_fail"),
                          (1, "review", "fail", "on_fail"),
                          (2, "lint", "pass", "next"),
                          (2, "review", "pass", "next")])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertIn("fix", self.subjects())

    def test_persistent_required_failure_stops_at_the_same_cap_as_red_verify(self):
        failure = "exit 19"

        def terminal_record():
            self.assertEqual([row[:2] for row in self.rounds()],
                             [(1, "changes_requested"), (2, "changes_requested")])
            self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
            return (self.read("SELECT reviewRoundCap, outcome, failureKind FROM runs"),
                    self.read("SELECT kind, source FROM ledger ORDER BY id"))

        first_fix, next_fix = Commit("fix one"), Commit("fix two")
        self.configure(UNTRIMMED)
        self.loop(Commit("work"), APPROVE, first_fix, Idle(),
                  APPROVE, next_fix, Idle(),
                  provider=StubProvider(dict(a_task(), verify=failure)))
        oracle = terminal_record()
        self.assertEqual(oracle[0], [(2, "failed", "verify")])
        self.assertIn(("failure", "loop"), oracle[1])

        self.fresh()
        self.configure(UNTRIMMED
                       + check_table("lint", "implement", [failure], "required"))
        self.loop(Commit("work"), APPROVE, first_fix, APPROVE, next_fix)
        self.assertEqual(terminal_record(), oracle)
        self.assertEqual([row[2][0]["path"] for row in self.rounds()], ["lint", "lint"])

    def test_timed_out_check_on_the_trimmed_head_blocks_the_first_round(self):
        commands = ["tail -1 work.txt", "exec sleep 10"]
        self.configure("[trim]\nbudget_min = 1\n[verify]\ntimeout_sec = 1\n"
                       + check_table("tidy", "trim", commands, "required"))
        self.loop(TrimTurn(Commit("work", "work.txt", lines(60)),
                           Commit("tests", "test_work.py", WITNESS)),
                  TrimTurn(Commit("trim: delete", "work.txt", lines(40))),
                  APPROVE, Idle())

        first, = self.rounds()
        self.assertEqual(first[:2], (1, "changes_requested"))
        finding, = first[2]
        self.assertEqual(finding["path"], "tidy")
        self.assertIn("line 39", finding["message"])
        tidy, = [row for row in self.attempts() if row[0] == "tidy"]
        self.assertEqual(tidy[2:4], ("fail", "on_fail"))
        self.assertTrue(tidy[4]["timed_out"])

    def test_passing_required_check_preserves_the_review_cycle(self):
        script = (Commit("work"), REQUEST_CHANGES, Commit("fix"), APPROVE)
        plain = self.walked("", *script)
        rounds = self.rounds()
        self.assertEqual(len(plain[0]), 4)
        self.assertEqual(plain[3], [("merged",)])

        self.fresh()
        checked = self.walked(check_table("lint", "implement", ["true"], "required"),
                              *script)
        self.assertEqual([(row[4]["round"], row[2:4]) for row in self.attempts()
                          if row[0] == "lint"],
                         [(1, ("pass", "next")), (2, ("pass", "next"))])
        self.assertEqual(self.rounds(), rounds)
        self.assertEqual(checked, plain)

import json
import os
import re
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_agent import APPROVE, REQUEST_CHANGES, Commit, Idle  # noqa: E402
from loop_fixture import UNTRIMMED, StubProvider, a_task  # noqa: E402
from pause_fixture import PauseReply  # noqa: E402
from test_pipeline_checks import (  # noqa: E402
    GIT_DATES,
    CheckRuns,
    PausingCommit,
    check_table,
)
from test_pipeline_notes import WITNESS, TrimTurn, lines  # noqa: E402

import store  # noqa: E402
from holophyte.loop import review_round, stop  # noqa: E402
from holophyte.stages import record  # noqa: E402


class RequiredCheckTests(CheckRuns):
    def configure_required(self, stage, after, commands, config=UNTRIMMED):
        self.configure(config + check_table(stage, after, commands, "required"))

    def rounds(self):
        return [(rnd, verdict, json.loads(findings)) for rnd, verdict, findings
                in self.read("SELECT round, verdict, findings FROM reviewRounds"
                             " ORDER BY round")]

    def test_a_lint_failure_reaches_the_fix_and_the_fixed_candidate_merges(self):
        self.configure_required("lint", "implement", ["sh lint.sh"])
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
        self.configure_required("lint", "implement", [failure])
        self.loop(Commit("work"), APPROVE, first_fix, APPROVE, next_fix)
        self.assertEqual(terminal_record(), oracle)
        self.assertEqual([row[2][0]["path"] for row in self.rounds()], ["lint", "lint"])

    def test_timed_out_check_on_the_trimmed_head_blocks_the_first_round(self):
        self.configure_required(
            "tidy", "trim", ["tail -1 work.txt", "exec sleep 10"],
            config="[trim]\nbudget_min = 1\n[verify]\ntimeout_sec = 1\n")
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

    def test_an_unreadable_config_after_trim_preserves_the_failure(self):
        config = shlex.quote(str(self.project.config_path))
        commands = [f"printf '\\377' > {config}", "echo missing docs", "exit 19"]
        self.configure_required("docs", "trim", commands)
        fake, _ = self.loop(Commit("work"), APPROVE, Idle())

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertIn("missing docs", fake.turns[-1].goal)
        first, = self.rounds()
        self.assertEqual(first[:2], (1, "changes_requested"))
        self.assertEqual([finding["path"] for finding in first[2]], ["docs"])

    def test_passing_required_check_preserves_the_review_cycle(self):
        def cycle():
            with patch.dict(os.environ, GIT_DATES):
                fake, _ = self.loop(Commit("work"), REQUEST_CHANGES,
                                    Commit("fix"), APPROVE)
            self.assertEqual(fake.roles, ["implement", "review", "implement", "review"])
            self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
            ledger = []
            for kind, source, text in self.read(
                    "SELECT kind, source, text FROM ledger ORDER BY id"):
                if kind == "merge":
                    text = re.sub(r"(?m)^actual: \d+\.\d+ min", "actual: N min", text)
                ledger.append((kind, source, text))
            phases = self.read("SELECT summary FROM runEvents"
                               " WHERE kind = 'phase_change' ORDER BY seq")
            return [turn.goal for turn in fake.turns], self.rounds(), phases, ledger

        self.configure(UNTRIMMED)
        plain = cycle()

        self.fresh()
        self.configure_required("lint", "implement", ["true"])
        self.assertEqual(cycle(), plain)
        self.assertEqual([(row[4]["round"], row[2:4]) for row in self.attempts()
                          if row[0] == "lint"],
                         [(1, ("pass", "next")), (2, ("pass", "next"))])

    def test_pausing_a_resumed_fix_preserves_the_failed_check_for_review(self):
        self.configure_required("lint", "implement", ["test -f fixed"])
        self.loop(Commit("work"), PauseReply(self.db, APPROVE))
        stop.command(self.project, "KO-131", None, resume=True)

        def pause_before_review(conn, run_id, phase, **state):
            if phase == "reviewing":
                store.pause(conn, run_id, "inspect the candidate")
            stop.boundary(conn, run_id, phase, **state)

        with patch.object(review_round, "boundary", side_effect=pause_before_review):
            self.loop()
        self.assertEqual(self.read("SELECT resumePhase FROM runs ORDER BY id"),
                         [("addressing",), ("reviewing",)])
        stop.command(self.project, "KO-131", None, resume=True)
        fake, _ = self.loop(APPROVE, Commit("repair lint", "fixed", "ok\n"), APPROVE)

        self.assertEqual(fake.roles, ["review", "implement", "review"])
        self.assertIn("test -f fixed", fake.turns[1].goal)
        self.assertIn("repair lint", self.subjects())

    def resume_redacted_check(self, command, fix, *resume_turns):
        self.configure_required(
            "lint", "implement", ["echo fixture-credential", command],
            config=UNTRIMMED + '[extras]\napi_token = "fixture-credential"\n')
        self.loop(Commit("work"), REQUEST_CHANGES, PausingCommit(self.db, fix))
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "verifying")])
        stop.command(self.project, "KO-131", None, resume=True)
        with patch.dict(record.PINNED_CHECKS, clear=True):
            return self.loop(*resume_turns)[0]

    def test_a_resumed_redacted_check_keeps_the_pass_for_the_same_commit(self):
        fake = self.resume_redacted_check("true", Idle(), APPROVE)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])
        self.assertEqual(fake.roles, ["review"])
        lint, = [row for row in self.attempts(2) if row[0] == "lint"]
        self.assertEqual(lint[2:4], ("pass", "next"))
        self.assertEqual(lint[4]["round"], 2)
        self.assertEqual(self.read("SELECT id FROM stageAttempts"
                                   " WHERE runId = 1 AND stageId = 'lint'"),
                         [(lint[4]["reused_attempt"],)])

    def test_a_resumed_redacted_check_keeps_the_command_failure_for_the_fix(self):
        fake = self.resume_redacted_check("echo lint-diagnostic && exit 23",
                                          Idle(), APPROVE, Idle())
        first, second = self.rounds()
        original = next(item for item in first[2] if item["path"] == "lint")
        self.assertEqual(second, (2, "changes_requested", [original]))
        self.assertIn("exit 23", original["title"])
        self.assertIn("lint-diagnostic", fake.turns[-1].goal)
        self.assertNotIn("fixture-credential", fake.turns[-1].goal)

    def test_a_resumed_redacted_check_cannot_reuse_a_pass_for_a_different_commit(self):
        self.resume_redacted_check("true", Commit("fix"), APPROVE, Idle())
        before, = [row for row in self.attempts() if row[0] == "lint"]
        after, = [row for row in self.attempts(2) if row[0] == "lint"]
        self.assertNotEqual(before[1], after[1])
        self.assertEqual(before[2:4], ("pass", "next"))
        self.assertEqual(after[2:4], ("error", "on_fail"))
        self.assertEqual(self.rounds()[-1][1], "changes_requested")

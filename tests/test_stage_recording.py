"""The stage attempts the real loop records for a run's candidate stages:
implement, trim, verify and review, their commits, outcomes and edges, the
agent usage each owns, and a store that refuses every attempt write without
changing what the run does.

Run: python3 -m unittest discover -s tests -p 'test_stage_recording.py' -v
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    MALFORMED,
    REQUEST_CHANGES,
    Commit,
    FakeAgent,
    Idle,
    Reply,
    block_until_killed,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    UNTRIMMED,
    LoopFixture,
    StubProvider,
    a_task,
)
from test_not_reproduced import (  # noqa: E402 - after the sys.path insert
    REFUSED,
    REPRODUCED,
    Declare,
)
from test_reproduce_first import (  # noqa: E402 - after the sys.path insert
    PASSES,
    REPRODUCE,
    bug_task,
)

import holophyte.loop.review_round  # noqa: E402 - after the sys.path insert
import store  # noqa: E402 - after the sys.path insert above
import store.stages  # noqa: E402 - after the sys.path insert above
from holophyte.loop.gates import InfraFailure  # noqa: E402

TRIMMED = "[trim]\nbudget_min = 1\n"
# What each scripted turn reports, by its 1-based place in the script.
USAGE = {n: {"input_tokens": 1000 * n, "output_tokens": 100 * n,
             "cost_usd": n / 10} for n in range(1, 10)}


def lines(count):
    return "".join(f"line {n}\n" for n in range(count))


class Metered(FakeAgent):
    """The scripted agent, recording each turn's `agent_turn` usage the way
    the real `agent()` does when it is handed the run's store."""

    def __call__(self, target, role, goal, cwd, **kwargs):
        reply = super().__call__(target, role, goal, cwd, **kwargs)
        conn, run_id = kwargs.get("conn"), kwargs.get("run_id")
        if conn is not None and run_id is not None:
            n = len(self.turns)
            store.record_event(conn, run_id, "agent_turn", f"{role} turn ended",
                               level="detail", payload=json.dumps(
                                   {"role": role, "seconds": n,
                                    "usage": USAGE[n]}))
        return reply


class TrimTurn:
    """A trim turn committing the named passes."""

    role = "implement"

    def __init__(self, *commits):
        self.commits = commits

    def play(self, cwd, turn):
        for commit in self.commits:
            commit.play(cwd, turn)
        return "trimmed"


TWO_BLOCKERS = Reply(
    "- README.md:1 [p1] [correctness] the first blocker\n"
    "- README.md:1 [p2] [correctness] the second blocker\n"
    "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
    "VERDICT: REQUEST_CHANGES")


class CommitThenTimeOut(Commit):
    """An implement turn that commits, then runs past its budget."""

    def play(self, cwd, turn):
        super().play(cwd, turn)
        block_until_killed(cwd)


class RouteFails:
    """A trim turn whose route fails as infrastructure."""

    role = "implement"

    def play(self, cwd, turn):
        raise InfraFailure("the trimmer route did not start")


class AdversaryRouteFails:
    """An adversary turn whose route fails as infrastructure."""

    role = "adversary"

    def play(self, cwd, turn):
        raise InfraFailure("the adversary route did not start", "review_route")


class EditThenFail:
    """A fix turn that leaves an edit uncommitted, then its route fails."""

    role = "implement"

    def play(self, cwd, turn):
        (cwd / "half-done.txt").write_text("half done\n")
        raise InfraFailure("the implementer route fell over")


class LoseHead:
    """A fix turn that leaves HEAD naming a branch that does not exist."""

    role = "implement"

    def play(self, cwd, turn):
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/gone"],
                       cwd=cwd, check=True)
        return "moved on"


class StageRecordingTests(LoopFixture):
    def run_loop(self, config, *script, task=None):
        self.configure(config)
        return self.loop(fake=Metered(*script),
                         provider=StubProvider(task or a_task()))[0]

    def attempts(self):
        return [(stage, attempt, outcome, edge, json.loads(detail))
                for stage, attempt, outcome, edge, detail in self.read(
                    "SELECT stageId, attempt, outcome, edge, detail"
                    " FROM stageAttempts ORDER BY startedAt, id")]

    def shas(self, stage, attempt):
        (row,) = self.read(
            "SELECT inputSha, outputSha FROM stageAttempts"
            f" WHERE stageId = '{stage}' AND attempt = {attempt}")
        return row

    def outcomes(self):
        return [(stage, attempt, outcome, edge)
                for stage, attempt, outcome, edge, _ in self.attempts()]

    def test_a_fresh_approved_run_records_its_four_candidate_stages(self):
        self.run_loop(UNTRIMMED, Commit("work"), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        implemented = self.git("rev-parse", "main^2").strip()
        self.assertEqual(self.outcomes(), [
            ("implement", 1, "pass", "next"), ("trim", 1, "skipped", "next"),
            ("verify", 1, "pass", "next"), ("review", 1, "pass", "next")])
        details = {stage: detail for stage, _, _, _, detail in self.attempts()}
        self.assertEqual(details["trim"]["reason"], "[trim] enabled = false")
        self.assertEqual(
            {key: details["review"][key]
             for key in ("round", "decision", "adversary")},
            {"round": 1, "decision": "APPROVE", "adversary": "off"})
        self.assertEqual(self.shas("implement", 1), (self.base, implemented))
        self.assertEqual(self.shas("review", 1)[0], implemented)

    def test_a_request_for_changes_records_the_fix_and_the_second_round(self):
        self.run_loop(UNTRIMMED, Commit("work"), REQUEST_CHANGES,
                      Commit("fix"), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        fixed = self.git("rev-parse", "main^2").strip()
        implemented = self.git("rev-parse", "main^2^").strip()
        recorded = self.attempts()
        self.assertEqual(self.outcomes(), [
            ("implement", 1, "pass", "next"), ("trim", 1, "skipped", "next"),
            ("verify", 1, "pass", "next"), ("review", 1, "fail", "on_fail"),
            ("implement", 2, "pass", "next"), ("verify", 2, "pass", "next"),
            ("review", 2, "pass", "next")])
        self.assertEqual(recorded[4][4]["kind"], "fix")
        self.assertEqual(self.shas("verify", 2), (fixed, fixed))
        self.assertEqual(self.shas("review", 1)[0], implemented)
        self.assertEqual(self.shas("implement", 2), (implemented, fixed))
        self.assertEqual(self.shas("review", 2)[0], fixed)
        self.assertEqual(self.read(
            "SELECT agentSeconds, inputTokens, outputTokens, costUsd"
            " FROM stageAttempts WHERE stageId = 'implement' AND attempt = 2"),
            [(3, USAGE[3]["input_tokens"], USAGE[3]["output_tokens"],
              USAGE[3]["cost_usd"])])

    def test_a_trim_that_shortens_the_candidate_passes_with_the_trimmed_head(self):
        self.run_loop(TRIMMED, Commit("work", "work.txt", lines(60)),
                      TrimTurn(Commit("trim: delete", "work.txt", lines(40))),
                      APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        trimmed = self.git("rev-parse", "main^2").strip()
        self.assertEqual(self.git("log", "-1", "--format=%s", trimmed).strip(),
                         "trim: delete")
        self.assertIn(("trim", 1, "pass", "next"), self.outcomes())
        self.assertEqual(self.shas("trim", 1),
                         (self.git("rev-parse", f"{trimmed}^").strip(), trimmed))

    def test_a_trim_that_raises_after_moving_head_records_the_moved_head(self):
        record_event = store.record_event

        def refuse_trim(conn, run_id, kind, *args, **kwargs):
            if kind == "trim":
                raise sqlite3.OperationalError("database is locked")
            return record_event(conn, run_id, kind, *args, **kwargs)

        with patch.object(store, "record_event", side_effect=refuse_trim):
            self.run_loop(TRIMMED, Commit("work", "work.txt", lines(60)),
                          TrimTurn(Commit("trim: delete", "work.txt", lines(40))),
                          APPROVE)

        trimmed = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.git("log", "-1", "--format=%s", trimmed).strip(),
                         "trim: delete")
        self.assertEqual(self.read(
            "SELECT outcome, edge, inputSha, outputSha FROM stageAttempts"
            " WHERE stageId = 'trim'"),
            [("fail", "end", self.git("rev-parse", f"{trimmed}^").strip(),
              trimmed)])

    def test_a_trim_under_the_line_floor_is_skipped_with_that_reason(self):
        self.run_loop(TRIMMED, Commit("work", "work.txt", lines(49)), APPROVE)

        (stage, _, outcome, _, detail), = [
            attempt for attempt in self.attempts() if attempt[0] == "trim"]
        self.assertEqual(outcome, "skipped")
        self.assertEqual(detail["reason"],
                         "diff of 49 changed lines is under 50")

    def test_a_trim_whose_route_fails_closes_as_an_error_and_the_run_goes_on(self):
        self.run_loop(TRIMMED, Commit("work", "work.txt", lines(60)),
                      RouteFails(), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertIn(("trim", 1, "error", "next"), self.outcomes())

    def test_an_unreproduced_bug_records_its_trim_skipped(self):
        self.configure(TRIMMED)
        self.loop(REPRODUCE, PASSES, provider=StubProvider(bug_task("echo ok")))

        self.assertEqual(self.read("SELECT parkKind FROM runs"),
                         [("not_reproduced",)])
        (_, _, outcome, _, detail), = self.attempts()
        self.assertEqual((outcome, detail["reason"]),
                         ("skipped", "the bug was not reproduced"))

    def test_an_implementer_that_commits_then_times_out_keeps_its_commit(self):
        self.run_loop(UNTRIMMED, CommitThenTimeOut("work"))

        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "budget")])
        self.assertEqual(self.outcomes(), [("implement", 1, "fail", "end")])
        self.assertEqual(self.shas("implement", 1),
                         (self.base, self.git("rev-parse", BRANCH).strip()))

    def test_a_review_counts_the_reviewers_own_blockers(self):
        self.run_loop(UNTRIMMED, Commit("work"), TWO_BLOCKERS, Commit("fix"),
                      APPROVE)

        review = [detail for stage, attempt, _, _, detail in self.attempts()
                  if (stage, attempt) == ("review", 1)][0]
        self.assertEqual((review["decision"], review["findings"]),
                         ("REQUEST_CHANGES", 2))

    def test_a_reviewer_malformed_twice_ends_its_review_attempt_as_an_error(self):
        self.run_loop(UNTRIMMED, Commit("work"), MALFORMED, MALFORMED)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.outcomes()[-1], ("review", 1, "error", "end"))
        self.assertEqual(self.attempts()[-1][4]["decision"], "MALFORMED")

    def test_a_red_verify_before_review_fails_onto_the_review(self):
        task = dict(a_task(), verify="test -f fixed.txt")
        self.run_loop(UNTRIMMED, Commit("work"), APPROVE,
                      Commit("fix", "fixed.txt"), APPROVE, task=task)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(self.outcomes()[2:4], [
            ("verify", 1, "fail", "next"), ("review", 1, "fail", "on_fail")])


    def test_a_red_verify_on_the_fix_is_recorded_before_the_self_check(self):
        task = dict(a_task(), verify="! grep -q broken state.txt")
        self.run_loop(UNTRIMMED, Commit("work"), REQUEST_CHANGES,
                      Commit("fix", "state.txt", "broken\n"),
                      Commit("self-check", "state.txt", "ok\n"), APPROVE,
                      task=task)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(
            [(stage, attempt, outcome, detail.get("kind"))
             for stage, attempt, outcome, _, detail in
             self.attempts()][4:], [
                ("implement", 2, "pass", "fix"), ("verify", 2, "fail", None),
                ("implement", 3, "pass", "self_check"),
                ("verify", 3, "pass", None), ("review", 2, "pass", None)])
        fixed = self.git("rev-parse", "main^2^").strip()
        self.assertEqual(self.shas("verify", 2)[0], fixed)

    def test_a_verify_rerun_on_a_dirty_tree_at_the_same_head_is_recorded(self):
        task = dict(a_task(), verify="echo ran >> verify-runs.log")
        self.run_loop(UNTRIMMED, Commit("work"), REQUEST_CHANGES,
                      Commit("fix"), APPROVE, task=task)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        fixed = self.git("rev-parse", "main^2").strip()
        self.assertEqual(self.outcomes()[4:], [
            ("implement", 2, "pass", "next"), ("verify", 2, "pass", "next"),
            ("verify", 3, "pass", "next"), ("review", 2, "pass", "next")])
        self.assertEqual(self.shas("verify", 3), (fixed, fixed))

class TerminalReviewEdgeTests(LoopFixture):
    def run_loop(self, config, *script, task=None):
        self.configure(config)
        return self.loop(fake=Metered(*script),
                         provider=StubProvider(task or a_task()))[0]

    def reviews(self):
        return [(attempt, outcome, edge, json.loads(detail))
                for attempt, outcome, edge, detail in self.read(
                    "SELECT attempt, outcome, edge, detail FROM stageAttempts"
                    " WHERE stageId = 'review' ORDER BY attempt")]

    def stale(self):
        return Reply(f"CRITERION 1: met \u2014 approval at {self.base[:12]};"
                     " tests/test_thing.py::test_it_works\nVERDICT: APPROVE")

    def test_a_second_stale_only_approval_ends_the_run(self):
        work = Commit("work", "tests/test_thing.py",
                      "def test_it_works():\n    assert True\n")
        self.run_loop(UNTRIMMED, work, self.stale(), self.stale())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual([review[:3] for review in self.reviews()],
                         [(1, "fail", "again"), (2, "fail", "end")])

    def test_a_fix_turn_the_run_cap_refuses_ends_the_run(self):
        self.configure("[supervisor]\nrun_cap = 3\n" + UNTRIMMED)
        real = holophyte.loop.review_round.set_phase

        def aged(conn, run_id, phase, note=None):
            if phase == "reviewing":
                with store.transaction(conn):
                    conn.execute("UPDATE runs SET workingMs = ? WHERE id = ?",
                                 (70 * 60 * 1000, run_id))
            return real(conn, run_id, phase, note)

        with patch.object(holophyte.loop.review_round, "set_phase", aged):
            self.loop(fake=Metered(Commit("work"), REQUEST_CHANGES),
                      provider=StubProvider(dict(a_task(), budget_min=30)))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual([review[:3] for review in self.reviews()],
                         [(1, "fail", "end")])

    def test_an_adversary_failure_keeps_the_primary_decision_and_count(self):
        self.run_loop("[review]\nadversary = true\n" + UNTRIMMED,
                      Commit("work", "poetry.lock"), TWO_BLOCKERS,
                      AdversaryRouteFails())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        (_, outcome, edge, detail), = self.reviews()
        self.assertEqual((outcome, edge), ("error", "end"))
        self.assertEqual(
            (detail["decision"], detail["findings"], detail["adversary"]),
            ("REQUEST_CHANGES", 2, "ran"))

    def test_an_approval_after_a_rerun_that_timed_out_still_routes_to_a_fix(self):
        marker = self.target.parent / "first-run"
        task = dict(a_task(), verify=f"test -e {marker} || {{ touch {marker};"
                    " exit 1; }; sleep 5")
        self.run_loop("[verify]\ntimeout_sec = 1\n" + UNTRIMMED,
                      Commit("work"), APPROVE, Idle(), task=task)

        (detail,) = [json.loads(detail) for (detail,) in self.read(
            "SELECT detail FROM stageAttempts WHERE stageId = 'verify'"
            " AND attempt = 1")]
        self.assertIs(detail["timed_out"], True)
        self.assertEqual([summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'phase_change'"
            " ORDER BY seq")], [
            f"claimed -> working: cutting {BRANCH} and implementing",
            "working -> verifying: round 1: verify before review",
            "verifying -> reviewing: round 1 review",
            "reviewing -> addressing: round 1: addressing findings",
            "addressing -> failed: run ended, outcome failed"])
        self.assertEqual([(kind, text.splitlines()[0]) for kind, text in
                          self.read("SELECT kind, text FROM ledger ORDER BY id")],
                         [("note", "Review cap 2 for 1 changed lines"),
                          ("round", "Round 1: REQUEST_CHANGES -> fix round")])
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "fix_no_progress")])
        self.assertEqual([review[:3] for review in self.reviews()],
                         [(1, "fail", "on_fail")])

    def test_a_verify_that_commits_records_the_head_it_left(self):
        task = dict(a_task(), verify="git commit -q --allow-empty -m checked")
        self.run_loop(UNTRIMMED, Commit("work"), APPROVE, task=task)

        (input_sha, output_sha), = self.read(
            "SELECT inputSha, outputSha FROM stageAttempts"
            " WHERE stageId = 'verify' AND attempt = 1")
        self.assertEqual(self.git("log", "-1", "--format=%s", output_sha).strip(),
                         "checked")
        self.assertEqual(self.git("rev-parse", f"{output_sha}^").strip(),
                         input_sha)

    def test_a_fix_that_makes_no_progress_ends_its_attempt(self):
        self.run_loop(UNTRIMMED, Commit("work"), REQUEST_CHANGES, Idle())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.read(
            "SELECT outcome, edge FROM stageAttempts"
            " WHERE stageId = 'implement' AND attempt = 2"), [("fail", "end")])

    def test_a_verify_printing_a_timeout_line_is_not_a_timeout(self):
        task = dict(a_task(), verify="echo '[verify] FAILED: verify timed out"
                    " after 1s'; exit 1")
        self.run_loop(UNTRIMMED, Commit("work"), APPROVE, Idle(), task=task)

        (detail,) = [json.loads(detail) for (detail,) in self.read(
            "SELECT detail FROM stageAttempts WHERE stageId = 'verify'"
            " AND attempt = 1")]
        self.assertIs(detail["timed_out"], False)

    def test_a_fix_turn_that_raises_records_the_head_it_left(self):
        self.run_loop(UNTRIMMED, Commit("work"), REQUEST_CHANGES,
                      EditThenFail())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.read(
            "SELECT outcome, edge, outputSha FROM stageAttempts"
            " WHERE stageId = 'implement' AND attempt = 2"),
            [("error", "end", self.git("rev-parse", BRANCH).strip())])

    def test_an_unreadable_head_after_a_fix_keeps_the_fix_round_ledger(self):
        self.run_loop(UNTRIMMED, Commit("work"), REQUEST_CHANGES, LoseHead())

        (text,), = self.read("SELECT text FROM ledger WHERE text LIKE"
                             " 'Round 1: REQUEST_CHANGES -> fix round%'")
        self.assertIn("moved on", text)
        writes = [json.loads(payload)["write"] for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'stage_record_failed'")]
        self.assertEqual(writes, ["stage_head"])

    def test_the_evidence_checks_repair_turn_records_no_attempt(self):
        self.run_loop(UNTRIMMED, Declare("test the modal"), REFUSED,
                      Declare("open the modal in the test"), REPRODUCED)

        self.assertEqual(self.read("SELECT parkKind FROM runs"),
                         [("not_reproduced",)])
        self.assertEqual(self.read(
            "SELECT stageId, attempt, outcome FROM stageAttempts ORDER BY id"),
            [("implement", 1, "pass"), ("trim", 1, "skipped")])

    def test_an_idle_first_turn_records_its_attempt_without_a_refusal(self):
        self.run_loop(UNTRIMMED, Idle())

        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "no_commits")])
        self.assertEqual(self.read(
            "SELECT stageId, outcome, edge, inputSha, outputSha"
            " FROM stageAttempts"),
            [("implement", "fail", "end", self.base, self.base)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM runEvents"
                                   " WHERE kind = 'stage_record_failed'"),
                         [(0,)])


class RefusedStageWritesTests(LoopFixture):
    SCRIPT = (Commit("work"), REQUEST_CHANGES, Commit("fix"), APPROVE)

    def walked(self):
        phases = [summary for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'phase_change'"
            " ORDER BY seq")]
        entries = [(kind, re.sub(r"actual: [\d.]+ min", "actual: N min", text))
                   for kind, text in self.read(
                       "SELECT kind, text FROM ledger ORDER BY id")]
        return phases, entries, self.read("SELECT outcome FROM runs")

    def test_a_store_refusing_every_stage_write_leaves_the_run_unchanged(self):
        self.configure(UNTRIMMED)
        self.loop(*self.SCRIPT)
        recorded = self.walked()
        self.assertTrue(self.read("SELECT COUNT(*) FROM stageAttempts")[0][0])
        self.fresh_fixture()

        refused = sqlite3.OperationalError("database is locked")
        with patch.object(store.stages, "open_stage", side_effect=refused), \
                patch.object(store.stages, "close_stage", side_effect=refused), \
                patch.object(store.stages, "skip_stage", side_effect=refused):
            self.configure(UNTRIMMED)
            self.loop(*self.SCRIPT)

        self.assertEqual(self.walked(), recorded)
        self.assertEqual(recorded[2], [("merged",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM stageAttempts"), [(0,)])
        writes = {json.loads(payload)["write"] for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'stage_record_failed'"
            " AND level = 'detail'")}
        self.assertEqual(writes, {"open_stage", "skip_stage"})

    def test_a_store_refusing_every_close_leaves_the_run_unchanged(self):
        self.configure(UNTRIMMED)
        self.loop(*self.SCRIPT)
        recorded = self.walked()
        self.fresh_fixture()

        refused = sqlite3.OperationalError("database is locked")
        with patch.object(store.stages, "close_stage", side_effect=refused):
            self.configure(UNTRIMMED)
            self.loop(*self.SCRIPT)

        self.assertEqual(self.walked(), recorded)
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM stageAttempts WHERE outcome IS NOT NULL"
            " AND outcome != 'skipped'"), [(0,)])
        opened = self.read("SELECT COUNT(*) FROM stageAttempts"
                           " WHERE outcome IS NULL")[0][0]
        writes = [json.loads(payload)["write"] for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'stage_record_failed'"
            " AND level = 'detail'")]
        self.assertEqual(writes, ["close_stage"] * opened)
        self.assertTrue(opened)

    def fresh_fixture(self):
        self.doCleanups()
        self.setUp()


if __name__ == "__main__":
    unittest.main()

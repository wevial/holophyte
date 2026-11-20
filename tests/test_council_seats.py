"""The `[[review.council]]` review round: its seats in the config and the
pinned pipeline, each seat run in parallel on the round's prompt and stored,
the strictest-wins merge, a seat that fails, the additive migration, and a
loop without a council left as it was.

Run: python3 -m unittest discover -s tests -p 'test_council_seats.py' -v
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    IMPLEMENT,
    REQUEST_CHANGES,
    SCOPE_LIST_RE,
    Attack,
    Commit,
    Consolidate,
    FakeAgent,
    ScriptError,
    _git,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    UNTRIMMED,
    LoopFixture,
)

import holophyte.review.council  # noqa: E402 - after the sys.path insert
import store  # noqa: E402 - after the sys.path insert above
import store.council  # noqa: E402 - after the sys.path insert above
import store.schema  # noqa: E402 - after the sys.path insert above
from holophyte.agents import roles  # noqa: E402
from holophyte.config.checks import check_config  # noqa: E402
from holophyte.loop.gates import InfraFailure  # noqa: E402
from holophyte.review import review_runner  # noqa: E402

ASTRA = ("codex", "gpt-6-astra", "high")
OPUS = ("claude", "opus", "high")
LUNA = ("codex", "gpt-6-luna", "medium")
USAGE = {"gpt-6-astra": {"input_tokens": 1200, "output_tokens": 120},
         "opus": {"input_tokens": 3400, "output_tokens": 340},
         "gpt-6-luna": {"input_tokens": 500, "output_tokens": 50}}
SHARED = ("README.md", 1, "the base line is never read back")
OTHER = ("tests/test_thing.py", 2, "the test asserts nothing")
FIXED_DATES = {"GIT_AUTHOR_DATE": "2026-01-02T03:04:05+00:00",
               "GIT_COMMITTER_DATE": "2026-01-02T03:04:05+00:00"}


def council(*seats):
    return UNTRIMMED + "".join(
        f'[[review.council]]\nharness = "{harness}"\nmodel = "{model}"\n'
        f'effort = "{effort}"\n' for harness, model, effort in seats)


@dataclass(frozen=True)
class Answer:
    """A seat's schema-checked reply, answering the round's scope question."""

    verdict: str
    score: int
    findings: tuple = ()
    criterion: str = "met"

    def value(self, goal):
        listed = SCOPE_LIST_RE.search(goal)
        return {
            "verdict": self.verdict, "score": self.score,
            "criteria": [{"id": "C1", "status": self.criterion,
                          "reason": "the named test runs the thing",
                          "witness": "tests/test_thing.py::test_it_works"}],
            "findings": [{"path": path, "line": line, "severity": "p1",
                          "category": "correctness", "target": "code",
                          "message": message}
                         for path, line, message in self.findings],
            "follow_ups": [],
            "scope": [{"path": path, "status": "needed",
                       "reason": "the scripted work"}
                      for path in (json.loads(listed[1]) if listed else [])],
            "refuted": []}

    def play(self, goal):
        return json.dumps(self.value(goal))


@dataclass(frozen=True)
class Garbled:
    """A seat reply that is no JSON object at all."""

    def play(self, goal):
        return "Looks fine to me.\nVERDICT: APPROVE"


@dataclass(frozen=True)
class RouteDown:
    """A seat whose review container fails to run."""

    def play(self, goal):
        raise InfraFailure("reviewer route failed for review: the container "
                           "exited 125", "review_route")


@dataclass(frozen=True)
class SeatTurn:
    harness: str
    model: str
    goal: str
    base_sha: str
    candidate_sha: str


class CouncilAgent(FakeAgent):
    """The scripted agent, answering each council seat's turn from that
    seat's own queue, keyed by model, and recording its `agent_turn` usage
    the way the real `agent()` does."""

    def __init__(self, *script, seats=None, barrier=None):
        super().__init__(*script)
        self.seat_steps = {model: list(steps)
                           for model, steps in (seats or {}).items()}
        self.seat_turns = []
        self.barrier = barrier

    def __call__(self, target, role, goal, cwd, **kwargs):
        route = kwargs.get("family_route")
        if route is None:
            return super().__call__(target, role, goal, cwd, **kwargs)
        with self.lock:
            steps = self.seat_steps.get(route["model"])
            if role != "review" or not steps:
                raise ScriptError(f"no seat step for a {role!r} turn on "
                                  f"{route['model']}")
            step = steps.pop(0)
            self.seat_turns.append(SeatTurn(
                route["harness"], route["model"], str(goal),
                kwargs.get("base_sha"), kwargs.get("candidate_sha")))
        if self.barrier is not None:
            self.barrier.wait()
        reply = step.play(str(goal))
        conn, run_id = kwargs.get("conn"), kwargs.get("run_id")
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "agent_turn", f"{role} turn ended",
                               level="detail", payload=json.dumps(
                                   {"role": role, "seconds": 1,
                                    "usage": USAGE[route["model"]]}))
        return reply


class CouncilFixture(LoopFixture):
    def run_council(self, config, *script, seats, barrier=None):
        self.configure(config)
        fake = CouncilAgent(*script, seats=seats, barrier=barrier)
        return self.loop(fake=fake)[0]

    def seats(self, *columns):
        return self.read(f"SELECT {', '.join(columns)} FROM councilSeats"
                         " ORDER BY round, seat")

    def findings(self, rnd):
        [(text,)] = self.read(
            f"SELECT findings FROM reviewRounds WHERE round = {rnd}")
        return json.loads(text)


class CouncilConfigTests(CouncilFixture):
    def test_a_two_seat_council_loads_and_the_pinned_review_stage_lists_it(self):
        fake = self.run_council(council(ASTRA, OPUS), Commit("work"), seats={
            "gpt-6-astra": [Answer("APPROVE", 9)],
            "opus": [Answer("APPROVE", 8)]})

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual({(turn.harness, turn.model) for turn in fake.seat_turns},
                         {("codex", "gpt-6-astra"), ("claude", "opus")})
        [(document,)] = self.read(
            "SELECT p.document FROM pipelines p JOIN runs r"
            " ON r.pipelineId = p.id")
        [review] = [stage for stage in json.loads(document)["stages"]
                    if stage["id"] == "review"]
        self.assertEqual(review["council"], [
            {"harness": "codex", "model": "gpt-6-astra", "effort": "high"},
            {"harness": "claude", "model": "opus", "effort": "high"}])

    def test_one_seat_an_unknown_harness_or_an_empty_model_stops_the_load(self):
        cases = [
            (council(ASTRA), "[[review.council]] entry 1"),
            (council(ASTRA, ("gemini", "pro", "high")),
             "[[review.council]] entry 2: harness"),
            (council(("codex", "", "high"), OPUS),
             "[[review.council]] entry 1: model")]
        for config, named in cases:
            with self.subTest(named=named):
                self.configure(config)
                with self.assertRaises(SystemExit) as raised:
                    check_config(self.project)
                self.assertIn(named, str(raised.exception))


class CouncilRoundTests(CouncilFixture):
    def test_both_seats_run_together_on_one_range_and_prompt_and_are_stored(self):
        barrier = threading.Barrier(2, timeout=10)
        fake = self.run_council(
            council(ASTRA, OPUS), Commit("work"), barrier=barrier, seats={
                "gpt-6-astra": [Answer("APPROVE", 9)],
                "opus": [Answer("APPROVE", 7)]})

        self.assertFalse(barrier.broken)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        first, second = fake.seat_turns
        self.assertEqual((first.base_sha, first.candidate_sha, first.goal),
                         (second.base_sha, second.candidate_sha, second.goal))
        self.assertEqual(first.candidate_sha,
                         self.git("rev-parse", "main^2").strip())
        self.assertIn("score", first.goal)
        rows = self.seats("round", "seat", "route", "status", "verdict",
                          "score", "reply", "seconds", "startedAt", "endedAt")
        self.assertEqual([row[:6] for row in rows], [
            (1, 1, "codex gpt-6-astra", "answered", "APPROVE", 9),
            (1, 2, "claude opus", "answered", "APPROVE", 7)])
        for (*_, reply, seconds, started, ended), turn, score in zip(
                rows, (first, second), (9, 7)):
            self.assertEqual(json.loads(reply),
                             Answer("APPROVE", score).value(turn.goal))
            self.assertIsNotNone(seconds)
            self.assertLessEqual(started, ended)
        self.assertEqual(self.read(
            "SELECT inputTokens, outputTokens FROM stageAttempts"
            " WHERE stageId = 'review'"), [(4600, 460)])

    def test_an_approval_and_a_request_merge_to_changes_with_the_union(self):
        fake = self.run_council(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"), seats={
                "gpt-6-astra": [Answer("APPROVE", 8, (SHARED,)),
                                Answer("APPROVE", 9)],
                "opus": [Answer("REQUEST_CHANGES", 4, (SHARED, OTHER)),
                         Answer("APPROVE", 9)]})

        self.assertEqual(self.read(
            "SELECT round, verdict FROM reviewRounds ORDER BY round"),
            [(1, "changes_requested"), (2, "pass")])
        self.assertEqual(sorted((f["path"], f["line"]) for f in self.findings(1)),
                         [("README.md", 1), ("tests/test_thing.py", 2)])
        fix = [turn for turn in fake.turns if turn.role == IMPLEMENT][1]
        self.assertEqual(fix.goal.count(SHARED[2]), 1)
        self.assertIn(OTHER[2], fix.goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_criterion_one_approving_seat_leaves_unwitnessed_is_not_met(self):
        self.run_council(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"), seats={
                "gpt-6-astra": [Answer("APPROVE", 9), Answer("APPROVE", 9)],
                "opus": [Answer("APPROVE", 8, criterion="unwitnessed"),
                         Answer("APPROVE", 9)]})

        [(verdict, reply)] = self.read(
            "SELECT verdict, reply FROM reviewRounds WHERE round = 1")
        self.assertEqual(verdict, "changes_requested")
        self.assertEqual(json.loads(reply)["verdict"], "REQUEST_CHANGES")
        self.assertEqual([c["status"] for c in json.loads(reply)["criteria"]],
                         ["unwitnessed"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


class CouncilSeatFailureTests(CouncilFixture):
    def test_a_failed_route_leaves_its_seat_missing_and_the_others_decide(self):
        fake = self.run_council(
            council(ASTRA, OPUS, LUNA), Commit("work"), seats={
                "gpt-6-astra": [Answer("APPROVE", 9)],
                "opus": [RouteDown()],
                "gpt-6-luna": [Answer("APPROVE", 8)]})

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual(sorted(turn.model for turn in fake.seat_turns),
                         ["gpt-6-astra", "gpt-6-luna", "opus"])
        rows = self.seats("seat", "route", "status", "score", "reply", "reason")
        self.assertEqual([row[:3] for row in rows], [
            (1, "codex gpt-6-astra", "answered"),
            (2, "claude opus", "missing"),
            (3, "codex gpt-6-luna", "answered")])
        self.assertEqual(rows[1][3:5], (None, None))
        self.assertIn("the container exited 125", rows[1][5])
        self.assertEqual(self.read("SELECT reviewerModel FROM reviewRounds"),
                         [("council codex gpt-6-astra, codex gpt-6-luna",)])

    def test_a_seat_unreadable_twice_fails_a_two_seat_round_as_review_route(self):
        fake = self.run_council(council(ASTRA, OPUS), Commit("work"), seats={
            "gpt-6-astra": [Answer("APPROVE", 9)],
            "opus": [Garbled(), Garbled()]})

        [(outcome, kind, reason)] = self.read(
            "SELECT outcome, failureKind, outcomeReason FROM runs")
        self.assertEqual((outcome, kind), ("failed", "review_route"))
        candidate = _git(self.target, "rev-parse", BRANCH)
        self.assertIn(f"candidate preserved at {candidate}", reason)
        self.assertEqual(self.seats("seat", "status", "reason"), [
            (1, "answered", None),
            (2, "unreadable", "reply failed the council reply schema twice")])
        retried = [turn.goal for turn in fake.seat_turns if turn.model == "opus"]
        self.assertEqual(len(retried), 2)
        self.assertIn("Your previous reply had no clean terminal verdict",
                      retried[1])
        self.assertEqual(self.read("SELECT COUNT(*) FROM reviewRounds"), [(0,)])

    def test_a_seat_route_failure_switches_to_no_configured_fallback(self):
        self.configure('[agents]\nreview_fallback_model = "gpt-6-luna"\n'
                       'review_fallback_effort = "medium"\n')
        with patch.object(review_runner, "run_review", side_effect=(
                review_runner.ReviewBoundaryError("container exited 125"))) \
                as run_review, \
                patch.object(roles, "activate_fallback") as activate, \
                self.assertRaises(InfraFailure) as raised:
            roles.agent(self.project, "review", "goal", self.target,
                        base_sha=self.base, candidate_sha=self.base,
                        family_route={"harness": "codex",
                                      "model": "gpt-6-astra", "effort": "high",
                                      "credential": None})

        self.assertEqual(raised.exception.failure_kind, "review_route")
        [call] = run_review.call_args_list
        self.assertEqual((call.kwargs["harness"], call.kwargs["model"]),
                         ("codex", "gpt-6-astra"))
        activate.assert_not_called()


    def test_a_seat_turn_records_an_agent_turn_under_role_review(self):
        self.configure("")
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, "team-1", str(self.target))
        ticket = store.mirror_ticket(conn, project, linear_issue_id="i-1",
                                     linear_identifier="KO-1", title="t")
        run = store.claim(conn, project, ticket)
        reply = Answer("APPROVE", 9).play("")
        with patch.object(review_runner, "run_review", return_value=reply):
            answered = roles.agent(
                self.project, "review", "goal", self.target,
                base_sha=self.base, candidate_sha=self.base, conn=conn,
                run_id=run, family_route={"harness": "claude", "model": "opus",
                                          "effort": "high", "credential": None})

        self.assertEqual(str(answered), reply)
        [(payload,)] = conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'agent_turn'").fetchall()
        turn = json.loads(payload)
        self.assertEqual((turn["role"], turn["label"], turn["route"]),
                         ("review", "claude opus", "primary"))


class CouncilMigrationTests(unittest.TestCase):
    def test_a_previous_version_store_gains_the_council_seats_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = store.open(path)
            project = store.ensure_project(conn, "team-1", "/repo-1")
            ticket = store.mirror_ticket(conn, project, linear_issue_id="i-1",
                                         linear_identifier="KO-1", title="t")
            run = store.claim(conn, project, ticket)
            conn.close()
            raw = sqlite3.connect(path)
            raw.executescript("DROP TABLE councilSeats;\n"
                              "PRAGMA user_version = 73;\n")
            raw.close()

            conn = store.open(path)
            self.addCleanup(conn.close)

            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                             (store.schema.SCHEMA_VERSION,))
            self.assertEqual(store.schema.SCHEMA_VERSION, 74)
            self.assertEqual(store.schema.READABLE_FROM, 50)
            store.council.record_seat(conn, run, 1, 1, "codex gpt-6-astra",
                                      "missing", started_at=1, ended_at=2,
                                      reason="the container exited 125")
            self.assertEqual(conn.execute(
                "SELECT seat, status, reason FROM councilSeats").fetchall(),
                [(1, "missing", "the container exited 125")])
            with self.assertRaises(sqlite3.IntegrityError):
                store.council.record_seat(conn, run, 1, 2, "claude opus",
                                          "absent", started_at=1, ended_at=2)


ADVERSARY_ON = UNTRIMMED + "[review]\nadversary = true\n"
CONCERN = ("- src/app.py:4 [p1] a symlink may slip by\nEVIDENCE: concern\n"
           "ADVERSARY: DONE")
MINUTES_RE = re.compile(r"\d+(?:\.\d+)? ?min")


@dataclass
class Change(Commit):
    """An implementer turn that writes `path`, making its directories."""

    def play(self, cwd, turn):
        (cwd / self.path).parent.mkdir(parents=True, exist_ok=True)
        return super().play(cwd, turn)


class UnsetCouncilTests(unittest.TestCase):
    def walk(self, *patches):
        fixture = CouncilFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.configure(ADVERSARY_ON)
        with patch.dict(os.environ, FIXED_DATES):
            for started in patches:
                started.start()
            try:
                fake, _ = fixture.loop(
                    Change("change", "poetry.lock", "locked\n"),
                    REQUEST_CHANGES, Attack(CONCERN), Consolidate(),
                    Change("fix", "src/app.py", "fixed\n"), APPROVE)
            finally:
                for started in patches:
                    started.stop()
        return {
            "prompts": [turn.goal for turn in fake.turns
                        if turn.role == "review"],
            "seats": fixture.read("SELECT COUNT(*) FROM councilSeats"),
            "rounds": fixture.read(
                "SELECT runId, round, verificationResults, verdict, findings,"
                " findingsFingerprint, reviewerModel, reply FROM reviewRounds"
                " ORDER BY round"),
            "phases": fixture.read("SELECT summary FROM runEvents"
                                   " WHERE kind = 'phase_change' ORDER BY seq"),
            "ledger": [(kind, MINUTES_RE.sub("N min", text))
                       for kind, text in fixture.read(
                           "SELECT kind, text FROM ledger ORDER BY id")],
            "outcome": fixture.read("SELECT outcome FROM runs")}

    def test_a_raising_council_changes_nothing_while_the_table_is_unset(self):
        plain = self.walk()
        raising = self.walk(patch.object(
            holophyte.review.council, "run_round",
            side_effect=AssertionError("a council round ran while unset")))

        self.assertEqual(plain["outcome"], [("merged",)])
        self.assertEqual(plain["seats"], [(0,)])
        self.assertEqual(len(plain["prompts"]), 2)
        for key in plain:
            with self.subTest(key=key):
                self.assertEqual(raising[key], plain[key])


if __name__ == "__main__":
    unittest.main()

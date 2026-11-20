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
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_review_runner  # noqa: E402 - after the sys.path insert
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
    StubProvider,
)

import holophyte.review.council  # noqa: E402 - after the sys.path insert
import store  # noqa: E402 - after the sys.path insert above
import store.council  # noqa: E402 - after the sys.path insert above
import store.schema  # noqa: E402 - after the sys.path insert above
import store.stages  # noqa: E402 - after the sys.path insert above
from holophyte.agents import fallback, roles  # noqa: E402
from holophyte.agents.agent_routes import reset, routes  # noqa: E402
from holophyte.agents.probes import REVIEW_PROBE_GOAL  # noqa: E402
from holophyte.config.checks import check_config  # noqa: E402
from holophyte.config.review_settings import CouncilSeat  # noqa: E402
from holophyte.loop import pool  # noqa: E402
from holophyte.loop.gates import InfraFailure  # noqa: E402
from holophyte.redact import REDACTED, values_held  # noqa: E402
from holophyte.review import adversary, review_runner  # noqa: E402

ASTRA = ("codex", "gpt-6-astra", "high")
OPUS = ("claude", "opus", "high")
LUNA = ("codex", "gpt-6-luna", "medium")
USAGE = {"gpt-6-astra": {"input_tokens": 1200, "output_tokens": 120},
         "opus": {"input_tokens": 3400, "output_tokens": 340},
         "gpt-6-luna": {"input_tokens": 500, "output_tokens": 50}}
SHARED = ("README.md", 1, "the base line is never read back")
OTHER = ("tests/test_thing.py", 2, "the test asserts nothing")
CONSOLIDATOR = "You are the READ-ONLY consolidator of a review council."
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
    criterion: str | None = "met"
    witness: str = "tests/test_thing.py::test_it_works"

    def value(self, goal):
        listed = SCOPE_LIST_RE.search(goal)
        return {
            "verdict": self.verdict, "score": self.score,
            "criteria": [{"id": "C1", "status": self.criterion,
                          "reason": "the named test runs the thing",
                          "witness": self.witness}] if self.criterion else [],
            "findings": [{"path": path, "line": line,
                          "severity": (severity or ["p1"])[0],
                          "category": "correctness", "target": "code",
                          "message": message}
                         for path, line, message, *severity in self.findings],
            "follow_ups": [],
            "scope": [{"path": path, "status": "needed",
                       "reason": "the scripted work"}
                      for path in (json.loads(listed[1]) if listed else [])],
            "refuted": []}

    def play(self, goal):
        return json.dumps(self.value(goal))


@dataclass(frozen=True)
class OutOfRange:
    """A well-formed seat reply whose score the council schema refuses."""

    def play(self, goal):
        return json.dumps(dict(Answer("APPROVE", 9).value(goal), score=11))


@dataclass(frozen=True)
class RouteDown:
    """A seat whose review container exits non-zero."""

    def play(self, goal):
        raise review_runner.ReviewBoundaryError("the container exited 125")


@dataclass(frozen=True)
class SeatTurn:
    harness: str
    model: str
    goal: str
    base_sha: str
    candidate_sha: str


def provider_output(harness, text, usage):
    """What the review container prints for a reply, in its harness's shape."""
    if harness == "codex":
        return "\n".join(json.dumps(event) for event in (
            {"type": "item.completed",
             "item": {"type": "command_execution", "exit_code": 0}},
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": text}},
            {"type": "turn.completed", "usage": usage}))
    return json.dumps({"type": "result", "subtype": "success",
                       "is_error": False, "result": text, "usage": usage})


class Container:
    """The review container: it answers a startup probe with the checkout's
    commit, or fails to start for a model that is down, fails a council
    consolidator's turn so the round takes the strictest-wins merge, and
    answers each seat's turn from that seat's own queue, keyed by model, as
    its harness prints it."""

    def __init__(self, seats=None, down=(), barrier=None):
        self.steps = {model: list(steps)
                      for model, steps in (seats or {}).items()}
        self.down, self.barrier = set(down), barrier
        self.probed, self.seat_turns = [], []
        self.lock = threading.Lock()

    def __call__(self, **turn):
        harness, model = turn["harness"], turn["model"]
        if turn["prompt"] == REVIEW_PROBE_GOAL:
            self.probed.append(model)
            if model in self.down:
                raise review_runner.ReviewBoundaryError(
                    f"{model} is unreachable")
            return f"ready {turn['candidate_sha']}"
        if turn["prompt"].startswith(CONSOLIDATOR):
            raise review_runner.ReviewBoundaryError("the consolidator is down")
        with self.lock:
            steps = self.steps.get(model)
            if not steps:
                raise ScriptError(f"no seat step for a turn on {model}")
            step = steps.pop(0)
            self.seat_turns.append(SeatTurn(
                harness, model, turn["prompt"], turn["base_sha"],
                turn["candidate_sha"]))
        if self.barrier is not None:
            self.barrier.wait()
        return review_runner.PARSERS[harness](provider_output(
            harness, step.play(turn["prompt"]), USAGE[model]), None)[0]


class CouncilAgent(FakeAgent):
    """The scripted agent, handing each council seat's turn to the real
    `agent()`."""

    def __call__(self, target, role, goal, cwd, **kwargs):
        if kwargs.get("family_route") is None:
            return super().__call__(target, role, goal, cwd, **kwargs)
        return roles.agent(target, role, goal, cwd, **kwargs)


class CouncilFixture(LoopFixture):
    def run_council(self, config, *script, seats, barrier=None, down=()):
        self.configure(config)
        self.container = Container(seats, down, barrier)
        fake = CouncilAgent(*script)
        fake.seat_turns = self.container.seat_turns
        with patch.object(review_runner, "run_review", self.container):
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
        fake = self.run_council(
            council(ASTRA, ("claude", "opus", "medium")), Commit("work"),
            seats={"gpt-6-astra": [Answer("APPROVE", 9)],
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
            {"harness": "claude", "model": "opus", "effort": "medium"}])

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
        self.assertEqual(first.base_sha, self.base)
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
        self.assertEqual(self.findings(1), [])
        self.assertEqual(sorted(
            (turn["role"], turn["label"]) for (turn,) in (
                (json.loads(payload),) for (payload,) in self.read(
                    "SELECT payload FROM runEvents WHERE kind = 'agent_turn'"))
            if turn["role"] == "review"),
            [("review", "claude opus"), ("review", "codex gpt-6-astra")])

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

    def test_a_criterion_every_seat_omits_requests_changes_and_reaches_the_fix(self):
        fake = self.run_council(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"), seats={
                "gpt-6-astra": [Answer("APPROVE", 9, criterion=None),
                                Answer("APPROVE", 9)],
                "opus": [Answer("APPROVE", 8, criterion=None),
                         Answer("APPROVE", 9)]})

        [(verdict, reply)] = self.read(
            "SELECT verdict, reply FROM reviewRounds WHERE round = 1")
        self.assertEqual(verdict, "changes_requested")
        self.assertEqual(json.loads(reply)["verdict"], "REQUEST_CHANGES")
        fix = [turn for turn in fake.turns if turn.role == IMPLEMENT][1]
        self.assertIn("CRITERION C1: unwitnessed", fix.goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_met_claim_naming_no_real_test_fails_in_either_seat_order(self):
        real = Answer("APPROVE", 9).value("")
        unreal = Answer("APPROVE", 9,
                        witness="tests/test_thing.py::test_never_written"
                        ).value("")
        criteria = [(1, "Given the thing, when it runs, then it works")]
        for order in ((real, unreal), (unreal, real)):
            with self.subTest(first=order[0]["criteria"][0]["witness"]):
                value = holophyte.review.council.merged(
                    list(order), criteria, self.target, self.base)
                self.assertEqual(value["verdict"], "REQUEST_CHANGES")
                self.assertEqual([c["status"] for c in value["criteria"]],
                                 ["unwitnessed"])

    def test_two_approving_seats_store_each_of_their_findings_on_the_pass(self):
        self.run_council(council(ASTRA, OPUS), Commit("work"), seats={
            "gpt-6-astra": [Answer("APPROVE", 9, (SHARED,))],
            "opus": [Answer("APPROVE", 8, (OTHER,))]})

        self.assertEqual(self.read("SELECT verdict FROM reviewRounds"),
                         [("pass",)])
        self.assertEqual(sorted((f["path"], f["line"]) for f in self.findings(1)),
                         [("README.md", 1), ("tests/test_thing.py", 2)])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_stored_finding_keeps_its_own_path_over_one_its_message_cites(self):
        unpinned = ("Dockerfile", 7, "the base image is unpinned, unlike the "
                    "one README.md:12 names")
        self.run_council(council(ASTRA, OPUS), Commit("work"), seats={
            "gpt-6-astra": [Answer("APPROVE", 9, (unpinned,))],
            "opus": [Answer("APPROVE", 8, (unpinned,))]})

        self.assertEqual(self.findings(1), [
            {"path": "Dockerfile", "line": 7, "severity": "p1",
             "category": "correctness", "message": unpinned[2]}])

    def test_a_registered_value_in_a_finding_is_stored_redacted(self):
        leaked = ("README.md", 1, "the token council-dummy-credential-7 is "
                  "printed")
        with values_held(["council-dummy-credential-7"]):
            self.run_council(council(ASTRA, OPUS), Commit("work"), seats={
                "gpt-6-astra": [Answer("APPROVE", 9, (leaked,))],
                "opus": [Answer("APPROVE", 8)]})

        [(findings,)] = self.read("SELECT findings FROM reviewRounds")
        self.assertNotIn("council-dummy-credential-7", findings)
        self.assertIn(REDACTED, findings)

    def test_a_finding_keeps_its_own_path_through_the_adversary_handoff(self):
        unpinned = ("Dockerfile", 7, "the base image is unpinned, unlike the "
                    "one README.md:12 names")
        fake = self.run_council(
            council(ASTRA, OPUS).replace(UNTRIMMED, ADVERSARY_ON),
            Change("change", "poetry.lock", "locked\n"),
            Attack("- src/app.py:4 [p2] a symlink may slip by\n"
                   "EVIDENCE: concern\nADVERSARY: DONE"),
            Change("fix", "src/app.py", "fixed\n"), seats={
                "gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (unpinned,)),
                                Answer("APPROVE", 9)],
                "opus": [Answer("APPROVE", 8), Answer("APPROVE", 9)]})

        fix = [turn for turn in fake.turns if turn.role == IMPLEMENT][1]
        self.assertIn("Dockerfile:7 [p1]", fix.goal)
        self.assertNotIn("README.md:12 [p1]", fix.goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_findings_differing_only_in_severity_both_reach_the_fix(self):
        twins = (("README.md", 1, "the base line is never read back", "p1"),
                 ("README.md", 1, "the base line is never read back", "p2"))
        concern = ("- src/app.py:4 [p2] a symlink may slip by\n"
                   "EVIDENCE: concern\nADVERSARY: DONE")
        for side in (("Nothing broke.\nADVERSARY: DONE",),
                     (concern,)):
            with self.subTest(adversary=side[0][:20]):
                fixture = CouncilFixture()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fake = fixture.run_council(
                    council(ASTRA, OPUS).replace(UNTRIMMED, ADVERSARY_ON),
                    Change("change", "poetry.lock", "locked\n"),
                    Attack(side[0]), *side[1:],
                    Change("fix", "src/app.py", "fixed\n"), seats={
                        "gpt-6-astra": [Answer("REQUEST_CHANGES", 4, twins),
                                        Answer("APPROVE", 9)],
                        "opus": [Answer("APPROVE", 8), Answer("APPROVE", 9)]})

                fix = [turn for turn in fake.turns if turn.role == IMPLEMENT][1]
                self.assertEqual(fix.goal.count(twins[0][2]), 2, fix.goal)
                self.assertEqual(
                    sorted(f["severity"] for f in fixture.findings(1)),
                    ["p1", "p2"])

    def test_a_seat_down_at_its_startup_probe_is_missing_and_not_dispatched(self):
        fake = self.run_council(
            council(ASTRA, OPUS, LUNA), Commit("work"), down={"opus"}, seats={
                "gpt-6-astra": [Answer("APPROVE", 9)],
                "gpt-6-luna": [Answer("APPROVE", 8)]})

        self.assertEqual(sorted(set(self.container.probed)),
                         ["gpt-6-astra", "gpt-6-luna", "opus"])
        self.assertEqual(sorted(turn.model for turn in fake.seat_turns),
                         ["gpt-6-astra", "gpt-6-luna"])
        [(status, reason)] = self.read(
            "SELECT status, reason FROM councilSeats WHERE seat = 2")
        self.assertEqual(status, "missing")
        self.assertTrue(reason.startswith("startup probe failed: "), reason)
        self.assertIn("opus is unreachable", reason)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


SEATS_UNDER_SIGNAL = """
import functools
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from holophyte.review import council, review_runner
scratch = Path(sys.argv[2])
review_runner.SCRATCH_ROOT = scratch / "reviews"
review_runner.CODEX_AUTH = scratch / "auth.json"
review = functools.partial(
    review_runner.run_review, repo=Path(sys.argv[3]), base_sha=sys.argv[4],
    candidate_sha=sys.argv[5], prompt="review")
council.together([review, review])
"""


class CouncilSignalTests(unittest.TestCase):
    def test_a_stop_signal_removes_every_running_seat_container(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _, env = test_review_runner.docker_shim(root)
        log = Path(env["HOLOPHYTE_DOCKER_LOG"])
        base, candidate = test_review_runner.two_commit_repo(root / "repo")
        (root / "auth.json").write_text("{}")
        process = subprocess.Popen(
            [sys.executable, "-c", SEATS_UNDER_SIGNAL, str(HERE.parent),
             str(root), str(root / "repo"), base, candidate],
            env={**os.environ, **env}, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(
            test_review_runner.ContainerLifetimeTests._end_session, process)

        def runs():
            lines = log.read_text().splitlines() if log.exists() else []
            return [line.split()[line.split().index("--name") + 1]
                    for line in lines if line.startswith("run ")]
        deadline = time.monotonic() + 30
        while len(runs()) < 2 and process.poll() is None:
            self.assertLess(time.monotonic(), deadline, "seats never started")
            time.sleep(0.05)
        if process.poll() is not None:
            self.fail(f"seats ended before the signal: {process.communicate()[1]}")
        time.sleep(0.2)

        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=30)

        self.assertEqual(process.returncode, -signal.SIGTERM, stderr)
        recorded = log.read_text().splitlines()
        for name in runs():
            self.assertIn(f"rm --force {name}", recorded)

    def test_a_failed_removal_still_removes_the_rest_and_ends_by_the_signal(self):
        removed = []

        def remove(name):
            removed.append(name)
            if name == "holophyte-review-a":
                raise review_runner.ReviewBoundaryError("it still exists")
        with patch.object(review_runner, "_remove_container", remove), \
                patch.object(review_runner, "_LIVE",
                             {"holophyte-review-a", "holophyte-review-b"}), \
                patch.object(review_runner.os, "kill") as kill:
            with review_runner.removing_on_signal():
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        self.assertEqual(removed, ["holophyte-review-a", "holophyte-review-b"])
        kill.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_a_container_stays_registered_until_its_removal_ends(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        _, env = test_review_runner.docker_shim(root)
        base, candidate = test_review_runner.two_commit_repo(root / "repo")
        (root / "auth.json").write_text("{}")
        real_remove, live = review_runner._remove_container, []

        def remove(name, **kwargs):
            live.append(name in review_runner._LIVE)
            real_remove(name, **kwargs)
        with patch.dict(os.environ, env), \
                patch.object(review_runner, "SCRATCH_ROOT", root / "reviews"), \
                patch.object(review_runner, "CODEX_AUTH", root / "auth.json"), \
                patch.object(review_runner, "_remove_container", remove), \
                self.assertRaises(subprocess.TimeoutExpired):
            review_runner.run_review(repo=root / "repo", base_sha=base,
                                     candidate_sha=candidate, prompt="review",
                                     timeout=1)

        self.assertEqual(live, [True])
        self.assertEqual(review_runner._LIVE, set())


class AdversarySignalTests(unittest.TestCase):
    def test_the_stop_handler_stays_until_the_adversary_joins(self):
        before = signal.getsignal(signal.SIGTERM)
        primary_done, seen = threading.Event(), []

        def primary():
            primary_done.set()
            return "reviewed"

        def side():
            primary_done.wait(10)
            time.sleep(0.1)
            seen.append(signal.getsignal(signal.SIGTERM))
            return "attacked"
        result = adversary.beside(None, None, primary, side)

        self.assertEqual(result, ("reviewed", "attacked"))
        self.assertNotEqual(seen, [before])
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


class CouncilSeatFailureTests(CouncilFixture):
    def a_run(self):
        self.configure("")
        conn = store.open(self.db)
        self.addCleanup(conn.close)
        project = store.ensure_project(conn, "team-1", str(self.target))
        ticket = store.mirror_ticket(conn, project, linear_issue_id="i-1",
                                     linear_identifier="KO-1", title="t")
        return conn, store.claim(conn, project, ticket)

    def test_a_seat_probe_that_cannot_launch_leaves_only_that_seat_down(self):
        self.configure(council(ASTRA, OPUS, ("claude", "sonnet", "high")))

        def container(**turn):
            if turn["harness"] == "codex":
                raise FileNotFoundError(2, "No such file", "auth.json")
            return f"ready {turn['candidate_sha']}"
        with patch.object(review_runner, "run_review",
                          side_effect=container) as run_review:
            fallback.probe_council(self.project)

        self.assertEqual([call.kwargs["model"]
                          for call in run_review.call_args_list],
                         ["gpt-6-astra", "opus", "sonnet"])
        down = routes(self.project).council_down
        self.assertEqual(list(down), [1])
        self.assertIn("auth.json", down[1])

    def scheduler_env(self, down):
        state = routes(self.project)
        state.council_checked = {number: list(seat) for number, seat in
                                 enumerate((ASTRA, OPUS, LUNA), 1)}
        state.council_down = dict(down)
        spawned = {}

        def spawn(argv, env, stdin):
            spawned.update(env)
            return types.SimpleNamespace(pid=1)
        with patch.object(pool, "SPAWN", spawn):
            pool._spawn_worker(self.project, 1)
        reset(self.project)
        return spawned

    def worker_routes(self, config, spawned):
        self.configure(config)
        container, seen = Container(), {}

        def work(target, provider):
            seen.update(routes(target).council_down)
            return 0
        with patch.dict(os.environ, spawned, clear=True), \
                patch.object(review_runner, "run_review", container), \
                patch.object(pool, "_worker", work):
            pool.worker(self.project, StubProvider())
        return seen, container.probed

    def test_a_pooled_worker_inherits_the_schedulers_failed_seat_probes(self):
        self.configure(council(ASTRA, OPUS, LUNA))
        spawned = self.scheduler_env({2: "claude opus: unreachable"})

        seen, probed = self.worker_routes(council(ASTRA, OPUS, LUNA), spawned)

        self.assertEqual(seen, {2: "claude opus: unreachable"})
        self.assertEqual(probed, [])

    def test_a_pooled_worker_probes_a_seat_its_config_changed_since(self):
        self.configure(council(ASTRA, OPUS, LUNA))
        spawned = self.scheduler_env({2: "claude opus: unreachable"})

        seen, probed = self.worker_routes(
            council(ASTRA, ("claude", "sonnet", "high"), LUNA), spawned)

        self.assertEqual(seen, {})
        self.assertEqual(probed, ["sonnet"])

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
            "opus": [OutOfRange(), OutOfRange()]})

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


    def test_a_seat_turn_records_its_provider_usage_under_role_review(self):
        conn, run = self.a_run()
        value = Answer("APPROVE", 9).value("")
        outputs = {
            "codex": "\n".join(json.dumps(event) for event in (
                {"type": "item.completed",
                 "item": {"type": "command_execution", "exit_code": 0}},
                {"type": "item.completed",
                 "item": {"type": "agent_message", "text": json.dumps(value)}},
                {"type": "turn.completed",
                 "usage": {"input_tokens": 1800, "cached_input_tokens": 600,
                           "output_tokens": 90}})),
            "claude": json.dumps({
                "type": "result", "subtype": "success", "is_error": False,
                "result": "reviewed", "structured_output": value,
                "usage": {"input_tokens": 100, "cache_read_input_tokens": 23,
                          "output_tokens": 8},
                "total_cost_usd": 0.02})}

        def container(**turn):
            return review_runner.PARSERS[turn["harness"]](
                outputs[turn["harness"]], None)[0]
        attempt = store.stages.open_stage(conn, run, "review", self.base)
        with patch.object(review_runner, "run_review", side_effect=container):
            for harness, model in (("codex", "gpt-6-astra"), ("claude", "opus")):
                answered = roles.agent(
                    self.project, "review", "goal", self.target,
                    base_sha=self.base, candidate_sha=self.base, conn=conn,
                    run_id=run, family_route={"harness": harness,
                                              "model": model, "effort": "high",
                                              "credential": None})
                self.assertEqual(json.loads(answered), value)
        store.stages.close_stage(conn, attempt, "pass", "next",
                                 roles=("review",))

        turns = [json.loads(payload) for (payload,) in conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'agent_turn'"
            " ORDER BY seq")]
        self.assertEqual([(t["role"], t["label"], t["route"]) for t in turns],
                         [("review", "codex gpt-6-astra", "primary"),
                          ("review", "claude opus", "primary")])
        self.assertEqual(conn.execute(
            "SELECT inputTokens, outputTokens FROM stageAttempts").fetchall(),
            [(1923, 98)])

    def test_real_seat_turns_together_on_one_store_connection_all_answer(self):
        conn, run = self.a_run()
        barrier = threading.Barrier(2, timeout=10)

        def container(**turn):
            barrier.wait()
            return Answer("APPROVE", 9).play("")
        seats = [CouncilSeat(*ASTRA), CouncilSeat(*OPUS)]
        with patch.object(review_runner, "run_review", side_effect=container):
            for rnd in range(1, 21):
                holophyte.review.council.run_round(
                    self.project, conn, run, self.target, self.base,
                    self.base, holophyte.review.council.reply_brief(""), rnd,
                    seats, [(1, "Given the thing, when it runs, then it works")],
                    roles.agent)

        self.assertEqual(conn.execute(
            "SELECT status, COUNT(*) FROM councilSeats GROUP BY status"
        ).fetchall(), [("answered", 40)])

    def test_a_claude_seat_out_of_schema_retries_is_unreadable_after_two(self):
        conn, run = self.a_run()
        native = json.dumps({"type": "result", "is_error": True,
                             "subtype": "error_max_structured_output_retries",
                             "usage": {"input_tokens": 200,
                                       "output_tokens": 20},
                             "total_cost_usd": 0.01})
        attempt = store.stages.open_stage(conn, run, "review", self.base)

        def container(**turn):
            if turn["harness"] == "codex":
                return Answer("APPROVE", 9).play("")
            return review_runner.parse_claude_output(native, None)
        seats = [CouncilSeat(*ASTRA), CouncilSeat(*OPUS)]
        with patch.object(review_runner, "run_review",
                          side_effect=container) as run_review, \
                self.assertRaises(InfraFailure) as raised:
            holophyte.review.council.run_round(
                self.project, conn, run, self.target, self.base, self.base,
                holophyte.review.council.reply_brief(""), 1, seats,
                [(1, "Given the thing, when it runs, then it works")],
                roles.agent)

        store.stages.close_stage(conn, attempt, "error", "end",
                                 roles=("review",))
        self.assertEqual(raised.exception.failure_kind, "review_route")
        self.assertEqual(
            [call.kwargs["harness"] for call in run_review.call_args_list
             ].count("claude"), 2)
        self.assertEqual(conn.execute(
            "SELECT inputTokens, outputTokens, costUsd FROM stageAttempts"
        ).fetchall(), [(400, 40, 0.02)])
        self.assertEqual(conn.execute(
            "SELECT seat, status FROM councilSeats ORDER BY seat").fetchall(),
            [(1, "answered"), (2, "unreadable")])


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
            self.assertEqual(store.schema.SCHEMA_VERSION, 75)
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

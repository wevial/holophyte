"""A council round's consolidator: one authoritative round from every
answering seat's reply and the adversary's findings under the split-vote
rule, the route it names, its fallback to the strictest-wins merge, the
review attempt's council detail, the description finding held for the
PR-text writer, the additive migration, and a loop without a council left
as it was.

Run: python3 -m unittest discover -s tests -p 'test_council_consolidation.py' -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
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
    Attack,
    Commit,
    Consolidate,
    Idle,
    Reply,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    UNTRIMMED,
    VALID_BODY,
    MergeModeFixture,
    StubProvider,
    a_task,
)
from test_council_seats import (  # noqa: E402 - after the sys.path insert
    ADVERSARY_ON,
    ASTRA,
    CONCERN,
    CONSOLIDATOR,
    FIXED_DATES,
    LUNA,
    MINUTES_RE,
    OPUS,
    USAGE,
    Answer,
    Change,
    Container,
    CouncilAgent,
    CouncilFixture,
    RouteDown,
    council,
    provider_output,
)

import holophyte.review.council  # noqa: E402 - after the sys.path insert
import store  # noqa: E402 - after the sys.path insert above
import store.schema  # noqa: E402 - after the sys.path insert above
from holophyte.agents.agent_turns import family_label  # noqa: E402
from holophyte.loop.stop import command  # noqa: E402
from holophyte.review import review_runner  # noqa: E402
from holophyte.review.json_reply import SCHEMA_RETRY  # noqa: E402

SHARED = ("README.md", 1, "the base line is never read back")
REWORDED = ("README.md", 1, "nothing reads README's first line again")
OTHER = ("tests/test_thing.py", 2, "the test asserts nothing")
CONSOLIDATED_BY = family_label({"harness": "codex",
                                "model": review_runner.MODEL})
WITNESS = "tests/test_thing.py::test_it_works"


@dataclass(frozen=True)
class Ruling:
    """A consolidator's schema-checked reply: kept findings as
    `(path, line, message, raised_by, target)`, dropped ones as
    `(finding, reason, raised_by)`."""

    verdict: str = "APPROVE"
    findings: tuple = ()
    refuted: tuple = ()
    criterion: str = "met"
    witness: str | None = WITNESS

    def play(self, goal):
        return json.dumps({
            "verdict": self.verdict,
            "criteria": [{"id": "C1", "status": self.criterion,
                          "reason": "the named test runs the thing",
                          "witness": self.witness}],
            "findings": [{"path": path, "line": line, "severity": "p1",
                          "category": "correctness", "target": target,
                          "message": message, "raised_by": list(raised_by)}
                         for path, line, message, raised_by, target
                         in self.findings],
            "follow_ups": [], "scope": [],
            "refuted": [{"finding": finding, "reason": reason,
                         "raised_by": list(raised_by)}
                        for finding, reason, raised_by in self.refuted]})


@dataclass(frozen=True)
class Prose:
    """A consolidator reply that is no JSON object."""

    def play(self, goal):
        return "I merged the seats' findings; F1 and F2 are one defect."


class Panel(Container):
    """The review container with a consolidator: a consolidator turn is
    answered from its own queue and kept with its route and prompt."""

    def __init__(self, seats, rulings, down=()):
        super().__init__(seats, down)
        self.rulings = list(rulings)
        self.consolidations, self.tiers = [], []

    def __call__(self, **turn):
        turn.setdefault("harness", "codex")
        if not turn["prompt"].startswith(CONSOLIDATOR):
            return super().__call__(**turn)
        with self.lock:
            self.consolidations.append(
                (turn["harness"], turn["model"], turn["prompt"]))
            self.tiers.append(turn.get("service_tier"))
            step = self.rulings.pop(0)
        return review_runner.PARSERS[turn["harness"]](provider_output(
            turn["harness"], step.play(turn["prompt"]), USAGE[turn["model"]]),
            None)[0]


class Consolidated(CouncilFixture):
    def run_panel(self, config, *script, seats, rulings, down=(),
                  provider=None):
        self.configure(config)
        self.panel = Panel(seats, rulings, down)
        fake = CouncilAgent(*script)
        with patch.object(review_runner, "run_review", self.panel):
            return self.loop(fake=fake, provider=provider)[0]

    def round_rows(self):
        return self.read("SELECT round, verdict, consolidatedBy FROM"
                         " reviewRounds ORDER BY round")

    def reply(self, rnd):
        [(text,)] = self.read(
            f"SELECT reply FROM reviewRounds WHERE round = {rnd}")
        return json.loads(text)

    def events(self, kind):
        return self.read(f"SELECT summary, level, payload FROM runEvents"
                         f" WHERE kind = '{kind}' ORDER BY seq")


class ConsolidatedRoundTests(Consolidated):
    def test_one_defect_in_two_seats_words_is_held_once_naming_both_seats(self):
        fake = self.run_panel(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (SHARED,)),
                                   Answer("APPROVE", 9)],
                   "opus": [Answer("REQUEST_CHANGES", 5, (REWORDED,)),
                            Answer("APPROVE", 9)]},
            rulings=[Ruling("REQUEST_CHANGES", findings=(
                (*SHARED, ("F1", "F2"), "code"),)), Ruling()])

        self.assertEqual(self.round_rows(), [
            (1, "changes_requested", CONSOLIDATED_BY),
            (2, "pass", CONSOLIDATED_BY)])
        self.assertEqual(self.findings(1), [
            {"path": "README.md", "line": 1, "severity": "p1",
             "category": "correctness", "message": SHARED[2],
             "raised_by": ["seat 1", "seat 2"]}])
        fix = [turn for turn in fake.turns if turn.role == IMPLEMENT][1]
        self.assertEqual(fix.goal.count(SHARED[2]), 1)
        self.assertNotIn(REWORDED[2], fix.goal)
        seat_goals = {turn.model: turn.goal
                      for turn in self.panel.seat_turns[:2]}
        rows = self.seats("seat", "reply")
        self.assertEqual(json.loads(rows[0][1]), Answer(
            "REQUEST_CHANGES", 4, (SHARED,)).value(seat_goals["gpt-6-astra"]))
        self.assertEqual(json.loads(rows[1][1]), Answer(
            "REQUEST_CHANGES", 5, (REWORDED,)).value(seat_goals["opus"]))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_kept_report_and_its_refuted_twin_are_held_once(self):
        self.run_panel(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (SHARED,)),
                                   Answer("APPROVE", 9)],
                   "opus": [Answer("REQUEST_CHANGES", 5, (REWORDED,)),
                            Answer("APPROVE", 9)]},
            rulings=[Ruling("REQUEST_CHANGES",
                            findings=((*SHARED, ("F1",), "code"),),
                            refuted=((REWORDED[2], "already covered by F1",
                                      ("F2",)),)), Ruling()])

        self.assertEqual(
            [(f["path"], f["line"], f["message"], f["raised_by"])
             for f in self.findings(1)],
            [("README.md", 1, SHARED[2], ["seat 1", "seat 2"])])

    def test_the_consolidator_keeps_the_configured_review_service_tier(self):
        self.run_panel(
            '[agents]\nreview_service_tier = "priority"\n'
            + council(ASTRA, OPUS), Commit("work"),
            seats={"gpt-6-astra": [Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 8)]},
            rulings=[Ruling()])

        self.assertEqual(self.panel.tiers, ["priority"])
        self.assertEqual(self.round_rows(), [(1, "pass", CONSOLIDATED_BY)])

    def test_a_lone_finding_dropped_with_a_reason_lets_the_round_approve(self):
        reason = "the test asserts the return value two lines down"
        self.run_panel(
            council(ASTRA, OPUS), Commit("work"),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (OTHER,))],
                   "opus": [Answer("APPROVE", 9)]},
            rulings=[Ruling(refuted=((OTHER[2], reason, ("F1",)),))])

        self.assertEqual(self.round_rows(), [(1, "pass", CONSOLIDATED_BY)])
        self.assertEqual(self.findings(1), [])
        reply = self.reply(1)
        self.assertEqual(reply["verdict"], "APPROVE")
        self.assertEqual([(entry["finding"], entry["reason"])
                          for entry in reply["refuted"]], [(OTHER[2], reason)])
        self.assertEqual(self.events("council_kept"), [])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_finding_two_seats_raised_at_one_place_survives_its_drop(self):
        fake = self.run_panel(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (SHARED,)),
                                   Answer("APPROVE", 9)],
                   "opus": [Answer("REQUEST_CHANGES", 5, (REWORDED,)),
                            Answer("APPROVE", 9)]},
            rulings=[Ruling(refuted=((SHARED[2], "a style point",
                                      ("F1", "F2")),)), Ruling()])

        self.assertEqual(self.round_rows()[0],
                         (1, "changes_requested", CONSOLIDATED_BY))
        self.assertEqual(
            [(f["path"], f["line"], f["raised_by"]) for f in self.findings(1)],
            [("README.md", 1, ["seat 1", "seat 2"])])
        self.assertEqual(self.reply(1)["refuted"], [])
        [(summary, level, payload)] = self.events("council_kept")
        self.assertEqual(level, "detail")
        self.assertIn("README.md:1", summary)
        self.assertIn(SHARED[2], summary)
        self.assertEqual(json.loads(payload)["finding"]["message"],
                         f"{SHARED[2]}\n{REWORDED[2]}")
        fix = [turn for turn in fake.turns if turn.role == IMPLEMENT][1]
        self.assertIn(SHARED[2], fix.goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_lone_finding_dropped_with_a_blank_reason_is_kept(self):
        self.run_panel(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (OTHER,)),
                                   Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 9), Answer("APPROVE", 9)]},
            rulings=[Ruling(refuted=((OTHER[2], " ", ("F1",)),)), Ruling()])

        self.assertEqual(self.round_rows()[0],
                         (1, "changes_requested", CONSOLIDATED_BY))
        self.assertEqual([(f["path"], f["raised_by"]) for f in self.findings(1)],
                         [("tests/test_thing.py", ["seat 1"])])
        [(summary, _, _)] = self.events("council_kept")
        self.assertIn("dropped without a reason", summary)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_an_adversary_finding_dropped_with_a_reason_no_longer_blocks(self):
        traced = ("- src/app.py:4 [p1] a symlink may slip by\nEVIDENCE: traced\n"
                  "- src/app.py:9 [p2] the lock may go stale\n"
                  "EVIDENCE: concern\nADVERSARY: DONE")
        self.run_panel(
            council(ASTRA, OPUS).replace(UNTRIMMED, ADVERSARY_ON),
            Change("change", "poetry.lock", "locked\n"), Attack(traced),
            seats={"gpt-6-astra": [Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 8)]},
            rulings=[Ruling(refuted=((
                "a symlink may slip by", "the path is resolved first",
                ("F1",)),))])

        [(_, _, brief)] = self.panel.consolidations
        self.assertIn("F1, raised by adversary: src/app.py:4 [p1] evidence "
                      "traced", brief)
        self.assertIn("F2, raised by adversary: src/app.py:9 [p2] evidence "
                      "concern", brief)
        self.assertEqual(self.round_rows(), [(1, "pass", CONSOLIDATED_BY)])
        self.assertEqual(self.findings(1), [])
        self.assertEqual(self.events("council_kept"), [])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_seats_unmet_criterion_outlasts_a_met_claim_naming_no_witness(self):
        fake = self.run_panel(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"),
            seats={"gpt-6-astra": [Answer("APPROVE", 9), Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 6, criterion="not_met"),
                            Answer("APPROVE", 9)]},
            rulings=[Ruling(witness=None), Ruling()])

        self.assertEqual(self.round_rows()[0],
                         (1, "changes_requested", CONSOLIDATED_BY))
        reply = self.reply(1)
        self.assertEqual(reply["verdict"], "REQUEST_CHANGES")
        self.assertEqual([entry["status"] for entry in reply["criteria"]],
                         ["not_met"])
        fix = [turn for turn in fake.turns if turn.role == IMPLEMENT][1]
        self.assertIn("CRITERION C1: not met", fix.goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_the_review_attempt_records_each_seat_and_the_consolidator(self):
        self.run_panel(
            council(ASTRA, OPUS, LUNA), Commit("work"),
            seats={"gpt-6-astra": [Answer("APPROVE", 9)],
                   "opus": [RouteDown()],
                   "gpt-6-luna": [Answer("APPROVE", 7)]},
            rulings=[Ruling()])

        [(detail,)] = self.read(
            "SELECT detail FROM stageAttempts WHERE stageId = 'review'")
        self.assertEqual(json.loads(detail)["council"], {
            "seats": [
                {"seat": 1, "route": "codex gpt-6-astra", "status": "answered",
                 "score": 9},
                {"seat": 2, "route": "claude opus", "status": "missing",
                 "score": None},
                {"seat": 3, "route": "codex gpt-6-luna", "status": "answered",
                 "score": 7}],
            "consolidated_by": CONSOLIDATED_BY})
        [(_, _, brief)] = self.panel.consolidations
        self.assertIn("Seat 2 (claude opus) did not vote, missing:", brief)
        self.assertIn("the container exited 125", brief)


    def test_an_unmentioned_concern_on_a_high_path_is_still_raised(self):
        self.run_panel(
            council(ASTRA, OPUS).replace(
                UNTRIMMED, ADVERSARY_ON + 'high_paths = ["src/*"]\n'),
            Change("change", "poetry.lock", "locked\n"), Attack(CONCERN),
            seats={"gpt-6-astra": [Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 8)]},
            rulings=[Ruling()])

        self.assertEqual(self.round_rows(), [(1, "pass", CONSOLIDATED_BY)])
        [(summary, _, payload)] = self.events("concern_raised")
        self.assertIn("src/app.py", summary)
        self.assertEqual(json.loads(payload)["pattern"], "src/*")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_council_under_quorum_still_records_its_seats_on_the_attempt(self):
        self.run_panel(
            council(ASTRA, OPUS), Commit("work"),
            seats={"gpt-6-astra": [Answer("APPROVE", 9)],
                   "opus": [RouteDown()]}, rulings=[])

        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "review_route")])
        [(detail,)] = self.read(
            "SELECT detail FROM stageAttempts WHERE stageId = 'review'")
        self.assertEqual(json.loads(detail)["council"], {
            "seats": [
                {"seat": 1, "route": "codex gpt-6-astra", "status": "answered",
                 "score": 9},
                {"seat": 2, "route": "claude opus", "status": "missing",
                 "score": None}],
            "consolidated_by": None})


class UnconsolidatedTests(Consolidated):
    SEATS = {"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (SHARED,)),
                             Answer("APPROVE", 9)],
             "opus": [Answer("APPROVE", 8, (OTHER,)), Answer("APPROVE", 9)]}

    def test_a_failed_consolidator_route_leaves_the_strictest_wins_merge(self):
        fallback = ('[agents]\nreview_fallback_model = "gpt-6-luna"\n'
                    'review_fallback_effort = "medium"\n')
        fake = self.run_panel(
            fallback + council(ASTRA, OPUS), Commit("work"), Commit("fix"),
            seats=self.SEATS, rulings=[RouteDown(), Ruling()])

        self.assertEqual(self.round_rows(), [
            (1, "changes_requested", None), (2, "pass", CONSOLIDATED_BY)])
        self.assertEqual(
            sorted((f["path"], f["line"]) for f in self.findings(1)),
            [("README.md", 1), ("tests/test_thing.py", 2)])
        [(summary, level, _)] = self.events("council_unconsolidated")
        self.assertEqual(level, "narrative")
        self.assertIn("the consolidator's route failed", summary)
        self.assertIn("the container exited 125", summary)
        self.assertEqual([(harness, model) for harness, model, _
                          in self.panel.consolidations],
                         [("codex", "gpt-6-astra")] * 2)
        self.assertEqual(sorted(turn.model for turn in self.panel.seat_turns),
                         ["gpt-6-astra", "gpt-6-astra", "opus", "opus"])
        self.assertNotIn("consolidate", fake.roles)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_failed_consolidator_beside_an_adversary_runs_no_other(self):
        traced = ("- src/app.py:4 [p1] a symlink may slip by\n"
                  "EVIDENCE: traced\nADVERSARY: DONE")
        fake = self.run_panel(
            council(ASTRA, OPUS).replace(UNTRIMMED, ADVERSARY_ON),
            Change("change", "poetry.lock", "locked\n"), Attack(traced),
            Change("fix", "src/app.py", "fixed\n"),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (SHARED,)),
                                   Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 8), Answer("APPROVE", 9)]},
            rulings=[RouteDown(), Ruling()])

        self.assertEqual(self.round_rows()[0], (1, "changes_requested", None))
        self.assertEqual(len(self.panel.consolidations), 2)
        fix = [turn for turn in fake.turns if turn.role == IMPLEMENT][1]
        self.assertIn(SHARED[2], fix.goal)
        self.assertIn("a symlink may slip by", fix.goal)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_configured_codex_json_consolidator_reply_is_read(self):
        value = json.loads(Ruling("REQUEST_CHANGES", findings=(
            (*SHARED, ("F1",), "code"),)).play(""))
        events = "\n".join(json.dumps(event) for event in (
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": json.dumps(value)}},
            {"type": "turn.completed", "usage": {}}))

        def run_agent(project, role, goal, cwd, **kwargs):
            return events
        got, why = holophyte.review.council.consolidator_reply(
            self.project, None, None, self.target, self.base, self.base,
            "consolidate", None, run_agent)

        self.assertEqual((got, why), (value, None))

    def test_a_consolidator_reply_unreadable_twice_leaves_the_merge(self):
        self.run_panel(
            council(ASTRA, OPUS), Commit("work"), Commit("fix"),
            seats=self.SEATS, rulings=[Prose(), Prose(), Ruling()])

        self.assertEqual(self.round_rows()[0], (1, "changes_requested", None))
        [(summary, _, _)] = self.events("council_unconsolidated")
        self.assertIn("failed the consolidated reply schema twice", summary)
        first, retried, _ = [brief for *_, brief in self.panel.consolidations]
        self.assertNotIn(SCHEMA_RETRY, first)
        self.assertTrue(retried.endswith(SCHEMA_RETRY))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


WRITTEN = Idle("TITLE: Add the thing\n\nThe thing is now added.\n")
DESCRIBED = (None, None, "the description never says the thing is added")


@dataclass(frozen=True)
class Pausing:
    """A seat's answer given after the run is asked to pause."""

    db: Path
    answer: Answer

    def play(self, goal):
        conn = store.open(self.db)
        try:
            [(run,)] = conn.execute(
                "SELECT id FROM runs WHERE endedAt IS NULL").fetchall()
            store.pause(conn, run, "pause before the pull request")
        finally:
            conn.close()
        return self.answer.play(goal)


class DescriptionFindingTests(MergeModeFixture, Consolidated):
    PR = '[merge]\nmode = "pr"\n'

    def writer(self, fake):
        return [turn.goal for turn in fake.turns if turn.role == IMPLEMENT
                and "Write the pull request title" in turn.goal]

    def test_the_fix_gets_the_code_finding_and_the_writer_the_description(self):
        self.fake_route(states=[self.pr_state()])
        fake = self.run_panel(
            self.PR + council(ASTRA, OPUS), Commit("work"),
            Commit("fix"), WRITTEN,
            provider=StubProvider(dict(a_task(), body=VALID_BODY)),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (OTHER,)),
                                   Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 8), Answer("APPROVE", 9)]},
            rulings=[Ruling("REQUEST_CHANGES", findings=(
                (*OTHER, ("F1",), "code"), (*DESCRIBED, (), "description"))),
                Ruling()])

        implement = [turn.goal for turn in fake.turns if turn.role == IMPLEMENT]
        fix, writer = implement[1], implement[2]
        self.assertIn(OTHER[2], fix)
        self.assertNotIn(DESCRIBED[2], fix)
        self.assertIn("Write the pull request title and description", writer)
        self.assertIn(DESCRIBED[2], writer)
        self.assertNotIn(OTHER[2], writer)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])


    def test_a_description_only_round_goes_to_the_writer_without_a_fix(self):
        self.fake_route(states=[self.pr_state()])
        fake = self.run_panel(
            self.PR + council(ASTRA, OPUS), Commit("work"), WRITTEN,
            provider=StubProvider(dict(a_task(), body=VALID_BODY)),
            seats={"gpt-6-astra": [Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 8)]},
            rulings=[Ruling("REQUEST_CHANGES", findings=(
                (*DESCRIBED, (), "description"),))])

        self.assertEqual(self.round_rows()[0],
                         (1, "changes_requested", CONSOLIDATED_BY))
        [writer] = self.writer(fake)
        self.assertIn(DESCRIBED[2], writer)
        self.assertEqual(len([turn for turn in fake.turns
                              if turn.role == IMPLEMENT]), 2)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_an_adjudicated_round_still_routes_its_description_finding(self):
        self.fake_route(states=[self.pr_state()])
        both = (SHARED, OTHER)
        fake = self.run_panel(
            self.PR + "[loop]\nreview_rounds = 3\n" + council(ASTRA, OPUS),
            Commit("work"), Commit("fix"),
            Reply("THREAD 1: DROP -- the base line is read by the loader\n"
                  "THREAD 2: FIX -- the test still asserts nothing\n"
                  "THREAD 3: FIX -- the description still omits it"),
            Commit("fix again"), WRITTEN,
            provider=StubProvider(dict(a_task(), body=VALID_BODY)),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, both),
                                   Answer("REQUEST_CHANGES", 4, both),
                                   Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 8)] * 3},
            rulings=[Ruling("REQUEST_CHANGES", findings=(
                (*SHARED, ("F1",), "code"), (*OTHER, ("F2",), "code"),
                (*DESCRIBED, (), "description")))] * 2 + [Ruling()])

        fixes = [turn.goal for turn in fake.turns if turn.role == IMPLEMENT][1:3]
        self.assertIn(OTHER[2], fixes[1])
        self.assertNotIn(SHARED[2], fixes[1].split("adjudicator")[0])
        for fix in fixes:
            self.assertNotIn(DESCRIBED[2], fix)
        [writer] = self.writer(fake)
        self.assertIn(DESCRIBED[2], writer)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_pause_at_the_merge_gate_carries_the_description_finding(self):
        self.fake_route(states=[self.pr_state()])
        self.run_panel(
            self.PR + council(ASTRA, OPUS), Commit("work"), Commit("fix"),
            provider=StubProvider(dict(a_task(), body=VALID_BODY)),
            seats={"gpt-6-astra": [Answer("REQUEST_CHANGES", 4, (OTHER,)),
                                   Answer("APPROVE", 9)],
                   "opus": [Answer("APPROVE", 8),
                            Pausing(self.db, Answer("APPROVE", 9))]},
            rulings=[Ruling("REQUEST_CHANGES", findings=(
                (*OTHER, ("F1",), "code"), (*DESCRIBED, (), "description"))),
                Ruling()])
        self.assertEqual(self.read(
            "SELECT resumePhase FROM runs WHERE outcome = 'paused'"),
            [("merge_gate",)])
        command(self.project, "KO-131", "go on", resume=True)

        fake = self.run_panel(
            self.PR + council(ASTRA, OPUS), WRITTEN, seats={}, rulings=[],
            provider=StubProvider(dict(a_task(), body=VALID_BODY)))

        [writer] = self.writer(fake)
        self.assertIn(DESCRIBED[2], writer)
        self.assertEqual(self.read(
            "SELECT outcome FROM runs ORDER BY attempt DESC LIMIT 1"),
            [("merged",)])


class ConsolidationMigrationTests(unittest.TestCase):
    def test_a_previous_version_store_gains_a_null_consolidated_by(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            conn = store.open(path)
            project = store.ensure_project(conn, "team-1", "/repo-1")
            ticket = store.mirror_ticket(conn, project, linear_issue_id="i-1",
                                         linear_identifier="KO-1", title="t")
            run = store.claim(conn, project, ticket)
            store.record_review_round(conn, run, 1, "pass", "codex gpt-6-astra")
            conn.close()
            raw = sqlite3.connect(path)
            raw.executescript("ALTER TABLE reviewRounds DROP COLUMN"
                              " consolidatedBy;\nPRAGMA user_version = 74;\n")
            raw.close()

            conn = store.open(path)
            self.addCleanup(conn.close)

            self.assertEqual(conn.execute("PRAGMA user_version").fetchone(),
                             (75,))
            self.assertEqual(store.schema.SCHEMA_VERSION, 75)
            self.assertEqual(store.schema.READABLE_FROM, 50)
            self.assertEqual(conn.execute(
                "SELECT round, consolidatedBy FROM reviewRounds").fetchall(),
                [(1, None)])


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
            "prompts": [(turn.role, turn.goal) for turn in fake.turns
                        if turn.role in ("review", "consolidate")],
            "rounds": fixture.read(
                "SELECT runId, round, verificationResults, verdict, findings,"
                " findingsFingerprint, reviewerModel, reply, consolidatedBy"
                " FROM reviewRounds ORDER BY round"),
            "phases": fixture.read("SELECT summary FROM runEvents"
                                   " WHERE kind = 'phase_change' ORDER BY seq"),
            "ledger": [(kind, MINUTES_RE.sub("N min", text))
                       for kind, text in fixture.read(
                           "SELECT kind, text FROM ledger ORDER BY id")],
            "outcome": fixture.read("SELECT outcome FROM runs")}

    def test_a_raising_consolidation_changes_nothing_while_the_table_is_unset(self):
        plain = self.walk()
        raising = self.walk(patch.object(
            holophyte.review.council, "consolidated",
            side_effect=AssertionError("a council consolidated while unset")))

        self.assertEqual(plain["outcome"], [("merged",)])
        self.assertEqual([role for role, _ in plain["prompts"]],
                         ["review", "consolidate", "review"])
        self.assertEqual([row[-1] for row in plain["rounds"]], [None, None])
        for key in plain:
            with self.subTest(key=key):
                self.assertEqual(raising[key], plain[key])


if __name__ == "__main__":
    unittest.main()

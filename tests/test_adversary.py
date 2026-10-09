"""The adversarial pass beside the primary review: when it runs, what blocks."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402
    ADVERSARY,
    APPROVE,
    IMPLEMENT,
    REQUEST_CHANGES,
    REVIEW_ROLES,
    Attack,
    Consolidate,
    FakeAgent,
    _git,
)
from loop_fixture import BRANCH, LoopFixture, StubProvider, a_task  # noqa: E402

from holophyte.agents import probes, roles  # noqa: E402
from holophyte.agents.agent_routes import reset  # noqa: E402
from holophyte.review.adversary import Family, Pass, brief  # noqa: E402

ON = "[review]\nadversary = true\n"
LEVELS = ("EVIDENCE: reproduced", "EVIDENCE: traced", "EVIDENCE: concern")
REFUSAL = ("This content was flagged for possible cybersecurity risk. If this "
           "seems wrong, try rephrasing your request.")
FILTERED_WORDS = ("attack", "break", "exploit", "attacker", "bypass")


@dataclass
class Change:
    """An implementer turn that writes `path`, making its directories, and commits."""

    path: str
    body: str = "changed\n"

    role = IMPLEMENT

    def play(self, cwd, turn):
        target = cwd / self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.body)
        _git(cwd, "add", "-A")
        _git(cwd, "commit", "-q", "-m", f"change {self.path}")
        return f"committed {self.path}"


@dataclass
class AtBarrier:
    """A turn that answers only once the other party reaches the same barrier."""

    role: object
    text: str
    barrier: threading.Barrier

    def play(self, cwd, turn):
        self.barrier.wait()
        return self.text


def attack(*findings):
    return Attack("".join(findings) + "ADVERSARY: DONE")


def finding(path, line, message, evidence=None):
    tail = f"EVIDENCE: {evidence}\n" if evidence else ""
    return f"- {path}:{line} [p1] {message}\n{tail}"


class AdversaryFixture(LoopFixture):
    def events(self):
        return [json.loads(payload) for (payload,) in self.read(
            "SELECT payload FROM runEvents WHERE kind = 'adversary_round'"
            " ORDER BY seq")]

    def turns(self, fake, role):
        return [turn for turn in fake.turns if turn.role == role]

    def round_row(self, rnd):
        [(verdict, findings)] = self.read(
            f"SELECT verdict, findings FROM reviewRounds WHERE round = {rnd}")
        return verdict, json.loads(findings)


class WhenItRunsTests(AdversaryFixture):
    def test_a_lockfile_round_runs_one_full_pass_beside_the_review(self):
        self.configure(ON)
        fake, _ = self.loop(Change("poetry.lock"), APPROVE, attack())
        [review] = self.turns(fake, "review")
        [attacker] = self.turns(fake, ADVERSARY)
        self.assertEqual((attacker.base_sha, attacker.candidate_sha),
                         (review.base_sha, review.candidate_sha))
        self.assertIn("Depth: full", attacker.goal)
        self.assertIn("at most 5 subagents", attacker.goal)
        for level in LEVELS:
            self.assertIn(level, attacker.goal)
        self.assertEqual(attacker.timeout, 1800)
        [event] = self.events()
        self.assertEqual(
            {key: event[key] for key in
             ("round", "tier", "depth", "scope", "family", "range")},
            {"round": 1, "tier": "high", "depth": "full", "scope": "candidate",
             "family": "codex",
             "range": [review.base_sha, review.candidate_sha]})
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_medium_round_runs_a_light_pass_without_module_attackers(self):
        self.configure(ON + 'medium_paths = ["src/parse/*"]\n')
        fake, _ = self.loop(Change("src/parse/csv.py"), APPROVE, attack())
        [attacker] = self.turns(fake, ADVERSARY)
        self.assertIn("Depth: light", attacker.goal)
        self.assertIn("one review subagent per surface", attacker.goal)
        self.assertIn("no per-module subagents", attacker.goal)
        self.assertNotIn("per risky module", attacker.goal)
        self.assertEqual(attacker.timeout, 900)
        [event] = self.events()
        self.assertEqual((event["tier"], event["depth"]), ("medium", "light"))

    def test_no_pass_on_a_low_round_or_with_the_switch_absent_or_off(self):
        for n, (toml, path) in enumerate(
                ((ON, "README.md"), ("", "poetry.lock"),
                 ("[review]\nadversary = false\n", "poetry.lock")), 1):
            with self.subTest(toml=toml, path=path):
                self.configure(toml)
                fake, _ = self.loop(Change(path, f"change {n}\n"), APPROVE,
                                    provider=StubProvider(a_task(n)))
                self.assertEqual(fake.roles, [IMPLEMENT, "review"])
                self.assertEqual(self.events(), [])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)] * 3)

    def test_the_two_turns_run_together_and_neither_sees_the_other(self):
        self.configure(ON)
        barrier = threading.Barrier(2, timeout=10)
        primary_text = APPROVE.text.replace("no blockers", "PRIMARY-ONLY-MARK")
        fake, _ = self.loop(
            Change("poetry.lock"),
            AtBarrier(REVIEW_ROLES, primary_text, barrier),
            AtBarrier(ADVERSARY, "ADVERSARY-ONLY-MARK\nADVERSARY: DONE", barrier))
        self.assertFalse(barrier.broken)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        [review] = self.turns(fake, "review")
        [attacker] = self.turns(fake, ADVERSARY)
        self.assertNotIn("ADVERSARY-ONLY-MARK", review.goal)
        self.assertNotIn("PRIMARY-ONLY-MARK", attacker.goal)


class BriefWordingTests(AdversaryFixture):
    def test_every_brief_avoids_the_filtered_words_and_keeps_its_contract(self):
        ticket = "Add a CSV export. Given a file, when exported, then it parses."
        codex = Family("codex")
        concern = ({"message": "a symlink may slip by"},)
        briefs = {
            "full": brief(Pass(1, "high", "full", "candidate", "a1", "b2",
                               codex), ticket, 7),
            "light": brief(Pass(1, "medium", "light", "candidate", "a1", "b2",
                                codex), ticket, 7),
            "fix": brief(Pass(2, "high", "light", "fix", "b2", "c3", codex,
                              concern), ticket, 7)}
        for name, goal in briefs.items():
            with self.subTest(brief=name):
                for word in FILTERED_WORDS:
                    self.assertNotIn(word, goal.lower())
                depth, seconds = ("full", 1800) if name == "full" else (
                    "light", 900)
                self.assertIn(f"Depth: {depth}. Time box: {seconds} seconds",
                              goal)
                self.assertIn("at most 5 subagents", goal)
                for level in LEVELS:
                    self.assertIn(level, goal)
                self.assertIn("`SUBAGENT: MODEL — SURFACE`", goal)
                self.assertTrue(goal.endswith("\nADVERSARY: DONE"))
        self.assertIn("plus one per risky module", briefs["full"])
        for name in ("light", "fix"):
            self.assertIn("no per-module subagents", briefs[name])
            self.assertNotIn("per risky module", briefs[name])
        self.assertIn("- a symlink may slip by", briefs["fix"])


class BlockingTests(AdversaryFixture):
    def blocked_by(self, evidence):
        self.configure(ON)
        broken = finding("src/app.py", 3, "the rule skips re-exports", evidence)
        fake, _ = self.loop(Change("poetry.lock"), APPROVE, attack(broken),
                            Change("src/app.py"), APPROVE)
        verdict, findings = self.round_row(1)
        self.assertEqual(verdict, "changes_requested")
        [found] = [f for f in findings if f.get("reviewer") == "adversary"]
        self.assertEqual((found["path"], found["line"], found["evidence"]),
                         ("src/app.py", 3, evidence))
        fix = self.turns(fake, IMPLEMENT)[1].goal
        self.assertIn(f"1. src/app.py:3 [p1] evidence {evidence}, found by "
                      "adversary\n    the rule skips re-exports", fix)
        self.assertEqual(self.round_row(2)[0], "pass")

    def test_a_reproduced_finding_blocks_an_approved_round(self):
        self.blocked_by("reproduced")

    def test_a_traced_finding_blocks_an_approved_round(self):
        self.blocked_by("traced")

    def test_concerns_are_recorded_and_never_block(self):
        self.configure(ON)
        fake, _ = self.loop(Change("poetry.lock"), APPROVE, attack(
            finding("src/app.py", 4, "a symlink may slip by", "concern"),
            finding("src/cli.py", 9, "an empty argv may crash"),
            finding("Dockerfile", 1, "the runtime runs as root"),
            finding("src/db.py", 2, "a lock may be skipped",
                    "traced_but_unverified"),
            finding("src/io.py", 5, "a write may be lost", "reproduced_")))
        self.assertEqual(sorted(fake.roles), sorted([IMPLEMENT, "review",
                                                     ADVERSARY]))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        [event] = self.events()
        self.assertEqual(event["findings"], [])
        self.assertEqual([(c["path"], c["evidence"]) for c in event["concerns"]],
                         [("src/app.py", "concern"), ("src/cli.py", "concern"),
                          ("Dockerfile", "concern"), ("src/db.py", "concern"),
                          ("src/io.py", "concern")])
        [note] = [text for (text,) in self.read(
            "SELECT text FROM ledger WHERE kind = 'note'")
            if text.startswith("Round 1 adversary concerns")]
        self.assertIn("an empty argv may crash", note)
        self.assertIn("the runtime runs as root", note)
        verdict, findings = self.round_row(1)
        self.assertEqual((verdict, findings), ("pass", []))


class RerunTests(AdversaryFixture):
    CONCERN = finding("src/app.py", 4, "a symlink may slip by", "concern")

    def test_a_fix_outside_the_gated_paths_runs_no_second_pass(self):
        self.configure(ON)
        fake, _ = self.loop(Change("poetry.lock"), REQUEST_CHANGES,
                            attack(self.CONCERN), Consolidate(),
                            Change("src/app.py"), APPROVE)
        self.assertEqual(len(self.turns(fake, ADVERSARY)), 1)
        self.assertEqual([event["round"] for event in self.events()], [1])

    def test_a_fix_touching_a_gated_path_runs_a_light_fix_pass(self):
        self.configure(ON)
        fake, _ = self.loop(Change("poetry.lock"), REQUEST_CHANGES,
                            attack(self.CONCERN), attack(), Consolidate(),
                            Change("poetry.lock", "relocked\n"), APPROVE)
        first, second = (turn.candidate_sha
                         for turn in self.turns(fake, "review"))
        goal = self.turns(fake, ADVERSARY)[1].goal
        self.assertIn("Depth: light", goal)
        self.assertIn("Scope: fix", goal)
        self.assertIn(f"{first}..{second}", goal)
        self.assertIn("a symlink may slip by", goal)
        events = self.events()
        self.assertEqual([(e["round"], e["scope"]) for e in events],
                         [(1, "candidate"), (2, "fix")])
        self.assertEqual(events[1]["range"], [first, second])


class RefusalTests(AdversaryFixture):
    def test_a_refused_turn_is_recorded_once_and_the_primary_review_stands(self):
        self.configure(ON)
        fake, _ = self.loop(Change("poetry.lock"), APPROVE, Attack(REFUSAL))
        self.assertEqual(len(self.turns(fake, ADVERSARY)), 1)
        [event] = self.events()
        self.assertEqual((event["outcome"], event["findings"],
                          event["concerns"]), ("refused", [], []))
        self.assertEqual(self.round_row(1), ("pass", []))
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("merged", None)])
        self.assertEqual(self.read(
            "SELECT COUNT(*) FROM runEvents WHERE kind = 'route_fallback'"),
            [(0,)])
        [note] = [text for (text,) in self.read(
            "SELECT text FROM ledger WHERE kind = 'note'")
            if "adversary" in text]
        self.assertIn("refused by the provider", note)
        self.assertIn("stands on the primary review", note)

    def test_a_finished_reply_quoting_the_refusal_line_parses_as_findings(self):
        self.configure(ON)
        self.loop(Change("poetry.lock"), APPROVE, attack(
            finding("src/review.py", 8, f"the reply text '{REFUSAL}' is "
                    "matched anywhere", "concern")))
        [event] = self.events()
        self.assertEqual(event["outcome"], "clear")
        self.assertEqual([c["path"] for c in event["concerns"]],
                         ["src/review.py"])


class FailureTests(AdversaryFixture):
    def test_a_reply_without_the_done_line_twice_fails_the_route(self):
        self.configure(ON)
        self.loop(Change("poetry.lock"), APPROVE, Attack("I looked."),
                  Attack("Still looking."))
        self.assertEqual(self.read("SELECT outcome, failureKind FROM runs"),
                         [("failed", "review_route")])
        self.assertIn(BRANCH, self.branches())
        self.assertIn("change poetry.lock", self.subjects(BRANCH))

    def test_an_outage_switches_the_adversary_to_its_probed_fallback(self):
        calls = self.db.parent / "fallback-calls"
        script = self.db.parent / "adversary-fallback"
        script.write_text(
            f"#!{sys.executable}\nimport subprocess, sys\n"
            f"open({str(calls)!r}, 'a').write(sys.argv[-1][:40] + '\\n')\n"
            f"if sys.argv[-1] == {probes.REVIEW_PROBE_GOAL!r}:\n"
            " print('ready ' + subprocess.check_output("
            "['git', 'rev-parse', 'HEAD'], text=True).strip())\n"
            "else:\n print('Nothing broke.\\nADVERSARY: DONE')\n")
        script.chmod(0o755)
        self.configure(ON + f'[agents]\nadversary_fallback = "{script}"\n')
        self.addCleanup(reset, self.project)
        containers = []

        def run_review(*, candidate_sha, prompt, **kwargs):
            containers.append(kwargs.get("multi_agent", False))
            if prompt == probes.REVIEW_PROBE_GOAL:
                return f"ready {candidate_sha}"
            return "ERROR: You've hit your usage limit. Try again later."

        class RealAdversary(FakeAgent):
            def __call__(self, target, role, goal, cwd, **kwargs):
                if role == ADVERSARY:
                    return roles.agent(target, role, goal, cwd, **kwargs)
                return super().__call__(target, role, goal, cwd, **kwargs)

        fake = RealAdversary(Change("poetry.lock"), APPROVE)
        with patch.object(roles.review_runner, "run_review",
                          side_effect=run_review), \
                contextlib.redirect_stdout(io.StringIO()):
            self.loop(fake=fake)
        [switch] = [json.loads(summary) for (summary,) in self.read(
            "SELECT summary FROM runEvents WHERE kind = 'route_fallback'")]
        self.assertEqual(switch["seat"], "adversary")
        self.assertEqual([line[:31] for line in calls.read_text().splitlines()],
                         [probes.REVIEW_PROBE_GOAL[:31],
                          "You are a READ-ONLY adversarial"])
        self.assertEqual(containers, [False, True])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

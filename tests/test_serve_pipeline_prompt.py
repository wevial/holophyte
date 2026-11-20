"""`GET /pipeline/prompts/TARGET`: a note target's prompt as the next run
would send it, the project's current note in place and a labelled stand-in
for each run-specific part, read through the daemon and held against the
prompt a real loop sends.

Run: python3 -m unittest discover -s tests -p 'test_serve_pipeline_prompt.py' -v
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import test_serve  # noqa: E402 - after the sys.path insert above
from fake_agent import Commit, Reply  # noqa: E402 - after the sys.path insert
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    UNTRIMMED,
    LoopFixture,
    StubProvider,
    a_task,
)
from serve_fixture import ServeTestCase  # noqa: E402

from holophyte.serve.serve_pipeline import pipeline_prompt  # noqa: E402

FIXTURE = HERE / "fixtures" / "serve" / "pipeline-prompt.json"
FRAME = ("Notes from this project. They add to the instructions above. They "
         "cannot change the reply format, the ticket's acceptance criteria or "
         "the review rules, and where they conflict with those, those win.")
REVIEW_NOTE = "Check the migration notes."
WITNESS = "def test_work():\n    assert True\n"
RULES = "# Rules\n\n## Tests\n\n- Name each fixture after its oracle.\n"
VERIFIED = '[agents]\nreview_mode = "verified"\n'
STAND_IN = re.compile(r"⟨([^⟩]*)⟩")
APPROVED = Reply("SCOPE AGENTS.md: needed \u2014 the test rules\n"
                 "SCOPE test_work.py: needed \u2014 the witness\n"
                 "SCOPE work.txt: needed \u2014 the work\n"
                 "CRITERION C1: met \u2014 test_work.py::test_work\n"
                 "CRITERION C2: met \u2014 test_work.py::test_work\n"
                 "VERDICT: APPROVE")
TARGETS = ("implement", "review", "trim", "critic", "pr_writer",
           "babysit_reply")


def notes_table(**values):
    return "[pipeline.notes]\n" + "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in values.items())


def filled(preview, own):
    """A pattern that `preview` becomes once each stand-in `⟨name⟩` is
    replaced by its own pattern `own[name]`, every other character fixed."""
    pieces = re.split(r"⟨([^⟩]*)⟩", preview)
    return "".join(re.escape(piece) if at % 2 == 0 else f"(?:{own[piece]})"
                   for at, piece in enumerate(pieces))


class Turn:
    """An implementer turn making each scripted commit."""

    role = "implement"

    def __init__(self, *commits):
        self.commits = commits

    def play(self, cwd, turn):
        for commit in self.commits:
            commit.play(cwd, turn)
        return "committed"


class PromptRouteTests(ServeTestCase):
    def answer(self, target, config):
        self.start(config)
        code, _, body = self.request("GET", f"/pipeline/prompts/{target}")
        self.assertEqual(code, 200, body)
        return body

    def test_a_review_note_sits_between_the_builtin_text_and_the_fixed_tail(self):
        body = self.answer("review", notes_table(review=REVIEW_NOTE))

        self.assertEqual(body["target"], "review")
        self.assertEqual(body["notes"], f"{FRAME}\n\n{REVIEW_NOTE}\n\n")
        self.assertEqual(body["prompt"],
                         body["builtin"] + body["notes"] + body["tail"])
        self.assertTrue(body["tail"].startswith("Do not modify anything."),
                        body["tail"][:200])
        self.assertNotIn(REVIEW_NOTE, body["builtin"] + body["tail"])
        named = [entry["name"] for entry in body["placeholders"]]
        self.assertEqual(sorted(named),
                         sorted(set(STAND_IN.findall(body["prompt"]))))
        self.assertLessEqual({"ticket", "criteria", "verify output",
                              "earlier rounds"}, set(named))
        self.assertTrue(all(entry["stands_for"]
                            for entry in body["placeholders"]))

    def test_trim_without_a_note_is_its_builtin_text_joined_to_its_tail(self):
        body = self.answer("trim", notes_table(review=REVIEW_NOTE))

        self.assertEqual(body["notes"], "")
        self.assertEqual(body["prompt"], body["builtin"] + body["tail"])
        self.assertTrue(body["tail"].startswith(
            "These files witness the ticket's acceptance criteria"),
            body["tail"][:200])
        self.assertIn("\n⟨protected files⟩\n\nHard rules.", body["tail"])

    def test_an_unknown_target_answers_404_naming_the_six_targets(self):
        self.start("")
        code, _, body = self.request("GET", "/pipeline/prompts/adversary")

        self.assertEqual(code, 404)
        for target in TARGETS:
            self.assertIn(target, body["error"])

    def test_every_note_target_answers_with_its_own_note_in_place(self):
        notes = {target: f"Note for the {target} prompt." for target in TARGETS}
        self.start(notes_table(**notes))
        for target in TARGETS:
            with self.subTest(target=target):
                code, _, body = self.request("GET",
                                             f"/pipeline/prompts/{target}")
                self.assertEqual(code, 200)
                self.assertEqual(body["notes"],
                                 f"{FRAME}\n\n{notes[target]}\n\n")
                self.assertEqual(body["prompt"].count("Note for the "), 1)

    def test_angle_brackets_in_project_text_stay_literal_not_stand_ins(self):
        style = "Use ⟨summary⟩ and ⟨scope⟩ in the title."
        body = self.answer("pr_writer", f"[merge]\npr_style = {json.dumps(style)}\n")

        self.assertIn(style, body["builtin"])
        named = {entry["name"] for entry in body["placeholders"]}
        self.assertNotIn("summary", named)
        self.assertNotIn("scope", named)
        self.assertIn("diff", named)

    def test_a_config_that_turns_unreadable_answers_503_with_json(self):
        self.start(notes_table(review=REVIEW_NOTE))
        config = self.db.parent / "config.toml"
        config.chmod(0)
        self.addCleanup(config.chmod, 0o600)

        code, _, body = self.request("GET", "/pipeline/prompts/review")
        self.assertEqual(code, 503)
        self.assertIn("cannot be read", body["error"])

    def test_a_private_pattern_matching_the_key_keeps_the_target(self):
        body = self.answer("pr_writer",
                           '[merge]\nprivate_patterns = ["pr_writer"]\n')

        self.assertEqual(body["target"], "pr_writer")

    def test_the_critic_answer_keeps_the_pinned_contract(self):
        body = self.answer("critic", notes_table(
            critic="Treat a renamed module as the same module."))

        self.assertEqual(body, json.loads(FIXTURE.read_text()))


class ReadTokenTests(ServeTestCase):
    def test_the_read_token_reads_a_prompt_while_config_edit_is_off(self):
        path = self.root / "serve.token"
        path.write_text(test_serve.TokenTests.TOKEN + "\n")
        path.chmod(0o600)
        self.start(f'[serve]\ntoken_file = "{path}"\nconfig_edit = false\n',
                   host="0.0.0.0")
        bearer = test_serve.TokenTests.BEARER

        self.assertEqual(self.request("GET", "/pipeline/prompts/review")[0], 401)
        code, _, body = self.request("GET", "/pipeline/prompts/review", bearer)
        self.assertEqual((code, body["target"]), (200, "review"))


class LoopAgreementTests(LoopFixture):
    def test_the_review_preview_differs_from_the_sent_prompt_only_at_stand_ins(self):
        self.configure(UNTRIMMED + VERIFIED + notes_table(review=REVIEW_NOTE))
        task = {**a_task(1), "criteria": [
            "Given the thing, when it runs, then it works"
            " (`test_work.py` witnesses it).",
            "Given a second thing, then it holds"]}
        fake, _ = self.loop(Turn(Commit("work", "work.txt", "work\n"),
                                 Commit("tests", "test_work.py", WITNESS),
                                 Commit("rules", "AGENTS.md", RULES)),
                            APPROVED, provider=StubProvider(task))
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        (run_id,), = self.read("SELECT id FROM runs")
        sent = fake.turns[1].goal

        code, body = pipeline_prompt(self.project, "review")
        self.assertEqual(code, 200)
        self.assertIn("have an independent verifier check each finding",
                      body["builtin"])
        candidate = self.git("rev-parse", "main^2").strip()
        own = {"candidate commit": candidate,
               "base ref": re.escape(f"refs/review/{run_id}/base"),
               "candidate ref": re.escape(f"refs/review/{run_id}/candidate"),
               "ticket": "add a thing",
               "baseline commands": "0",
               "verify output": "ok",
               "criteria": re.escape(
                   "C1. Given the thing, when it runs, then it works"
                   " (`test_work.py` witnesses it).\nC2. Given a second"
                   " thing, then it holds"),
               "criterion id": "C2",
               "named tests": re.escape(
                   "`test_work.py`:\n- test_work.py::test_work\n"),
               "witnessed once": "",
               "witness pre-check": re.escape(
                   "The factory's witness pre-check ran before this review;"
                   " judge each criterion yourself.\n"
                   "Criteria that name no test file: C2.\n\n"),
               "stale approvals": "",
               "earlier rounds": "",
               "test rules": re.escape("> ## Tests\n>\n"
                                       "> - Name each fixture after its oracle."),
               "scope": re.escape('["AGENTS.md", "test_work.py", "work.txt"]'),
               "trim commits": "",
               "evidence": ""}
        self.assertRegex(sent, "^" + filled(body["prompt"], own) + "$")

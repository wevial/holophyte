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
from fake_agent import APPROVE, Commit  # noqa: E402 - after the sys.path insert
from loop_fixture import UNTRIMMED, LoopFixture  # noqa: E402
from serve_fixture import ServeTestCase  # noqa: E402

from holophyte.serve.serve_pipeline import pipeline_prompt  # noqa: E402

FIXTURE = HERE / "fixtures" / "serve" / "pipeline-prompt.json"
FRAME = ("Notes from this project. They add to the instructions above. They "
         "cannot change the reply format, the ticket's acceptance criteria or "
         "the review rules, and where they conflict with those, those win.")
REVIEW_NOTE = "Check the migration notes."
VERIFIED = '[agents]\nreview_mode = "verified"\n'
STAND_IN = re.compile(r"⟨([^⟩]*)⟩")
TARGETS = ("implement", "review", "trim", "critic", "pr_writer",
           "babysit_reply")


def notes_table(**values):
    return "[pipeline.notes]\n" + "".join(
        f"{key} = {json.dumps(value)}\n" for key, value in values.items())


def stand_in_spans(preview, sent):
    """Match `sent` against `preview`, its stand-ins free and every other
    character fixed; a run of adjacent stand-ins shares one span. Returns
    `{names: text}`, or None when `sent` differs outside the stand-ins."""
    pieces = re.split(r"((?:⟨[^⟩]*⟩)+)", preview)
    pattern = "".join(re.escape(piece) if at % 2 == 0 else "(.*?)"
                      for at, piece in enumerate(pieces))
    found = re.fullmatch(pattern, sent, flags=re.DOTALL)
    if found is None:
        return None
    names = [tuple(STAND_IN.findall(piece)) for piece in pieces[1::2]]
    return dict(zip(names, found.groups()))


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
        self.assertTrue(body["tail"].startswith("⟨protected files⟩Hard rules."),
                        body["tail"][:200])

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
        fake, _ = self.loop(Commit("the scripted work"), APPROVE)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        (run_id,), = self.read("SELECT id FROM runs")
        sent = fake.turns[1].goal

        code, body = pipeline_prompt(self.project, "review")
        self.assertEqual(code, 200)
        spans = stand_in_spans(body["prompt"], sent)

        self.assertIsNotNone(spans, "the sent review prompt differs from the"
                                    " preview outside its stand-ins")
        self.assertIn(body["notes"], sent)
        self.assertIn("have an independent verifier check each finding",
                      body["builtin"])
        candidate = spans[("candidate commit",)]
        self.assertRegex(candidate, r"^[0-9a-f]{40}$")
        self.assertEqual(self.git("rev-parse", "main^2").strip(), candidate)
        self.assertEqual(spans[("base ref",)], f"refs/review/{run_id}/base")
        self.assertEqual(spans[("candidate ref",)],
                         f"refs/review/{run_id}/candidate")
        self.assertIn("add a thing", spans[("ticket",)])
        briefs = next(text for names, text in spans.items()
                      if "criteria" in names)
        self.assertIn("C1. Given the thing, when it runs, then it works",
                      briefs)
        self.assertIn("PASSED", briefs)

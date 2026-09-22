"""End-to-end triage witnesses mixed into the babysitter thread suite."""

import io
import json
import os
import urllib.error
from unittest.mock import patch

from fake_agent import Commit, Idle, Reply
from loop_fixture import BRANCH


class TriageMentionCases:
    def triage_pass(self, choice="question", confidence=0.9, failure=None, marker=""):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        state = self.conversation_state(
            ("operator", "User"),
            "@holophyte " + marker + "Can an existing guest be renamed?",
        )
        self.resume_with_conversation(state, self.pr_state())
        sha = self.git("rev-parse", BRANCH).strip()
        answer = {
            "answers": {
                "q": {
                    "choice": choice,
                    "confidence": confidence,
                    "probabilities": {"fix": 0.1, "question": 0.8, "unclear": 0.1},
                }
            }
        }
        kwargs = (
            {"side_effect": failure}
            if isinstance(failure, Exception)
            else {"return_value": io.BytesIO(json.dumps(answer).encode())}
        )
        fix = marker == "fix: " or (
            not marker and choice == "fix" and confidence >= 0.6 and failure is None
        )
        turns = (
            (Commit("Rename guest"), Idle(""))
            if fix
            else (Reply("See src/app.py:30."),)
        )
        with (
            patch.dict(
                os.environ,
                {"TYPESAFE_API_KEY": ""}
                if failure == "no_key"
                else {"TYPESAFE_API_KEY": "triage-test-key"},
            ),
            patch("holophyte.questions.urllib.request.urlopen", **kwargs) as send,
        ):
            fake, _ = self.loop(*turns, provider=self.provider())
        self.assertEqual(send.call_count, 0 if marker or failure == "no_key" else 1)
        if fix:
            self.assertEqual(fake.roles, ["implement", "implement"])
            records = [
                f
                for (raw,) in self.read("SELECT findings FROM reviewRounds")
                for f in json.loads(raw)
                if f.get("kind") == "instruction"
            ]
            self.assertNotEqual(self.git("rev-parse", BRANCH).strip(), sha)
        else:
            self.assertEqual(fake.roles, ["adjudicate"])
            records = [
                json.loads(raw)
                for (raw,) in self.read(
                    "SELECT summary FROM runEvents WHERE kind = 'instruction'"
                )
            ]
            self.assertEqual(self.git("rev-parse", BRANCH).strip(), sha)
            body = next(d["body"] for k, d in self.api_calls() if k == "conversation")
            if not marker:
                self.assertTrue(
                    body.endswith("Reply with `fix:` to request a code change.")
                )
        if marker:
            self.assertNotIn("triage", records[-1])
            return
        result = records[-1]["triage"]
        reason = (
            "missing_key: TYPESAFE_API_KEY"
            if failure == "no_key"
            else "timeout"
            if isinstance(failure, TimeoutError)
            else "service_error"
            if failure
            else "low_confidence"
            if confidence < 0.6
            else choice
        )
        self.assertEqual(result["reason"], reason)
        self.assertEqual(result["confidence"], None if failure else confidence)
        self.assertEqual(
            result["decision"], "unclear" if failure or confidence < 0.6 else choice
        )
        self.assertEqual(result["route"], "fix" if fix else "answer")

    def test_bare_question(self):
        self.triage_pass()

    def test_bare_fix(self):
        self.triage_pass("fix")

    def test_low_confidence_fix(self):
        self.triage_pass("fix", 0.4)

    def test_unclear_mention(self):
        self.triage_pass("unclear")

    def test_triage_service_error(self):
        self.triage_pass(failure=urllib.error.URLError("unavailable"))

    def test_triage_timeout(self):
        self.triage_pass(failure=TimeoutError())

    def test_triage_no_key(self):
        self.triage_pass(failure="no_key")

    def test_marked_ask_skips_triage(self):
        self.triage_pass(marker="ask: ")

    def test_marked_fix_skips_triage(self):
        self.triage_pass(marker="fix: ")

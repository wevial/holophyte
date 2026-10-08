"""A typed question through a real, signed-in `claude` or `codex` CLI.

Run: HOLOPHYTE_LIVE_QUESTIONS=claude (or codex) python3 -m unittest discover
-s tests -p 'test_questions_live.py'
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.tickets
from holophyte import question_cli, questions
from holophyte.babysit import thread_mentions
from holophyte.loop.failure_triage import FAILURE_CAUSE

LIVE = os.environ.get("HOLOPHYTE_LIVE_QUESTIONS", "")
LABELLED = Path(__file__).resolve().parent / "fixtures" / "failures" / "labelled.jsonl"


@unittest.skipUnless(LIVE, "set HOLOPHYTE_LIVE_QUESTIONS=claude or codex")
class LiveQuestionTests(unittest.TestCase):
    def test_real_cli_answers_mention_intent_and_records_usage(self):
        self.assertIn(LIVE, ("claude", "codex"))
        self.assertIsNotNone(shutil.which(LIVE), f"{LIVE} is not on PATH")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.open(Path(tmp.name) / "store.db")
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team", "/repos/example")
        ticket = store.tickets.mirror_ticket(
            conn, project, "issue", "KO-1", "Example",
            acceptance_criteria=["works"], verification_commands=["true"])
        run_id = store.claim(conn, project, ticket)
        question_cli.ROUTES.clear()
        self.addCleanup(question_cli.ROUTES.clear)
        outputs = []
        real_launch = question_cli.launch

        def recorded(argv, cwd):
            status, output = real_launch(argv, cwd)
            outputs.append(output)
            return status, output

        with patch.object(question_cli, "launch", side_effect=recorded):
            answer = questions.ask(
                thread_mentions.MENTION_INTENT,
                {"comment": "fix: rename this helper to load_config"},
                config={"questions": {"backend": LIVE}}, conn=conn, run_id=run_id)
        self.assertIsInstance(answer, questions.Answer)
        self.assertIn(answer.choice, thread_mentions.MENTION_INTENT.criteria)
        (payload,) = conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'question'").fetchone()
        self.assertGreater(json.loads(payload)["input_tokens"], 0)
        if LIVE == "codex":
            items = [json.loads(line).get("item", {}).get("type")
                     for output in outputs for line in output.splitlines()
                     if line.startswith("{")]
            self.assertNotIn("command_execution", items)

    def test_real_cli_classifies_the_labelled_failures(self):
        self.assertIsNotNone(shutil.which(LIVE), f"{LIVE} is not on PATH")
        question_cli.ROUTES.clear()
        self.addCleanup(question_cli.ROUTES.clear)
        failures = {} if LIVE == "claude" else {"backend": LIVE}
        config = {"questions": {"failures": failures}}
        answers = {}
        for line in LABELLED.read_text().splitlines():
            row = json.loads(line)
            answer = questions.ask(FAILURE_CAUSE, row["state"], config=config,
                                   seat="failures")
            self.assertIsInstance(answer, questions.Answer, row["run"])
            self.assertIn(answer.choice, FAILURE_CAUSE.criteria)
            print(f"{row['ticket']} run {row['run']} ({row['label']}):"
                  f" {answer.choice} {answer.confidence}")
            answers[row["ticket"], row["run"]] = answer.choice
        self.assertEqual(answers["HOLO-164", 1019], "infra")


if __name__ == "__main__":
    unittest.main()

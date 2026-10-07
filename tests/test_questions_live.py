"""A typed question through a real, signed-in `claude` or `codex` CLI.

Run: HOLOPHYTE_LIVE_QUESTIONS=claude (or codex) python3 -m unittest discover
-s tests -p 'test_questions_live.py'
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.tickets
from holophyte import question_cli, questions
from holophyte.babysit import thread_mentions

LIVE = os.environ.get("HOLOPHYTE_LIVE_QUESTIONS", "")


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
        real_run = subprocess.run

        def recorded(*args, **kwargs):
            done = real_run(*args, **kwargs)
            outputs.append(done.stdout)
            return done

        with patch.object(question_cli.subprocess, "run", side_effect=recorded):
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


if __name__ == "__main__":
    unittest.main()

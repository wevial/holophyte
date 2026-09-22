"""The hand-run evaluator uses the same seam and enforces its accuracy floor."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from holophyte import questions
from scripts import eval_triage


class EvalTriageTests(unittest.TestCase):
    def test_lookup_service_counts_misses_and_exit(self):
        path = eval_triage.ROOT / "tests/fixtures/mentions/labelled.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertGreaterEqual(len(rows), 12)
        lookup = {r["comment"]: r["label"] for r in rows}
        lookup[rows[0]["comment"]] = "question"

        def service(question, state, *, config):
            return questions.Answer(lookup[state["comment"]], 0.9)

        for floor, status in (("0.8", 0), ("1.0", 1)):
            with (
                self.subTest(floor=floor),
                patch("holophyte.questions.ask", side_effect=service) as ask,
            ):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    result = eval_triage.main(["--min-accuracy", floor])
                self.assertEqual(result, status)
                self.assertEqual(ask.call_count, len(rows))
                text = out.getvalue()
                self.assertIn("fix: 3/4 correct", text)
                self.assertIn("question: 4/4 correct", text)
                self.assertIn("unclear: 4/4 correct", text)
                self.assertIn("Overall accuracy: 91.7% (11/12)", text)
                self.assertIn("MISS expected=fix got=question", text)
                self.assertIn(rows[0]["comment"], text)

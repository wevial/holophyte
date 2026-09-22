"""Typed questions reject malformed answers and keep credentials out of prose."""

import http.client
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.tickets
from holophyte import pr, questions, redact, thread_mentions


class QuestionsTests(unittest.TestCase):
    def test_request_context_and_redaction(self):
        secret = "typed-question-sentinel"
        response = {
            "answers": {
                "q": {
                    "choice": "question",
                    "confidence": 0.9,
                    "probabilities": {"fix": 0.05, "question": 0.9, "unclear": 0.05},
                }
            }
        }
        thread = pr.Thread(
            "1",
            secret,
            12,
            "writer",
            secret + "x" * 2000,
            "",
            replies=(pr.Comment("reader", secret + "y" * 5000),),
        )
        with (
            patch.dict(os.environ, {"TYPESAFE_API_KEY": secret}),
            patch(
                "holophyte.questions.urllib.request.urlopen",
                return_value=io.BytesIO(json.dumps(response).encode()),
            ) as send,
        ):
            result = thread_mentions.triage(thread, secret, {})
        self.assertEqual(result["decision"], "question")
        request = send.call_args.args[0]
        self.assertEqual(send.call_args.kwargs["timeout"], 10)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + secret)
        body = json.loads(request.data)
        self.assertNotIn(secret, request.data.decode())
        self.assertEqual(len(body["state"]["comment"]), 4000)
        self.assertEqual(len(body["state"]["earlier_comments"][0]), 1000)
        self.assertEqual(body["state"]["file"], "[redacted]")
        self.assertEqual(body["state"]["ticket_title"], "[redacted]")
        self.assertEqual(
            redact.redact_document({"event": secret}), {"event": "[redacted]"}
        )
        self.assertEqual(
            redact.redact_values("printed " + secret), "printed [redacted]"
        )

    def test_registered_key_is_redacted_in_events_and_prints(self):
        secret = "question-event-secret"
        with (
            patch.dict(os.environ, {"CUSTOM_QUESTION_KEY": secret}),
            patch(
                "holophyte.questions.urllib.request.urlopen", side_effect=TimeoutError()
            ),
        ):
            result = questions.ask(
                thread_mentions.MENTION_INTENT,
                {},
                config={"questions": {"key_env": "CUSTOM_QUESTION_KEY"}},
            )
        self.assertEqual(result.reason, "timeout")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.open(Path(tmp.name) / "events.db")
        self.addCleanup(conn.close)
        store.init(conn)
        project = store.tickets.ensure_project(conn, "team", "/repos/example")
        ticket = store.tickets.mirror_ticket(
            conn,
            project,
            "issue",
            "KO-1",
            "Example",
            acceptance_criteria=["works"],
            verification_commands=["true"],
        )
        run = store.claim(conn, project, ticket)
        store.record_event(conn, run, "example", secret)
        summary = conn.execute(
            "SELECT summary FROM runEvents WHERE kind = 'example'"
        ).fetchone()[0]
        self.assertEqual(summary, "[redacted]")
        out = io.StringIO()
        redact.safe_print("printed", secret, file=out)
        self.assertEqual(out.getvalue(), "printed [redacted]\n")

    def test_configuration_and_confidence_boundary(self):
        thread = pr.Thread("1", "app.py", 1, "author", "Rename it", "")
        for floor, decision in ((0.6, "fix"), (0.61, "unclear")):
            with patch(
                "holophyte.questions.ask", return_value=questions.Answer("fix", 0.6)
            ):
                result = thread_mentions.triage(
                    thread, "Title", {"questions": {"min_confidence": floor}}
                )
            self.assertEqual(result["decision"], decision)
        for value in (True, -1, 1.1, float("nan"), "0.6"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                questions.settings({"questions": {"min_confidence": value}})

    def test_truncated_http_response_fails_closed(self):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-question-key"}), patch(
            "holophyte.questions.urllib.request.urlopen",
            side_effect=http.client.IncompleteRead(b"partial"),
        ):
            result = questions.ask(thread_mentions.MENTION_INTENT, {}, config={})
        self.assertEqual(result, questions.Failure("service_error"))

    def test_invalid_responses_fail_closed(self):
        for answer in (
            None,
            {"choice": "fix", "confidence": 10**400, "probabilities": {}},
            {},
            {"choice": "fix", "confidence": 0.9},
            {"choice": "fix", "confidence": True, "probabilities": {}},
            {"choice": "fix", "confidence": 1.1, "probabilities": {}},
            {"choice": "other", "confidence": 0.9, "probabilities": {}},
        ):
            with (
                self.subTest(answer=answer),
                patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-question-key"}),
                patch(
                    "holophyte.questions.urllib.request.urlopen",
                    return_value=io.BytesIO(
                        json.dumps({"answers": {"q": answer}}).encode()
                    ),
                ),
            ):
                result = questions.ask(thread_mentions.MENTION_INTENT, {}, config={})
                self.assertIsInstance(result, questions.Failure)

"""Console contract: a failed attention row names a durable run card."""
import unittest

import store
from holophyte import board
from tests.serve_fixture import MIN, ServeTestCase


class FailedRunCardTests(ServeTestCase):
    def test_attention_names_a_run_that_remains_readable_after_requeue(self):
        self.seed()
        conn = store.open(str(self.db))
        try:
            store.release(conn, self.run, "failed", reason="Verification failed",
                          now=self.now)
        finally:
            conn.close()
        self.start()

        code, _, attention = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        failed = [item for item in attention["items"] if item["kind"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["run"], self.run)
        self.assertEqual(failed[0]["reason"], "Verification failed")
        path = f"/runs/{failed[0]['run']}"
        code, _, detail = self.request("GET", path)
        self.assertEqual(code, 200)
        self.assertEqual(detail["run"]["id"], self.run)
        self.assertEqual(detail["run"]["outcome"], "failed")
        self.assertEqual(detail["run"]["time_box_ms"], 25 * MIN)
        self.assertEqual(detail["run"]["ended_ms"], self.now)
        self.assertEqual(detail["rounds"], [])

        conn = store.open(str(self.db))
        try:
            ticket = store.read.run_snapshot(conn, self.run).ticketId
            store.requeue(conn, ticket, "operator requeued")
        finally:
            conn.close()
        code, _, attention = self.request("GET", "/attention")
        self.assertEqual(code, 200)
        self.assertEqual([item["run"] for item in attention["items"]
                          if item["kind"] == "failed"], [self.run])
        code, _, retained = self.request("GET", path)
        self.assertEqual(code, 200)
        self.assertEqual(retained["run"]["id"], self.run)
        self.assertEqual(retained["run"]["outcome"], "failed")
        self.assertEqual(retained["run"]["ended_ms"], self.now)


class CommentBodyTests(unittest.TestCase):
    def test_tool_banners_are_removed_and_findings_preserved(self):
        text = (
            "Round 1: changes requested\n"
            "Reading additional input from stdin...\n"
            "OpenAI Codex v0.154.0\n"
            "**OpenAI Codex v0.154.0**\n"
            "workdir: /workspace\nmodel: reviewer\nprovider: openai\n"
            "approval: never\nsandbox: read-only\n"
            "reasoning effort: high\nreasoning summaries: auto\n"
            "session id: example\n**session id: example**\n"
            "tokens used\n"
            "<!-- devin-review-badge-begin -->\n"
            "[Review badge](https://example.com)\n"
            "<!-- devin-review-badge-end -->\n"
            "\n\n\nReviewer findings:\n"
            "Keep the timeout finding and its proposed fix.\n"
            "The model: label in this sentence is prose.\n"
        )
        self.assertEqual(board.comment_body(text), (
            "Round 1: changes requested\n\n\nReviewer findings:\n"
            "Keep the timeout finding and its proposed fix.\n"
            "The model: label in this sentence is prose.\n"
        ))

    def test_long_body_is_capped_with_an_accurate_cut_count(self):
        result = board.comment_body("x" * 20000)
        head, closing = result.rsplit("\n", 1)
        self.assertEqual(head, "x" * 12000)
        self.assertEqual(closing, (
            "[... 8000 characters cut; "
            "the full round is on the run in the store]"
        ))
        self.assertLessEqual(len(result), 12000 + 1 + len(closing))

    def test_short_clean_body_is_unchanged(self):
        text = "Round 2: approved\n\nReviewer findings:\nNo findings.\n"
        self.assertEqual(board.comment_body(text), text)

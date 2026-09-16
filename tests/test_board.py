"""Console contract: a failed attention row names a durable run card."""
import store
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
        self.assertFalse(any(item["kind"] == "failed" for item in attention["items"]))
        code, _, retained = self.request("GET", path)
        self.assertEqual(code, 200)
        self.assertEqual(retained["run"]["id"], self.run)
        self.assertEqual(retained["run"]["outcome"], "failed")
        self.assertEqual(retained["run"]["ended_ms"], self.now)

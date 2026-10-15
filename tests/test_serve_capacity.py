"""`/status` serves the project's worker ceiling and each live run's
review round cap."""
import store
from holophyte.loop.runs import MAX_ROUNDS
from tests.serve_fixture import ServeTestCase


class StatusCapacityTests(ServeTestCase):
    def test_configured_workers_and_recorded_cap_are_served(self):
        self.seed()
        with store.open(str(self.db)) as conn:
            store.set_review_round_cap(conn, self.run, 3)
        self.start("[loop]\nworkers = 3\n")
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["workers"], 3)
        (run,) = body["runs"]
        self.assertEqual(run["max_rounds"], 3)

    def test_no_loop_table_and_no_cap_serve_the_defaults(self):
        self.seed()
        self.start()
        code, _, body = self.request("GET", "/status")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["workers"], 1)
        (run,) = body["runs"]
        self.assertEqual(run["max_rounds"], MAX_ROUNDS)

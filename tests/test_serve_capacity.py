"""`/status` serves the project's worker ceiling and each live run's
review round cap."""
import store
import store.tickets
from holophyte.loop.runs import MAX_ROUNDS
from tests.phase_fixture import advance_phase, park_run
from tests.serve_fixture import MIN, ServeTestCase

PR = "https://github.com/acme/repo/pull/2404"


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


class ServedRoundCapTests(ServeTestCase):
    def seed_babysat(self, cap):
        """KO-7's first run recorded `cap` and parked on its pull request;
        the babysit run that resumed it records none."""
        self.seed()
        first = self.run
        with store.open(str(self.db)) as conn:
            store.set_review_round_cap(conn, first, cap)
            park_run(conn, first, "awaiting_merge_approval", pr_url=PR,
                       now=self.now - MIN)
            ticket, project = conn.execute(
                "SELECT t.id, t.projectId FROM runs r JOIN tickets t"
                " ON t.id = r.ticketId WHERE r.id = ?", (first,)).fetchone()
            store.tickets.transition(conn, ticket, "blocked_on_operator")
            store.babysit(conn, ticket, "resume", now=self.now - MIN)
            self.run = store.claim(conn, project, ticket, now=self.now)
            advance_phase(conn, self.run, "reviewing", now=self.now)
        self.assertNotEqual(self.run, first)

    def test_the_recorded_cap_outranks_the_configured_rounds(self):
        self.seed_babysat(4)
        self.start("[loop]\nreview_rounds = 3\nreview_rounds_max = 4\n")
        _code, _, status = self.request("GET", "/status")
        (run,) = status["runs"]
        self.assertEqual((run["id"], run["max_rounds"]), (self.run, 4))
        code, _, detail = self.request("GET", f"/runs/{self.run}")
        self.assertEqual(code, 200, detail)
        self.assertEqual(detail["run"]["max_rounds"], 4)

    def test_a_ticket_with_no_recorded_cap_serves_the_configured_rounds(self):
        self.seed()
        self.start("[loop]\nreview_rounds = 3\n")
        _code, _, status = self.request("GET", "/status")
        (run,) = status["runs"]
        self.assertEqual(run["max_rounds"], 3)
        _code, _, detail = self.request("GET", f"/runs/{self.run}")
        self.assertEqual(detail["run"]["max_rounds"], 3)

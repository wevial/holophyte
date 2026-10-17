import store
import store.tickets
from tests.phase_fixture import finish_run
from tests.serve_fixture import MIN, ServeTestCase


class TicketCriteriaTests(ServeTestCase):
    def setUp(self):
        super().setUp()
        self.conn = store.open(str(self.db))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", self.target)

    def mirror(self, criteria):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-1",
            linear_identifier="DEMO-1", title="Stable criteria",
            acceptance_criteria=criteria, verification_commands=["echo ok"],
            time_box_ms=60 * MIN)

    def detail(self):
        self.conn.commit()
        self.start()
        code, _, body = self.request("GET", "/tickets/DEMO-1")
        self.assertEqual(code, 200, body)
        return body

    def test_current_criteria_keep_ids_after_a_retirement_and_addition(self):
        self.mirror(["First criterion", "Second criterion", "Retired criterion"])
        current = ["First criterion", "Second criterion", "Fourth criterion"]
        self.mirror(current)

        body = self.detail()

        self.assertEqual(body["acceptance_criteria"], current)
        self.assertEqual(body["criteria"], [
            {"id": "C1", "text": "First criterion"},
            {"id": "C2", "text": "Second criterion"},
            {"id": "C4", "text": "Fourth criterion"}])

    def test_merged_ticket_without_criterion_records_numbers_by_position(self):
        current = ["First criterion", "Second criterion"]
        ticket = self.mirror(current)
        store.tickets.transition(self.conn, ticket, "in_flight")
        run = store.claim(self.conn, self.project_id, ticket)
        finish_run(self.conn, run, "merged", "Done")
        store.tickets.transition(self.conn, ticket, "merged")
        self.conn.execute("DELETE FROM ticketCriteria WHERE ticketId = ?", (ticket,))

        body = self.detail()

        self.assertEqual(body["status"], "merged")
        self.assertEqual(body["acceptance_criteria"], current)
        self.assertEqual(body["criteria"], [
            {"id": "C1", "text": "First criterion"},
            {"id": "C2", "text": "Second criterion"}])

"""The daemon's run, history, shipped, ticket and board answers carry the revert a
merged revert ticket recorded on a merged run, and `/status` says whether
the project allows reverts; the event is written by the loop's real merge
recording against a native board's store and a real git repository.

Run: python3 -m unittest discover -s tests -p 'test_serve_reverted*.py' -v
"""
import contextlib
import http.client
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from phase_fixture import finish_run  # noqa: E402

import holophyte.config.project  # noqa: E402
import holophyte.serve.server  # noqa: E402
import store  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from tests import test_cli_revert as cli_revert  # noqa: E402
from tests import test_revert_close as revert_close  # noqa: E402


class RevertedResponseTests(ConfigTestCase):
    setUp = revert_close.RevertCloseTests.setUp
    git = revert_close.RevertCloseTests.git
    commit = revert_close.RevertCloseTests.commit
    project_with = revert_close.RevertCloseTests.project_with
    holo = revert_close.RevertCloseTests.holo
    read = revert_close.RevertCloseTests.read
    file_revert = revert_close.RevertCloseTests.file_revert
    run_revert = revert_close.RevertCloseTests.run_revert
    merge_the_revert = revert_close.RevertCloseTests.merge_the_revert
    ticket_id = staticmethod(revert_close.RevertCloseTests.ticket_id)

    def serve(self, target):
        project = holophyte.config.project.Project.locate(target)
        server = holophyte.serve.server.make_server(
            project, "127.0.0.1", 0, console_dir=self.root / "dist",
            token=holophyte.serve.server.resolve_token(project, "127.0.0.1"))
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            body = json.loads(response.read())
        finally:
            conn.close()
        self.assertEqual(response.status, 200, body)
        return body

    def reverted_nat_1(self):
        """NAT-3 reverts NAT-1's merge and merges; the revert's sha."""
        self.file_revert()
        self.run_revert(self.merge_the_revert)
        return self.git("rev-parse", "HEAD")

    def board_entry(self, port, key):
        return next(ticket for column in self.get(port, "/board")["columns"]
                    for ticket in column["tickets"] if ticket["ticket"] == key)

    def test_a_reverted_merged_run_names_its_revert_and_stays_merged(self):
        revert_sha = self.reverted_nat_1()
        port = self.serve(self.target)
        expected = {"by": "NAT-3", "sha": revert_sha,
                    "reason": revert_close.REASON}
        rows = {row["id"]: row
                for row in self.get(port, "/history?since=0")["rows"]}
        shipped = {row["id"]: row for row in self.get(port, "/shipped")["rows"]}
        for key, reverted in (("NAT-1", expected), ("NAT-2", None)):
            run = self.runs[key]
            with self.subTest(ticket=key):
                served = self.get(port, f"/runs/{run}")["run"]
                self.assertEqual(served["outcome"], "merged")
                self.assertEqual(served["reverted"], reverted)
                self.assertEqual(rows[run]["outcome"], "merged")
                self.assertEqual(rows[run]["reverted"], reverted)
                self.assertEqual(shipped[run]["reverted"], reverted)

    def test_a_ticket_carries_the_revert_of_its_latest_merged_run_only(self):
        revert_sha = self.reverted_nat_1()
        port = self.serve(self.target)
        expected = {"by": "NAT-3", "sha": revert_sha,
                    "reason": revert_close.REASON}
        self.assertEqual(self.get(port, "/tickets/NAT-1")["reverted"],
                         expected)
        self.assertEqual(self.board_entry(port, "NAT-1")["reverted"], expected)
        self.assertIsNone(self.get(port, "/tickets/NAT-2")["reverted"])
        self.assertIsNone(self.board_entry(port, "NAT-2")["reverted"])

        with contextlib.closing(open_store(self.project)) as conn:
            (project_id,) = conn.execute("SELECT id FROM projects").fetchone()
            ticket_id = self.ticket_id(conn, "NAT-1")
            run = store.claim(conn, project_id, ticket_id)
            store.tickets.transition(conn, ticket_id, "in_flight")
            store.set_phase(conn, run, "working")

        self.assertEqual(self.get(port, "/tickets/NAT-1")["reverted"],
                         expected)
        self.assertEqual(self.board_entry(port, "NAT-1")["reverted"], expected)

        with contextlib.closing(open_store(self.project)) as conn:
            finish_run(conn, run, "merged", merge_sha=self.commit(
                {"src/header.py": "header, fixed\n"}, "NAT-1: header again"))
            store.tickets.transition(conn, ticket_id, "merged")

        self.assertIsNone(self.get(port, "/tickets/NAT-1")["reverted"])

    def test_status_reports_reverts_from_each_projects_revert_table(self):
        for config, reverts in ((cli_revert.ENABLED, True),
                                (cli_revert.NATIVE, False)):
            with self.subTest(reverts=reverts):
                self.project_with(config, {"src/header.py": "broken\n"})
                self.assertEqual("[revert]" in config, reverts)
                port = self.serve(self.target)
                self.assertIs(self.get(port, "/status")["reverts"], reverts)

    def test_status_reads_reverts_from_the_saved_config_on_each_request(self):
        self.project_with(cli_revert.ENABLED, {"src/header.py": "broken\n"})
        port = self.serve(self.target)
        self.assertIs(self.get(port, "/status")["reverts"], True)
        for config in (cli_revert.NATIVE, *(
                cli_revert.NATIVE + f'[revert]\nenabled = {enabled}\n'
                'refuse_paths = [""]\n' for enabled in ("false", "true"))):
            with self.subTest(config=config):
                self.project.config_path.write_text(config)
                self.assertIs(self.get(port, "/status")["reverts"], False)

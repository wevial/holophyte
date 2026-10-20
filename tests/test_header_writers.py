"""A `+++` header ticket keeps its fields where it is written or sent: to and
from Linear, through a dependency rewrite and in the capture brief and the
pull request's Evidence lines.

Run: python3 -m unittest discover -s tests -p 'test_header_writers.py' -v
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import linear_provider
import provider
import store
import store.board
import store.tickets
import ticket_depends
import ticket_template
from holophyte.pr import pr_media
from tests import test_cli_file_ticket
from tests.test_cli_file_ticket import FakeLinear
from tests.test_ticket_header import BODY, HEADER, header_body

TOML_LINES = HEADER.split("\n")[1:-2]
REST = BODY[len(HEADER):]


class KeepingLinear(FakeLinear):
    """A board that stores each description as it was sent."""

    def __call__(self, query, variables=None):
        if "issueCreate" in query or "issueUpdate" in query:
            self.stored = variables["input"]["description"]
        return super().__call__(query, variables)


class LinearHeaderTests(unittest.TestCase):
    def sent(self):
        linear = FakeLinear()
        with patch.object(linear_provider, "_gql", linear):
            linear_provider.create_issue("p-1", "T", "Show the row defaults",
                                         BODY, 45, "Todo")
            linear_provider.update_issue("KO-9", "Show the row defaults", BODY, 45)
        updates = linear.updates()
        self.assertEqual(len(updates), 1)
        return linear.created()["description"], updates[0][1]["description"]

    def test_the_header_is_sent_as_a_toml_code_block_above_the_h1(self):
        created, updated = self.sent()

        self.assertEqual(created, "```toml\n" + "\n".join(TOML_LINES)
                         + "\n```\n" + REST)
        self.assertEqual(updated, created)

    def test_the_sent_description_reads_back_as_the_header_body(self):
        description, _ = self.sent()
        issue = {"identifier": "KO-9", "title": "Show the row defaults",
                 "description": description}
        with patch.object(linear_provider, "_gql", FakeLinear(stored=description)):
            fetched = linear_provider.fetch_description("KO-9")

        task = linear_provider.parse_task(issue)

        self.assertEqual(task["body"].encode(), BODY.encode())
        self.assertEqual(fetched.encode(), BODY.encode())
        original = provider.parse_body("KO-9", BODY)
        self.assertEqual((task["criteria"], task["verify"]),
                         (original["criteria"], original["verify"]))

    def test_a_description_without_a_toml_block_above_its_h1_reads_unchanged(self):
        below = REST + "\n```toml\nestimate = 1\n```\n"
        for description in (REST, below):
            with self.subTest(description=description[-30:]):
                issue = {"identifier": "KO-9", "title": "T",
                         "description": description}
                with patch.object(linear_provider, "_gql",
                                  FakeLinear(stored=description)):
                    fetched = linear_provider.fetch_description("KO-9")
                self.assertEqual(linear_provider.parse_task(issue)["body"],
                                 description)
                self.assertEqual(fetched, description)


class FileHeaderTicketCliTests(unittest.TestCase):
    cli_tests = test_cli_file_ticket.FileTicketCliTests
    setUp, with_board, cli = cli_tests.setUp, cli_tests.with_board, cli_tests.cli

    def test_a_header_ticket_is_filed_with_its_estimate_and_passes_its_recheck(self):
        self.with_board()
        self.ticket.write_text(header_body("Add export endpoint",
                                           "estimate = 40\n").replace(
            "python3 -m unittest discover -s tests -p 'test_thing.py'",
            ".venv/bin/python -m unittest test_orders_export"))
        linear = KeepingLinear()

        status, printed = self.cli(linear=linear)

        self.assertEqual(status, 0, printed)
        self.assertNotIn("as stored by Linear", printed)
        self.assertEqual(linear.created()["estimate"], 40)


DEPENDS_HEADER = '''+++
# typed fields
ui_change = "minor"
estimate = 30
depends_on = ["HOLO-1", "HOLO-2"]
implementer = "fast"

[mockup]
url = "https://claude.ai/artifact/abc123"
approved = 2026-10-19
note = "the row"
+++
'''
DEPENDS_BODY = DEPENDS_HEADER + REST
DEPENDS_LINE = 'depends_on = ["HOLO-1", "HOLO-2"]\n'


class RewriteDependsTests(unittest.TestCase):
    def test_the_depends_on_line_alone_is_rewritten(self):
        for rewritten, line in ((["HOLO-3"], 'depends_on = ["HOLO-3"]\n'),
                                ([], "depends_on = []\n")):
            with self.subTest(rewritten=rewritten):
                result = ticket_depends.rewrite_depends(
                    DEPENDS_BODY, lambda deps: rewritten)

                self.assertEqual(result, DEPENDS_BODY.replace(DEPENDS_LINE, line))
                self.assertEqual(ticket_template.parse(result).depends_on,
                                 rewritten)

    def test_a_multi_line_depends_on_becomes_one_line(self):
        body = DEPENDS_BODY.replace(DEPENDS_LINE,
                                    'depends_on = [\n  "HOLO-1",\n  "HOLO-2",\n]\n')

        result = ticket_depends.rewrite_depends(body, lambda deps: ["HOLO-3"])

        self.assertEqual(result, DEPENDS_BODY.replace(
            DEPENDS_LINE, 'depends_on = ["HOLO-3"]\n'))

    def test_a_missing_depends_on_is_added_above_the_first_table(self):
        body = DEPENDS_BODY.replace(DEPENDS_LINE, "")

        result = ticket_depends.rewrite_depends(body, lambda deps: ["HOLO-3"])

        self.assertEqual(result, body.replace(
            'implementer = "fast"\n',
            'implementer = "fast"\ndepends_on = ["HOLO-3"]\n'))
        self.assertLess(result.index("depends_on"), result.index("[mockup]"))
        self.assertEqual(ticket_template.parse(result).depends_on, ["HOLO-3"])


class CancelRewireTests(unittest.TestCase):
    def test_canceling_a_dependency_empties_a_header_dependents_depends_on(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.open(str(Path(tmp.name) / "store.db"))
        self.addCleanup(conn.close)
        store.init(conn)
        (Path(tmp.name) / "tests").mkdir()
        (Path(tmp.name) / "tests" / "test_thing.py").write_text("")
        project_id = store.tickets.ensure_project(conn, "native:NAT",
                                                  Path(tmp.name))
        store.board.file_ticket(conn, project_id, "NAT",
                                header_body("First", "estimate = 30\n"))
        store.board.file_ticket(conn, project_id, "NAT", header_body(
            "Second", 'estimate = 30\ndepends_on = ["NAT-1"]\n'))
        (revision,) = conn.execute(
            "SELECT revision FROM tickets WHERE linearIdentifier = 'NAT-1'"
        ).fetchone()

        store.board.cancel_ticket(conn, project_id, "NAT-1", revision,
                                  "not needed")

        body, depends = conn.execute(
            "SELECT body, dependsOn FROM tickets WHERE linearIdentifier = 'NAT-2'"
        ).fetchone()
        self.assertIn("depends_on = []\n", body.split("+++")[1])
        self.assertEqual(json.loads(depends), [])


EVIDENCE_HEADER = '''+++
estimate = 30

[[evidence]]
file = "01-row-dawn.png"
state = "the lane in Dawn"

[[evidence]]
file = "{second}"
state = "{state}"
+++
'''


def evidence_ticket(second, state):
    return EVIDENCE_HEADER.format(second=second, state=state) + REST


class CaptureBriefTests(unittest.TestCase):
    def setUp(self):
        config = {"merge": {"ui_paths": ["console/src/**"],
                            "ui_capture": "python3 capture.py"}}
        self.target = SimpleNamespace(path=Path("/nonexistent"),
                                      config=lambda: config,
                                      config_path=Path("/nonexistent/c.toml"))

    def test_a_header_brief_names_each_entry_by_its_file(self):
        ticket = evidence_ticket("02-row-night.png", "the same lane in Night")

        brief = pr_media.implementer_brief(self.target, ticket, "KO-9")

        lines = brief.split("\n")
        self.assertIn("01-row-dawn.png: the lane in Dawn", lines)
        self.assertIn("02-row-night.png: the same lane in Night", lines)


class RecordingEvidenceTests(unittest.TestCase):
    def evidence_lines(self, published):
        parsed = ticket_template.parse(
            evidence_ticket("02-flow.webm", "the send-back flow"))
        urls = {Path("/out", name): f"https://media.example/{name}"
                for name in published}
        return pr_media._state_media(parsed.evidence_states, urls,
                                     parsed.evidence_files)[0]

    def test_a_webm_entry_is_met_by_its_published_recording(self):
        self.assertEqual(
            self.evidence_lines(["01-row-dawn.png", "02-flow.webm"]),
            ["- the lane in Dawn — captured",
             "![the lane in Dawn](https://media.example/01-row-dawn.png)",
             "- the send-back flow — captured",
             "[the send-back flow](https://media.example/02-flow.webm)"])

    def test_a_webm_entry_without_its_recording_is_not_captured(self):
        self.assertEqual(
            self.evidence_lines(["01-row-dawn.png", "03-other.webm"]),
            ["- the lane in Dawn — captured",
             "![the lane in Dawn](https://media.example/01-row-dawn.png)",
             "- the send-back flow — not captured"])

if __name__ == "__main__":
    unittest.main()

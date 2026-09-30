"""`--gap-layer --found-by` records who found a gap; `--report` counts gaps by
finder and shows each story's durations, children, toil and first greens.

Run: python3 -m unittest discover -s tests -p 'test_story_report.py' -v
"""
from __future__ import annotations

import contextlib
import io

import holophyte.cli.entry
import store.board
from store.gap_layers import record_gap_layer
from store.operate import record_intervention
from store.stories import (
    approve_story,
    close_story,
    file_story,
    record_witness_result,
)
from store.tickets import walk_ticket
from tests.sweep_fixture import T0, SweepTestCase
from tests.test_store_board import body

HOUR = 3600 * 1000
WITNESSES = [
    {"key": key, "criterion": f"outcome {key}", "file": f"tests/test_{key}.py",
     "command": f"python3 -m unittest tests.test_{key}", "source": "pass\n"}
    for key in ("W1", "W2")]


class StoryReportTests(SweepTestCase):
    def file(self, title):
        identifier = store.board.file_ticket(
            self.conn, self.project_id, "NAT", body(title), column="backlog",
            now=T0)
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                code = holophyte.cli.entry.cli([str(self.target), *args])
            except SystemExit as exited:
                code = exited.code
        return code or 0, out.getvalue()

    def found_by(self):
        return [row[0] for row in self.conn.execute(
            "SELECT foundBy FROM gapLayers ORDER BY id")]

    def test_found_by_records_the_finder_defaults_and_refuses_an_unknown(self):
        self.file("a gap")
        code, _ = self.cli("--gap-layer", "NAT-1", "witness", "--note",
                           "caught by W2", "--found-by", "witness")
        self.assertEqual(code, 0)
        self.assertEqual(self.found_by(), ["witness"])
        code, _ = self.cli("--gap-layer", "NAT-1", "witness", "--note", "x")
        self.assertEqual(code, 0)
        (default,) = self.conn.execute(
            "SELECT dflt_value FROM pragma_table_info('gapLayers')"
            " WHERE name = 'foundBy'").fetchone()
        self.assertEqual(self.found_by(), ["witness", default.strip("'")])
        code, out = self.cli("--gap-layer", "NAT-1", "witness", "--note", "x",
                             "--found-by", "robot")
        self.assertEqual(code, 2, out)
        self.assertEqual(len(self.found_by()), 2)

    def test_report_counts_each_tickets_latest_finder(self):
        first, second = self.file("one gap"), self.file("another gap")
        record_gap_layer(self.conn, first, "static", "a", "operator")
        record_gap_layer(self.conn, first, "witness", "b", "operator",
                         found_by="witness")
        record_gap_layer(self.conn, second, "witness", "c", "operator",
                         found_by="witness")
        record_gap_layer(self.conn, second, "review", "d", "operator")
        code, out = self.cli("--report")
        self.assertEqual(code, 0)
        self.assertIn("gaps found: witness 1, operator 1", out.splitlines())

    def closed_story(self):
        parent = self.file("Guest checkout")
        children = [self.file(f"Guest checkout step {n}") for n in (1, 2, 3, 4)]
        file_story(self.conn, parent, WITNESSES,
                   [(child, "advances", ("W1",)) for child in children[:-1]]
                   + [(children[-1], "completes", ("W1", "W2"))])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go",
                      now=T0 + 2 * HOUR)
        for child in children[:2]:
            walk_ticket(self.conn, child, "in_flight")
            run = self.a_run(ticket=child)
            for n in range(3):
                record_intervention(self.conn, run, "operator_note", f"note {n}")
        record_intervention(self.conn, self.a_run(), "operator_note", "not a child")
        for child in children[:3]:
            walk_ticket(self.conn, child, "merged")
        walk_ticket(self.conn, children[3], "abandoned")
        for key, sha, verdict in (("W1", "0f0f0f", "red"),
                                  ("W1", "a1b2c3", "green"),
                                  ("W2", "a1b2c3", "absent"),
                                  ("W1", "d4e5f6", "green"),
                                  ("W2", "d4e5f6", "green")):
            record_witness_result(
                self.conn, parent, key, sha, verdict, "loop",
                red_kind="assert" if verdict == "red" else None)
        close_story(self.conn, parent, "d4e5f6", "done", now=T0 + 26 * HOUR)

    def test_report_shows_a_closed_storys_evidence(self):
        self.closed_story()
        code, out = self.cli("--report")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        first = lines.index("Stories:") + 1
        self.assertTrue(lines[first].startswith("NAT-1 "), lines[first])
        for part in ("2h to approval, 24h to close",
                     "children 3 merged, 1 abandoned",
                     "interventions per child merge 2.0"):
            self.assertIn(part, lines[first])
        self.assertIn("W1 first green a1b2c3", lines[first + 1])
        self.assertIn("W2 first green d4e5f6", lines[first + 1])

    def test_report_without_a_story_has_no_stories_heading(self):
        code, out = self.cli("--report")
        self.assertEqual(code, 0)
        self.assertIn("gaps found: witness 0, operator 0", out.splitlines())
        self.assertNotIn("Stories", out)


if __name__ == "__main__":
    import unittest
    unittest.main()

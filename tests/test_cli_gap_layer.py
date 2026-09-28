"""`--gap-layer` records where a gap's lesson landed; `--report` counts them."""
import contextlib
import getpass
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import holophyte.cli
import store.tickets
from holophyte.project import Project
from holophyte.runs import open_store
from store.gap_layers import record_gap_layer

LAYERS = ("impossible", "static", "witness", "guidance", "review", "none")


class GapLayerFlagTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        env.start()
        self.addCleanup(env.stop)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.target = Project.locate(self.repo)
        self.conn = open_store(self.target)
        self.addCleanup(self.conn.close)
        self.project = store.tickets.ensure_project(self.conn, "team", self.repo)
        self.tickets = {n: self.mirror(n) for n in (1, 2, 3)}
        self.board = Mock()

    def mirror(self, number):
        return store.tickets.mirror_ticket(
            self.conn, self.project, linear_issue_id=f"issue-{number}",
            linear_identifier=f"KO-{number}", title="a gap",
            acceptance_criteria=["The gap is answered"],
            verification_commands=["echo ok"], time_box_ms=60000)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("holophyte.cli.board_for", return_value=self.board), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                holophyte.cli.cli([str(self.repo), *args])
            except SystemExit as exited:
                return exited.code, out.getvalue() + err.getvalue()
        return 0, out.getvalue()

    def rows(self):
        return self.conn.execute(
            "SELECT ticketId, layer, note, author, carriedBy FROM gapLayers"
            " ORDER BY id").fetchall()

    def interventions(self):
        return self.conn.execute(
            "SELECT COUNT(*) FROM interventions").fetchone()[0]

    def test_records_one_row_and_no_intervention(self):
        before = self.interventions()
        code, out = self.cli("--gap-layer", "KO-1", "impossible", "--note",
                             "unproducible Evidence refused at filing")
        self.assertEqual(code, 0)
        (line,) = out.splitlines()
        self.assertIn("KO-1", line)
        self.assertIn("impossible", line)
        self.assertEqual(self.rows(), [
            (self.tickets[1], "impossible",
             "unproducible Evidence refused at filing", getpass.getuser(),
             None)])
        self.assertEqual(self.interventions(), before)
        self.assertEqual(self.board.mock_calls, [])

    def test_carried_by_names_the_ticket_carrying_the_lesson(self):
        code, _ = self.cli("--gap-layer", "KO-1", "witness", "--note", "x",
                           "--carried-by", "HOLO-7")
        self.assertEqual(code, 0)
        self.assertEqual([row[4] for row in self.rows()], ["HOLO-7"])

    def test_unknown_layer_is_a_usage_error_naming_the_six(self):
        code, out = self.cli("--gap-layer", "KO-1", "lint", "--note", "x")
        self.assertEqual(code, 2)
        for layer in LAYERS:
            self.assertIn(layer, out)
        self.assertEqual(self.rows(), [])

    def test_missing_note_and_stray_carried_by_are_usage_errors(self):
        for args in (("--gap-layer", "KO-1", "witness"),
                     ("--carried-by", "HOLO-7")):
            with self.subTest(args=args):
                code, _ = self.cli(*args)
                self.assertEqual(code, 2)
        self.assertEqual(self.rows(), [])

    def test_unknown_ticket_exits_naming_it(self):
        code, out = self.cli("--gap-layer", "KO-99", "witness", "--note", "x")
        self.assertNotEqual(code, 0)
        self.assertIn("KO-99", str(code) + out)
        self.assertEqual(self.rows(), [])

    def test_report_counts_each_ticket_at_its_latest_layer(self):
        for number, layer in ((1, "impossible"), (2, "witness"),
                              (2, "static"), (3, "witness")):
            record_gap_layer(self.conn, self.tickets[number], layer, "lesson",
                             "operator")
        _, out = self.cli("--report")
        self.assertIn("gap layers: impossible 1, static 1, witness 1,"
                      " guidance 0, review 0, none 0", out.splitlines())

    def test_report_on_a_store_with_no_rows_counts_zero(self):
        _, out = self.cli("--report")
        self.assertIn("gap layers: impossible 0, static 0, witness 0,"
                      " guidance 0, review 0, none 0", out.splitlines())


if __name__ == "__main__":
    unittest.main()

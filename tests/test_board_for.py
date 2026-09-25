"""`provider.board_for()`: the one constructor of a target's board (KO-731).

No `[board]` table is no board; `kind = "linear"`, the default, is a
`LinearBoard` of the table's `project_id`, `team` and `label`; `kind =
"native"` is refused, directly and where the loop starts, because this
build has no native board and a native project must not run against Linear.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

import holophyte.cli  # noqa: E402
import linear_provider  # noqa: E402
from provider import board_for  # noqa: E402
from tests.test_provider import FakeLinear, ticket_body  # noqa: E402

BOARD = '[board]\nproject_id = "p-1"\nteam = "T"\nlabel = "holophyte"\n'


class BoardForTests(ConfigTestCase):
    def test_no_table_is_no_board(self):
        self.assertIsNone(board_for(self.locate()))

    def test_a_linear_table_is_a_board_on_its_project_team_and_label(self):
        """With and without `kind`, the board is the table's: its `team`,
        and a ready listing asked of project `p-1` keeping only the issue
        carrying the label."""
        for config in (BOARD, BOARD + 'kind = "linear"\n'):
            with self.subTest(config=config):
                board = board_for(self.locate(config))
                self.assertEqual(board.team, "T")
                linear = FakeLinear()
                linear.add("KO-1", "plain", ticket_body("plain"))
                linear.add("KO-2", "labelled", ticket_body("labelled"))
                linear.issues["KO-2"]["labels"]["nodes"].append(
                    {"id": "label-holophyte", "name": "holophyte"})
                with patch.object(linear_provider, "_gql", linear.gql):
                    ready = board.ready_issues()
                self.assertEqual([task["id"] for task in ready], ["KO-2"])
                self.assertIn("p-1", [variables.get("project")
                                      for _, variables in linear.calls])

    def test_a_native_board_is_refused_directly_and_at_loop_start(self):
        target = self.locate(BOARD + 'kind = "native"\n')
        with self.assertRaises(SystemExit) as direct:
            board_for(target)
        with patch.object(holophyte.cli, "check_agent_commands"), \
                patch.object(holophyte.cli, "check_worktree_setup"), \
                patch.object(holophyte.cli, "main") as main, \
                self.assertRaises(SystemExit) as started:
            holophyte.cli.cli([str(target.path)])
        main.assert_not_called()
        for raised in (direct, started):
            message = str(raised.exception)
            self.assertIn(str(target.config_path), message)
            self.assertIn("[board] kind", message)

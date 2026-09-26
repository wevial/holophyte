"""`[board] kind = "native"`'s own table shape (KO-749).

A native table names its tickets' prefix with a required `key`, defaults
`team` (the store's project key) to `native:KEY` and `mode` to `"store"`,
and refuses Linear's `project_id` and `label` and `mode = "mirror"`. A
Linear table reads as it always has, with `key` refused and `None`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

from holophyte.config import check_config  # noqa: E402
from holophyte.config_tables import board_config, board_mode  # noqa: E402
from provider import LinearBoard, board_for  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "HOLO"\n'
LINEAR = '[board]\nproject_id = "p-1"\nteam = "T"\nlabel = "holophyte"\n'


class NativeBoardConfigTests(ConfigTestCase):
    def test_a_native_table_answers_its_key_and_the_native_defaults(self):
        """Only `kind` and `key`: team `native:HOLO`, no Linear project or
        label, mode `store`; a `team` set is the team read."""
        target = self.locate(NATIVE)
        check_config(target)
        settings = board_config(target)
        self.assertEqual(settings.key, "HOLO")
        self.assertEqual(settings.team, "native:HOLO")
        self.assertIsNone(settings.project_id)
        self.assertIsNone(settings.label)
        self.assertEqual(board_mode(target).mode, "store")

        target = self.locate(NATIVE + 'team = "KO team"\n')
        self.assertEqual(board_config(target).team, "KO team")
        self.assertEqual(board_config(target).key, "HOLO")

    def test_a_malformed_table_exits_naming_the_key_and_the_file(self):
        native = '[board]\nkind = "native"\n'
        cases = (
            (native, "[board] key"),
            (native + 'key = "holo"\n', "[board] key"),
            (native + 'key = "HOLO-1"\n', "[board] key"),
            (NATIVE + 'project_id = "p-1"\n', "[board] project_id"),
            (NATIVE + 'label = "holophyte"\n', "[board] label"),
            (NATIVE + 'mode = "mirror"\n', "[board] mode"),
            (LINEAR + 'key = "HOLO"\n', "[board] key"),
        )
        for config, named in cases:
            with self.subTest(config=config):
                target = self.locate(config)
                with self.assertRaises(SystemExit) as raised:
                    board_config(target)
                message = str(raised.exception)
                self.assertIn(named, message)
                self.assertIn(str(target.config_path), message)

    def test_a_linear_table_reads_as_today_and_builds_its_board(self):
        target = self.locate(LINEAR)
        settings = board_config(target)
        self.assertEqual(
            (settings.project_id, settings.team, settings.label, settings.key),
            ("p-1", "T", "holophyte", None))
        self.assertEqual(board_mode(target), ("mirror", "linear"))
        board = board_for(target)
        self.assertIsInstance(board, LinearBoard)
        self.assertEqual((board.project_id, board.team, board._label),
                         ("p-1", "T", "holophyte"))

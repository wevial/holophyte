"""`[board] kind = "native"`'s own table shape (KO-749).

A native table names its tickets' prefix with a required `prefix`, `key`
read as its deprecated alias (HOLO-8), defaults `team` (the store's project
key) to `native:PREFIX` and `mode` to `"store"`, and refuses Linear's
`project_id` and `label` and `mode = "mirror"`. A Linear table reads as it
always has, with `prefix` and `key` refused and `prefix` `None`.
"""
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert

from holophyte.config import check_config  # noqa: E402
from holophyte.config_tables import board_config, board_mode  # noqa: E402
from holophyte.native_board import NativeBoard  # noqa: E402
from provider import LinearBoard, board_for  # noqa: E402

NATIVE = '[board]\nkind = "native"\nprefix = "HOLO"\n'
ALIAS = '[board]\nkind = "native"\nkey = "HOLO"\n'
LINEAR = '[board]\nproject_id = "p-1"\nteam = "T"\nlabel = "holophyte"\n'


class NativeBoardConfigTests(ConfigTestCase):
    def test_a_native_table_answers_its_prefix_and_the_native_defaults(self):
        """Only `kind` and `prefix`: team `native:HOLO`, no Linear project or
        label, mode `store`; a `team` set is the team read."""
        target = self.locate(NATIVE)
        check_config(target)
        settings = board_config(target)
        self.assertEqual(settings.prefix, "HOLO")
        self.assertEqual(settings.team, "native:HOLO")
        self.assertIsNone(settings.project_id)
        self.assertIsNone(settings.label)
        self.assertEqual(board_mode(target).mode, "store")

        target = self.locate(NATIVE + 'team = "KO team"\n')
        self.assertEqual(board_config(target).team, "KO team")
        self.assertEqual(board_config(target).prefix, "HOLO")

    def test_prefix_and_its_key_alias_each_build_a_board_filing_holo_n(self):
        for config in (NATIVE, ALIAS):
            with self.subTest(config=config):
                target = self.locate(config)
                settings = board_config(target)
                self.assertEqual((settings.prefix, settings.team),
                                 ("HOLO", "native:HOLO"))
                board = board_for(target)
                self.assertIsInstance(board, NativeBoard)
                self.assertEqual(
                    board.file("Draft", "A draft.", 20, "Backlog"), "HOLO-1")

    def test_prefix_and_key_agree_or_are_refused(self):
        target = self.locate(NATIVE + 'key = "HOLO"\n')
        self.assertEqual(board_config(target).prefix, "HOLO")
        target = self.locate(NATIVE + 'key = "KO"\n')
        with self.assertRaises(SystemExit) as raised:
            board_config(target)
        message = str(raised.exception)
        for named in ("[board] prefix", "[board] key", str(target.config_path)):
            self.assertIn(named, message)

    def test_the_key_alias_prints_one_notice_per_check(self):
        for config, lines in ((ALIAS, 1), (NATIVE, 0)):
            with self.subTest(config=config):
                target = self.locate(config)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    check_config(target)
                notice = stderr.getvalue().splitlines()
                self.assertEqual(len(notice), lines)
                for line in notice:
                    for named in (str(target.config_path), "[board] key",
                                  "prefix"):
                        self.assertIn(named, line)

    def test_a_malformed_table_exits_naming_the_key_and_the_file(self):
        native = '[board]\nkind = "native"\n'
        cases = (
            (native, "[board] prefix"),
            (native + 'prefix = "holo"\n', "[board] prefix"),
            (native + 'prefix = "HOLO-1"\n', "[board] prefix"),
            (native + 'key = "HOLO-1"\n', "[board] key"),
            (NATIVE + 'project_id = "p-1"\n', "[board] project_id"),
            (NATIVE + 'label = "holophyte"\n', "[board] label"),
            (NATIVE + 'mode = "mirror"\n', "[board] mode"),
            (LINEAR + 'prefix = "HOLO"\n', "[board] prefix"),
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
            (settings.project_id, settings.team, settings.label, settings.prefix),
            ("p-1", "T", "holophyte", None))
        self.assertEqual(board_mode(target), ("mirror", "linear"))
        board = board_for(target)
        self.assertIsInstance(board, LinearBoard)
        self.assertEqual((board.project_id, board.team, board._label),
                         ("p-1", "T", "holophyte"))

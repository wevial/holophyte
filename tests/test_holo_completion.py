"""`holo completion SHELL` prints a thin script; `holo __complete WORDS...`
answers its candidates from the parser the command table builds, and ticket
keys from `holo board --json` cached per project for a minute.

Run: python3 -m unittest discover -s tests -p 'test_holo_completion.py' -v
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
import store
import store.board
import store.tickets
from holophyte.board.projection import FILE_TICKET_PRIORITIES
from holophyte.config.project import Project
from holophyte.holo.completion import cache_file
from holophyte.holo.grammar import ALIASES, COMMANDS, HELPER, READS, TICKET_VERBS
from store.enums import GapLayer
from tests.test_store_board import body as ticket_body

ROOT = Path(__file__).resolve().parent.parent


class CompletionCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.enterContext(patch.dict(os.environ,
                                     {"HOLOPHYTE_HOME": str(self.home)}))
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def register(self, name, prefix):
        path = self.root / name
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text(
            f'[board]\nkind = "native"\nprefix = "{prefix}"\n'
            f'[serve]\nname = "{name}"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(holophyte.cli.entry.cli(["project", "add", str(path)]))
        return path

    def environment(self):
        env = {key: value for key, value in os.environ.items()
               if key != "HOLO_PROJECT"}
        env.update(HOLOPHYTE_HOME=str(self.home), PYTHONPATH=str(ROOT),
                   GIT_CEILING_DIRECTORIES=str(self.root))
        return env

    def holo(self, *args):
        return subprocess.run([sys.executable, "-m", "holophyte.holo", *args],
                              cwd=self.outside, capture_output=True, text=True,
                              env=self.environment())

    def offered(self, *words):
        result = self.holo(HELPER, *words)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result.stdout.splitlines()


class BashScriptTests(CompletionCase):
    """The bash script sourced by real bash, its `holo` this checkout's."""

    def complete(self, words, cword):
        shims = self.root / "bin"
        shims.mkdir(exist_ok=True)
        shim = shims / "holo"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m holophyte.holo "$@"\n')
        shim.chmod(0o755)
        script = self.root / "holo.bash"
        result = self.holo("completion", "bash")
        self.assertEqual(result.returncode, 0, result.stderr)
        script.write_text(result.stdout)
        program = (f"source {script}\n"
                   "registered=$(complete -p holo)\n"
                   "function=${registered##*-F }; function=${function%% *}\n"
                   f"COMP_WORDS=({words}); COMP_CWORD={cword}\n"
                   '"$function"\nprintf "%s\\n" "${COMPREPLY[@]}"\n')
        env = self.environment()
        env["PATH"] = f"{shims}{os.pathsep}{env['PATH']}"
        result = subprocess.run(["bash", "--norc", "--noprofile", "-c", program],
                                cwd=self.outside, capture_output=True, text=True,
                                env=env)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result.stdout.splitlines()

    def test_a_command_prefix_completes_to_its_command(self):
        self.assertEqual(self.complete("holo req", 1), ["requeue"])

    def test_an_option_value_after_the_equals_bash_splits_off_completes(self):
        self.assertEqual(self.complete("holo file T.md --priority =", 4),
                         list(FILE_TICKET_PRIORITIES))

    def test_after_ticket_the_ticket_aliases_and_no_top_level_only_command(self):
        replies = self.complete('holo ticket ""', 2)
        self.assertEqual(sorted(replies), sorted(TICKET_VERBS))
        top_only = ({command.words[0] for command in COMMANDS + READS}
                    - set(TICKET_VERBS))
        self.assertTrue({"status", "hold", "story", "send-back"} <= top_only)
        self.assertEqual(set(replies) & top_only, set())


class TicketKeyTests(CompletionCase):
    """A registered native-board project `alpha` with HOLO-1 and HOLO-2 open
    and HOLO-3 canceled."""

    def setUp(self):
        super().setUp()
        self.alpha = self.register("alpha", "HOLO")
        self.conn = store.open(str(Project.locate(self.alpha).store_path))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "native:HOLO",
                                                       self.alpha)
        for title in ("One", "Two", "Three"):
            self.file(title)
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE linearIdentifier = 'HOLO-3'"
        ).fetchone()
        store.board.cancel_ticket(self.conn, self.project_id, "HOLO-3", revision,
                                  "not needed")

    def file(self, title):
        return store.board.file_ticket(self.conn, self.project_id, "HOLO",
                                       ticket_body(title))

    def test_a_key_position_offers_the_open_tickets_keys(self):
        self.assertEqual(self.offered("requeue", "-p", "alpha", "HO"),
                         ["HOLO-1", "HOLO-2"])
        split_by_bash = ("file", "T.md", "--project", "=", "alpha",
                         "--update", "=", "HO")
        self.assertEqual(self.offered(*split_by_bash), ["HOLO-1", "HOLO-2"])
        for project in ("-palpha", "-p=alpha"):
            self.assertEqual(self.offered("requeue", project, "HO"),
                             ["HOLO-1", "HOLO-2"])
        self.assertEqual(self.offered("file", "T.md", "-p", "alpha", "--update=HO"),
                         ["--update=HOLO-1", "--update=HOLO-2"])

    def test_the_keys_are_read_again_only_once_the_cache_is_a_minute_old(self):
        self.assertEqual(self.offered("requeue", "-p", "alpha", ""),
                         ["HOLO-1", "HOLO-2"])
        self.assertEqual(self.file("Four"), "HOLO-4")
        self.assertEqual(self.offered("requeue", "-p", "alpha", ""),
                         ["HOLO-1", "HOLO-2"])
        cache = cache_file(str(self.alpha))
        aged = time.time() - 61
        os.utime(cache, (aged, aged))
        self.assertEqual(self.offered("requeue", "-p", "alpha", ""),
                         ["HOLO-1", "HOLO-2", "HOLO-4"])

    def test_no_project_or_a_failed_board_read_offers_no_keys_silently(self):
        broken = self.register("beta", "BETA")
        Project.locate(broken).store_path.write_text("not a database\n")
        for words in (("requeue", "HO"), ("requeue", "-p", "nosuch", "HO"),
                      ("requeue", "-p", "beta", "")):
            with self.subTest(words=words):
                self.assertEqual(self.offered(*words), [])


class TableTests(CompletionCase):
    def test_every_command_and_alias_first_word_is_offered_and_the_helper_is_not(self):
        offered = self.offered("")
        first = ({command.words[0] for command in COMMANDS + READS}
                 | {alias[0] for alias, _ in ALIASES} | {"completion"})
        self.assertEqual(first - set(offered), set())
        self.assertNotIn(HELPER, offered)
        self.assertNotIn(HELPER, self.holo("--help").stdout)

    def test_a_read_named_by_its_group_word_completes_its_flags(self):
        self.assertEqual(self.offered("board", "--j"), ["--json"])
        self.assertEqual(self.offered("ticket", "HOLO-1", "--j"), ["--json"])
        self.assertEqual(self.offered("run", "5", "--l"), ["--ledger"])

    def test_a_positional_or_option_with_choices_offers_them(self):
        cases = {("move", "HOLO-1", ""): ["ready", "backlog"],
                 ("gap", "HOLO-1", ""): [layer.value for layer in GapLayer],
                 ("file", "T.md", "--priority", ""): list(FILE_TICKET_PRIORITIES),
                 ("file", "T.md", "--priority=h"): ["--priority=high"]}
        for words, expected in cases.items():
            with self.subTest(words=words):
                self.assertEqual(self.offered(*words), expected)


class ScriptTests(CompletionCase):
    def test_each_script_calls_the_helper_and_parses_where_its_shell_is(self):
        for shell in ("bash", "zsh", "fish"):
            with self.subTest(shell=shell):
                result = self.holo("completion", shell)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"holo {HELPER} ", result.stdout)
                if shutil.which(shell) is None:
                    continue
                script = self.root / f"holo.{shell}"
                script.write_text(result.stdout)
                parsed = subprocess.run([shell, "-n", str(script)],
                                        capture_output=True, text=True)
                self.assertEqual(parsed.returncode, 0, parsed.stderr)


if __name__ == "__main__":
    unittest.main()

"""Per-target config: `~/.holophyte/SLUG/config.toml`, `[agents]`, `[worktree]`.
Run: python3 -m unittest discover -s tests -p 'test_factory_config*' -v
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

import holophyte.agents
import holophyte.claim
import holophyte.cli
import holophyte.config
import holophyte.gates
import holophyte.loop
import holophyte.operator
import holophyte.project
import holophyte.redact
import holophyte.runs
import holophyte.supervisor
import holophyte.supervisor_lock
import review_runner
import store

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# `waiting` is a helper, not a test module: discovery never imports it, and
# how this file is imported decides whether `tests/` is on the path at all.
sys.path.insert(0, str(HERE))
# The thin entry point, by path: `test_importing_the_module_names_no_target`
# executes it fresh to show that importing `factory` chooses no target.
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
from bot_thread_fixture import BotConfigCases  # noqa: E402
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from fix_session_fixture import FixSessionConfigCases  # noqa: E402
from procs import KillWatch, assert_no_escaped_child  # noqa: E402
from waiting import wait_for  # noqa: E402 - after the sys.path insert above


class ConfigLoadingTests(FixSessionConfigCases, BotConfigCases, ConfigTestCase):
    def test_implementer_session_validation_at_startup(self):
        for value in ('"["', '"no group"', '"(one)(two)"', '42', '[]'):
            with self.subTest(value=value):
                self.locate(f"[agents]\nimplementer_session = {value}\n")
                with self.assertRaisesRegex(SystemExit, "implementer_session"):
                    holophyte.config.check_document(self.project)
        self.locate("[agents]\nimplementer_session = 'session id: ([0-9a-f-]{36})'\n")
        holophyte.config.check_document(self.project)

    def test_harness_table_refusals_name_the_key(self):
        table = '[agents.implementer]\nharness = "claude"\n'
        for config, message in (
            ('[agents.implementer]\nharness = "opencode"\n',
             r"\[agents\.implementer\] harness"),
            (table + 'sandbox = "none"\n', r"\[agents\.implementer\] sandbox"),
            ('[agents.reviewer]\nharness = "claude"\n',
             r"\[agents\.reviewer\] harness: 'claude' supports implementer,"),
            (table + '[harnesses]\nclaude = "bin/claude"\n', r"\[harnesses\] claude"),
            ("[agents]\nimplementer_session = 'id: (.+)'\n" + table,
             r"\[agents\] implementer_session"),
            ('[agents.reviewer]\nharness = "codex"\neffort = "max"\n',
             r"\[agents\.reviewer\] effort"),
            ('[agents]\nreview_model = "m"\n[agents.reviewer]\nharness = "codex"\n',
             r"\[agents\] review_model"),
            ('[agents.reviewer]\nharness = "cursor"\n',
             r"\[agents\.reviewer\] model is required"),
            ('[agents.adjudicator]\nharness = "cursor"\nmodel = "m"\n'
             'effort = "high"\n', r"\[agents\.adjudicator\] effort: harness"),
            ('[agents.reviewer]\nharness = "devin"\n',
             r"\[agents\.reviewer\] model is required"),
            ('[agents.adjudicator]\nharness = "devin"\nmodel = "opus"\n'
             'effort = "high"\n', r"\[agents\.adjudicator\] effort: harness 'devin'"),
            ('[agents.implementer]\nharness = "devin"\n',
             r"\[agents\.implementer\] model is required"),
            ('[agents.implementer]\nharness = "devin"\nmodel = "opus"\n'
             'effort = "high"\n', r"\[agents\.implementer\] effort: harness 'devin'"),
        ):
            with self.subTest(config=config):
                self.locate(config)
                with self.assertRaisesRegex(SystemExit, message):
                    holophyte.config.check_document(self.project)
        self.locate(table + '[harnesses]\nclaude = "/opt/claude/bin/claude"\n')
        holophyte.config.check_document(self.project)
        self.locate('[agents.reviewer]\nharness = "devin"\nmodel = "opus"\n')
        holophyte.config.check_document(self.project)
        self.locate('[agents.implementer]\nharness = "devin"\nmodel = "opus"\n')
        holophyte.config.check_document(self.project)

    def test_codex_implementer_table_defaults_and_refuses_an_unknown_effort(self):
        table = '[agents.implementer]\nharness = "codex"\n'
        self.locate(table)
        holophyte.config.check_document(self.project)
        self.locate(table + 'effort = "max"\n')
        with self.assertRaisesRegex(SystemExit, r"\[agents\.implementer\] effort"):
            holophyte.config.check_document(self.project)

    def test_worktree_environment_refusals(self):
        self.locate("")
        source = self.project.config_path.parent / "source.env"
        source.write_text("PUBLIC=sentinel-config-value\n")
        cases = [
            (f'env_source = "{source}"', "env_allow"),
            ('env_allow = ["PUBLIC"]', "env_source"),
            (f'env_source = "{source}"\nenv_allow = ["MISSING"]', "MISSING"),
            (f'env_source = "{source}"\nenv_allow = ["bad-name"]', "env_allow"),
        ]
        for config, message in cases:
            with self.subTest(config=config):
                target = type("Candidate", (), {
                    "config_path": self.project.config_path,
                    "path": self.project.path,
                    "config": lambda self: {"worktree": tomllib.loads(config)},
                })()
                with self.assertRaises(SystemExit) as caught:
                    holophyte.config.check_document(target)
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("sentinel-config-value", str(caught.exception))

    def test_capture_environment_refusals_and_redaction(self):
        self.enterContext(patch("holophyte.redact._environment_values", frozenset()))
        self.locate("")
        source = self.project.config_path.parent / "capture.env"
        source.write_text("CAPTURE_KEY=sentinel-capture\n")
        cases = [
            (f'capture_env_source = "{source}"', "capture_env_allow"),
            ('capture_env_allow = ["CAPTURE_KEY"]', "capture_env_source"),
            (f'capture_env_source = "{source}"\ncapture_env_allow = ["MISSING"]',
             "MISSING"),
        ]
        for config, message in cases:
            with self.subTest(config=config):
                target = type("Candidate", (), {
                    "config_path": self.project.config_path,
                    "path": self.project.path,
                    "config": lambda self: {"merge": tomllib.loads(config)},
                })()
                with self.assertRaises(SystemExit) as caught:
                    holophyte.config.check_document(target)
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("sentinel-capture", str(caught.exception))
        self.locate(f'[merge]\ncapture_env_source = "{source}"\n'
                    'capture_env_allow = ["CAPTURE_KEY"]\n')
        holophyte.config.check_document(self.project)
        with patch("builtins.print") as printed:
            holophyte.redact.safe_print("token sentinel-capture here")
        printed.assert_called_once_with("token [redacted] here")

    def test_strip_attribution_rejects_invalid_patterns_at_startup(self):
        for value in ('["["]', '"not a list"', '[1]'):
            with self.subTest(value=value):
                self.locate(f'[merge]\nstrip_attribution = {value}\n')
                with self.assertRaisesRegex(SystemExit, "strip_attribution"):
                    holophyte.config.check_document(self.project)

    def test_merge_changes_log_default_override_and_validation(self):
        for value, expected in ((None, False), ("false", False), ("true", True)):
            self.locate("" if value is None else f"[merge]\npr_changes_log = {value}\n")
            holophyte.config.check_document(self.project)
            self.assertIs(holophyte.config.merge_config(self.project).pr_changes_log,
                          expected)
        for value in ('"true"', "1", "0", "1.5", "[]", "{}"):
            with self.subTest(value=value):
                self.locate(f"[merge]\npr_changes_log = {value}\n")
                with self.assertRaisesRegex(SystemExit, "pr_changes_log.*boolean"):
                    holophyte.config.check_document(self.project)

    def test_merge_mention_handle(self):
        self.locate("")
        self.assertEqual(holophyte.config.merge_config(self.project).mention_handle,
                         "holophyte")
        self.locate('[merge]\nmention_handle = "factory-bot"\n')
        self.assertEqual(holophyte.config.merge_config(self.project).mention_handle,
                         "factory-bot")

    def test_merge_check_wait_default_override_and_validation(self):
        for value, expected in ((None, 1800), ("3600", 3600)):
            self.locate("" if value is None else f"[merge]\ncheck_wait_sec = {value}\n")
            holophyte.config.check_document(self.project)
            self.assertEqual(holophyte.config.merge_config(self.project).check_wait_sec,
                             expected)
        for value in ("0", "-5", "true", "1.5", '"60"'):
            with self.subTest(value=value), \
                    self.assertRaisesRegex(SystemExit, "check_wait_sec"):
                self.locate(f"[merge]\ncheck_wait_sec = {value}\n")
                holophyte.config.check_document(self.project)

    def test_an_absent_config_file_loads_as_empty(self):
        target = self.locate().path
        self.assertEqual(self.project.config_path,
                         holophyte.project.state_dir(target) / "config.toml")
        self.assertFalse(self.project.config_path.exists())
        self.assertEqual(self.project.config(), {})

    def test_malformed_toml_aborts_naming_the_file_and_the_problem(self):
        self.locate('[agents]\nimplementer = "unterminated\n')

        with self.assertRaises(SystemExit) as raised:
            self.project.config()

        message = str(raised.exception)
        self.assertIn(str(self.project.config_path), message)
        # The parser's own complaint, not just "could not read config": the
        # operator has to be told which line to go fix.
        self.assertIn("line 2", message)

    def test_the_config_is_read_for_the_target_the_command_line_names(self):
        target = self.locate().path
        self.write_config("[agents\n")

        with self.assertRaises(SystemExit) as raised:
            holophyte.cli.cli([str(target), "--report"])

        self.assertIn(str(self.project.config_path), str(raised.exception))

    def test_two_targets_in_one_process_each_read_their_own_config(self):
        self.locate()
        targets = []
        for name in ("one", "two"):
            path = self.root / name / "repo"
            path.mkdir(parents=True)
            target = holophyte.project.Project.locate(path)
            target.config_path.parent.mkdir(parents=True)
            target.config_path.write_text(
                f'[agents]\nimplementer = "harness-{name} run"\n')
            targets.append(target)
        first, second = targets

        self.assertEqual(first.config()["agents"]["implementer"],
                         "harness-one run")
        self.assertEqual(second.config()["agents"]["implementer"],
                         "harness-two run")
        self.assertEqual(first.config()["agents"]["implementer"],
                         "harness-one run")
        self.assertEqual(holophyte.config.agent_command(first, "implement", "go"),
                         ["harness-one", "run", "go"])
        self.assertEqual(holophyte.config.agent_command(second, "implement", "go"),
                         ["harness-two", "run", "go"])

    def test_importing_the_module_names_no_target(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)}):
            mod = importlib.util.module_from_spec(SPEC)
            SPEC.loader.exec_module(mod)

        for name in ("TARGET", "HOLO_DIR", "STORE_PATH", "WORKTREES",
                     "CONFIG_PATH", "CONFIG", "retarget", "config"):
            self.assertFalse(hasattr(mod, name), name)
        self.assertEqual(sorted(home.iterdir()), [])

    def test_help_does_not_read_any_config_or_touch_the_home(self):
        # `--help` exits before a target is worked with at all, so a malformed
        # config for the default target cannot break it -- nor can it break
        # importing this module, which every test here already relies on.
        # Nor is a target located or its legacy state adopted: the home it
        # ran under is as empty afterwards as before.
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)}), \
                patch.object(holophyte.project, "load_config",
                             side_effect=AssertionError("config read")) as load, \
                patch.object(holophyte.project.Project, "locate",
                             autospec=True) as locate, \
                patch.object(holophyte.project, "adopt_legacy_state",
                             autospec=True) as adopt:
            with contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli(["--help"])

        self.assertEqual(raised.exception.code, 0)
        load.assert_not_called()
        locate.assert_not_called()
        adopt.assert_not_called()
        self.assertEqual(sorted(home.iterdir()), [])

    def test_a_missing_project_is_a_usage_error_that_touches_nothing(self):
        # No default project: a bare `factory.py` used to name one operator's
        # checkout, a path that exists on one machine. Now it is an argparse
        # error -- usage on stderr, a non-zero exit -- and, like `--help`, it
        # is answered before a project is located or the home is touched.
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        stderr = io.StringIO()
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)}), \
                patch.object(holophyte.project.Project, "locate",
                             autospec=True) as locate, \
                patch.object(holophyte.project, "adopt_legacy_state",
                             autospec=True) as adopt:
            with contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([])

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("usage:", stderr.getvalue())
        self.assertRegex(stderr.getvalue(), r"required: project\b")
        self.assertNotRegex(stderr.getvalue(), r"(?i)\btarget\b")
        locate.assert_not_called()
        adopt.assert_not_called()
        self.assertEqual(sorted(home.iterdir()), [])

    def test_unknown_tables_are_left_alone(self):
        # A config written against a later version still loads, and the table
        # this version does read keeps working beside the ones it does not.
        target = self.locate('[notifier]\nchannel = "#factory"\n\n'
                               '[agents]\nimplementer = "harness run"\n').path

        self.assertEqual(self.project.config()["notifier"], {"channel": "#factory"})
        command = holophyte.config.agent_command(self.project, "implement", "do it")
        self.assertEqual(command, ["harness", "run", "do it"])
        # And startup tolerates the table: a report against this config runs.
        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])
        report.assert_called_once_with(self.project)


class KnownKeyTests(ConfigTestCase):
    """A key the factory does not read inside a table it does is a typo.

    The case these exist for is `[worktree] setup_timeout_min = 10`: a key
    that does not exist, which the factory used to ignore while the operator
    believed a timeout was in force. Startup names the file, the table, the
    key and what the table does accept, for every mode, before anything is
    claimed.
    """

    def test_an_unknown_key_in_a_known_table_is_a_startup_error(self):
        target = self.locate('[worktree]\nsetup_timeout_min = 10\n').path

        with patch.object(holophyte.cli, "report") as report:
            with self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([str(target), "--report"])

        message = str(raised.exception)
        self.assertIn(str(self.project.config_path), message)
        self.assertIn("[worktree]", message)
        self.assertIn("setup_timeout_min", message)
        # The accepted keys, so the operator can see the one they meant.
        self.assertIn("setup_timeout_sec", message)
        self.assertIn("setup", message)
        report.assert_not_called()

    def test_every_known_table_is_checked(self):
        for config in ('[agents]\nimplementor = "harness run"\n',
                       '[supervisor]\nstale_heartbeat_min = 7\n',
                       '[loop]\nstop_on_failures = false\n',
                       '[report]\nhots_label = "x"\n',
                       '[console]\nother = 1\n',
                       '[serve]\ntoken = "x"\n',
                       '[board]\nprojet_id = "x"\n'):
            with self.subTest(config=config):
                self.locate(config)

                with self.assertRaises(SystemExit) as raised:
                    holophyte.config.check_config_keys(self.project)

                self.assertIn(str(self.project.config_path), str(raised.exception))

    def test_a_config_of_only_known_keys_passes(self):
        target = self.locate(
            '[agents]\nimplementer = "harness run"\n'
            '[worktree]\nsetup = ["true"]\nsetup_timeout_sec = 30\n'
            '[supervisor]\nheartbeat_stale_min = 7\n'
            '[loop]\nstop_on_failure = false\n').path

        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])

        report.assert_called_once_with(self.project)


class StateDirectoryTests(ConfigTestCase):
    """Every per-target artifact lives under one `HOLOPHYTE_HOME/SLUG/`.

    `Project.locate()` derives the directory and the three paths in it
    together, so the tests go through it and look at what it derived.
    """

    def test_config_store_and_lock_share_the_target_directory(self):
        target = self.locate().path
        holo = self.project.holo_dir

        self.assertEqual(holo.parent, self.home)
        self.assertTrue(holo.name.startswith("repo-"), holo)
        self.assertEqual(self.project.config_path, holo / "config.toml")
        self.assertEqual(self.project.store_path, holo / "store.db")
        self.assertEqual(holophyte.supervisor_lock.supervisor_lock_path(self.project),
                         holo / "supervisor.lock")
        # The worktree directory is heavy git state, not factory state, and
        # keeps its own sibling address.
        self.assertEqual(self.project.worktrees, target.parent / "repo.worktrees")

    def test_two_targets_with_one_basename_get_two_state_directories(self):
        # The whole reason the directory carries a hash: `/a/repo` and
        # `/b/repo` are different repositories with different histories.
        self.locate()
        one = (self.root / "one" / "repo")
        two = (self.root / "two" / "repo")
        one.parent.mkdir()
        two.parent.mkdir()

        first = holophyte.project.Project.locate(one).holo_dir
        second = holophyte.project.Project.locate(two).holo_dir

        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, second.parent)

    def test_the_directory_is_created_on_first_need_and_nothing_else_is(self):
        target = self.locate().path
        holo = self.project.holo_dir
        self.assertFalse(holo.exists())

        conn = holophyte.runs.open_store(self.project)
        self.addCleanup(conn.close)
        lock = holophyte.supervisor_lock.acquire_supervisor_lock(
            holophyte.supervisor_lock.supervisor_lock_path(self.project),
            self.project.path)
        self.addCleanup(holophyte.supervisor_lock.release_supervisor_lock, lock)

        self.assertTrue((holo / "store.db").exists())
        self.assertTrue((holo / "supervisor.lock").exists())
        # Nothing dotted is left beside the target any more.
        beside = sorted(p.name for p in target.parent.iterdir())
        self.assertEqual(beside, ["home", "repo"])

    def test_a_target_with_no_store_gets_no_directory_either(self):
        self.locate()
        out = io.StringIO()

        holophyte.operator.report(self.project, out=out)

        self.assertIn("no store at", out.getvalue())
        self.assertFalse(self.project.holo_dir.exists())


class LegacyAdoptionTests(ConfigTestCase):
    """The one-time move of pre-`~/.holophyte` state into the new directory.

    KO-165 changed the address without moving what was at the old one, and a
    run against the new empty store shadowed fifteen runs and the target's
    agent routes. These go through `Project.locate()` for that reason:
    adoption that is not wired into the path a run takes is adoption that
    never runs.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.set_home(self.home)
        self.target = self.root / "repo"
        self.target.mkdir()

    def locate(self, config=None):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.project = holophyte.project.Project.locate(self.target)
        self.printed = printed.getvalue()
        return self.project

    def legacy_directory(self):
        """KO-165's layout: `<target>.holophyte/` holding the state files."""
        holo = self.root / "repo.holophyte"
        holo.mkdir()
        (holo / "store.db").write_bytes(b"legacy store\n")
        (holo / "config.toml").write_text('[agents]\nimplementer = "harness run"\n')
        return holo

    def legacy_siblings(self):
        """The older layout: dotted files beside the target, db with sidecars."""
        db = self.root / "repo.holophyte.db"
        db.write_bytes(b"legacy store\n")
        (self.root / "repo.holophyte.db-wal").write_bytes(b"wal\n")
        (self.root / "repo.holophyte.db-shm").write_bytes(b"shm\n")
        (self.root / "repo.holophyte.toml").write_text(
            '[agents]\nimplementer = "harness run"\n')
        return db

    def test_the_ko165_directory_is_adopted_whole(self):
        holo = self.legacy_directory()

        self.locate()

        self.assertEqual(self.project.store_path.read_bytes(), b"legacy store\n")
        self.assertEqual(self.project.config()["agents"]["implementer"], "harness run")
        self.assertFalse(holo.exists())
        self.assertIn(str(holo / "store.db"), self.printed)
        self.assertIn(str(self.project.store_path), self.printed)

    def test_the_dotted_siblings_are_adopted_with_their_sidecars(self):
        db = self.legacy_siblings()

        self.locate()

        self.assertEqual(self.project.store_path.read_bytes(), b"legacy store\n")
        self.assertEqual(
            self.project.holo_dir.joinpath("store.db-wal").read_bytes(), b"wal\n")
        self.assertEqual(
            self.project.holo_dir.joinpath("store.db-shm").read_bytes(), b"shm\n")
        self.assertEqual(self.project.config()["agents"]["implementer"], "harness run")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["home", "repo"])
        self.assertIn(str(db), self.printed)

    def test_two_stores_are_refused_rather_than_one_shadowing_the_other(self):
        holo = self.legacy_directory()
        new = holophyte.project.state_dir(self.target)
        new.mkdir(parents=True)
        (new / "store.db").write_bytes(b"new store\n")

        with self.assertRaises(SystemExit) as raised:
            self.locate()

        message = str(raised.exception)
        self.assertIn(str(holo / "store.db"), message)
        self.assertIn(str(new / "store.db"), message)
        # Neither store is touched: an operator decides which history wins.
        self.assertEqual((holo / "store.db").read_bytes(), b"legacy store\n")
        self.assertEqual((new / "store.db").read_bytes(), b"new store\n")

    def test_a_state_directory_without_a_store_still_adopts_the_legacy_one(self):
        """An empty-ish state directory is not proof the move already ran.

        The README tells an operator to write `config.toml` at the new
        address, and anything else that creates the directory first would do
        the same: gating on the directory rather than on the store is how a
        legacy history gets silently shadowed by the store `open_store()`
        creates a moment later -- exactly the KO-165 failure.
        """
        holo = self.legacy_directory()
        (holo / "config.toml").unlink()
        new = holophyte.project.state_dir(self.target)
        new.mkdir(parents=True)
        (new / "config.toml").write_text("[agents]\n")

        self.locate()

        self.assertEqual(self.project.store_path.read_bytes(), b"legacy store\n")
        self.assertFalse(holo.exists())
        self.assertIn(str(self.project.store_path), self.printed)

    def test_a_file_already_at_the_new_address_is_refused_not_overwritten(self):
        holo = self.legacy_directory()
        new = holophyte.project.state_dir(self.target)
        new.mkdir(parents=True)
        (new / "config.toml").write_text("[agents]\nimplementer = \"new\"\n")

        with self.assertRaises(SystemExit) as raised:
            self.locate()

        message = str(raised.exception)
        self.assertIn(str(holo / "config.toml"), message)
        self.assertIn(str(new / "config.toml"), message)
        # Nothing moved: the operator's file and the legacy one both stand.
        self.assertEqual((new / "config.toml").read_text(),
                         '[agents]\nimplementer = "new"\n')
        self.assertTrue((holo / "store.db").exists())
        self.assertTrue((holo / "config.toml").exists())

    def test_deriving_paths_without_adopting_leaves_the_target_alone(self):
        """`Project.locate(..., adopt=False)` derives the paths and nothing else.

        Adoption is a side effect the caller asks for: a value built for a
        target nobody is about to run against -- a daemon enumerating a
        host's targets, a test naming a directory -- must not move that
        target's state, and where the target has two stores must not exit.
        `cli()` asks; nothing else does.
        """
        holo = self.legacy_directory()
        new = holophyte.project.state_dir(self.target)
        new.mkdir(parents=True)
        (new / "store.db").write_bytes(b"new store\n")

        target = holophyte.project.Project.locate(self.target, adopt=False)

        self.assertEqual((holo / "store.db").read_bytes(), b"legacy store\n")
        self.assertEqual(target.store_path.read_bytes(), b"new store\n")

    def test_adoption_happens_once_and_a_later_run_leaves_the_target_alone(self):
        self.legacy_directory()
        self.locate()

        # A second target's worth of legacy state appearing later must not be
        # swept in on top of a state directory that is already the real one.
        (self.root / "repo.holophyte.toml").write_text("[agents]\n")
        self.locate()

        self.assertEqual(self.project.store_path.read_bytes(), b"legacy store\n")
        self.assertTrue((self.root / "repo.holophyte.toml").exists())
        self.assertEqual(self.printed, "")


class AgentCommandTests(ConfigTestCase):
    WORKTREE = Path("/tmp/holophyte-config-contract")

    def test_an_absent_config_leaves_todays_routes_byte_identical(self):
        self.locate()

        with patch.object(holophyte.agents, "run_capped") as run:
            run.return_value = (0, "implemented")
            holophyte.agents.agent(self.project, "implement", "make the change",
                                   self.WORKTREE)
        with patch.object(review_runner, "run_review") as run_review:
            run_review.return_value = "VERDICT: APPROVE"
            holophyte.agents.agent(self.project, "review", "review it", self.WORKTREE,
                          base_sha="1" * 40, candidate_sha="2" * 40)

        self.assertIsNone(holophyte.config.agent_command(
            self.project, "implement", "make the change"))
        run.assert_called_once_with(
            ["claude", "-p", "make the change",
             "--model", "opus", "--effort", "high"],
            self.WORKTREE, 1800,
        )
        # The reviewer still goes through the hardened container, not argv.
        self.assertEqual(run_review.call_args.kwargs["profile"], "codex-sol-medium")

    def test_an_implementer_override_replaces_the_argv(self):
        self.locate('[agents]\n'
                      'implementer = "claude --model sonnet --effort medium -p"\n')

        with patch.object(holophyte.agents, "run_capped") as run:
            run.return_value = (0, "implemented")
            result = holophyte.agents.agent(self.project, "implement",
                                            "make the change", self.WORKTREE)

        self.assertEqual(result, "implemented")
        # The goal lands as the command's last argument — one argv element, so
        # a task title full of quotes cannot rewrite the command.
        run.assert_called_once_with(
            ["claude", "--model", "sonnet", "--effort", "medium", "-p",
             "make the change"],
            self.WORKTREE, 1800,
        )

    def test_a_reviewer_override_replaces_the_container_route(self):
        self.locate('[agents]\nreviewer = "my-reviewer --diff"\n')

        with patch.object(review_runner, "run_review") as run_review, \
                patch.object(holophyte.agents, "publish_review_refs") as publish, \
                patch.object(holophyte.agents, "check_review_refs"), \
                patch.object(holophyte.agents, "review_scratch",
                             return_value=contextlib.nullcontext(Path("/scratch"))), \
                patch.object(holophyte.agents, "run_capped") as run:
            run.return_value = (0, "VERDICT: APPROVE")
            result = holophyte.agents.agent(self.project, "review", "review it",
                                            self.WORKTREE,
                                   base_sha="1" * 40, candidate_sha="2" * 40)

        self.assertEqual(result, "VERDICT: APPROVE")
        run_review.assert_not_called()
        publish.assert_called_once_with(self.WORKTREE, "1" * 40, "2" * 40,
                                        run_id=None)
        run.assert_called_once_with(
            ["my-reviewer", "--diff", "review it"],
            self.WORKTREE, 1800, on_start=ANY,
            env=dict(os.environ, HOLOPHYTE_REVIEW_CANDIDATE="refs/review/candidate",
                     HOLOPHYTE_REVIEW_SCRATCH="/scratch"),
        )
        # Overriding the reviewer leaves the adjudicator on its default route.
        self.assertIsNone(
            holophyte.config.agent_command(self.project, "adjudicate", "adjudicate it"))

    def test_the_round_records_the_route_that_actually_ran_it(self):
        self.locate('[agents]\nreviewer = "my-reviewer --diff"\n')

        with patch.object(store, "record_review_round") as record:
            holophyte.runs.record_round(self.project, object(), "run-1", 1, "review",
                                 "VERDICT: APPROVE", "echo ok", True, "")
            holophyte.runs.record_round(self.project, object(), "run-1", 2,
                                        "adjudicate",
                                 "VERDICT: PASS", "echo ok", True, "")

        # The override ran the review round, so the row names it; the
        # adjudicator went through the default container and says so.
        self.assertEqual(record.call_args_list[0].args[4], "my-reviewer")
        self.assertEqual(record.call_args_list[1].args[4], "codex-sol-medium")

    def test_an_unusable_command_is_an_error_not_a_silent_default(self):
        for config, expected in (
            ('[agents]\nimplementer = ""\n', "is empty"),
            ('[agents]\nimplementer = "   "\n', "is empty"),
            ('[agents]\nimplementer = ["claude", "-p"]\n', "command string"),
        ):
            with self.subTest(config=config):
                self.locate(config)

                with self.assertRaises(SystemExit) as raised:
                    holophyte.config.agent_command(self.project, "implement",
                                                   "make the change")

                self.assertIn(str(self.project.config_path), str(raised.exception))
                self.assertIn(expected, str(raised.exception))


class BudgetScaleTests(ConfigTestCase):
    """`[agents] budget_scale`: the implementer turn's wall-clock
    multiplier -- 1.0 when absent, a number from 1.0 to 3.0 when set."""

    def test_an_absent_key_is_the_unscaled_budget(self):
        self.locate()

        self.assertEqual(holophyte.config.budget_scale(self.project), 1.0)

    def test_a_scale_inside_the_range_is_read(self):
        for line in ("budget_scale = 1.5", "budget_scale = 2",
                     "budget_scale = 1", "budget_scale = 3"):
            with self.subTest(line=line):
                self.locate(f"[agents]\n{line}\n")

                self.assertEqual(holophyte.config.budget_scale(self.project),
                                 float(line.split("= ")[1]))

    def test_a_scale_outside_the_range_is_a_startup_error(self):
        """0.5 shrinks a budget the cap exists to stop; 4 stops being a
        cap. Each is refused at startup, for every mode, naming the key and
        the range -- before anything is claimed."""
        for line in ("budget_scale = 0.5", "budget_scale = 4",
                     "budget_scale = true", 'budget_scale = "two"',
                     "budget_scale = 0.999", "budget_scale = 3.5"):
            with self.subTest(line=line):
                target = self.locate(f"[agents]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.project.config_path), message)
                self.assertIn("[agents]", message)
                self.assertIn("budget_scale", message)
                self.assertIn("1.0", message)
                self.assertIn("3.0", message)
                report.assert_not_called()

    def test_the_key_is_a_known_agents_key(self):
        """A set `budget_scale` is not the unknown-key typo refusal: the
        config loads and the value is what the reader hands back."""
        target = self.locate("[agents]\nbudget_scale = 2\n").path

        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])

        report.assert_called_once_with(self.project)

    def test_the_scale_stretches_the_hard_cap_the_turn_is_held_under(self):
        """`IMPL_TIMEOUT` becomes the scaled thirty minutes: a caller that
        names no timeout gets the ceiling, and one that does is held under
        the scaled one."""
        self.locate("[agents]\nbudget_scale = 1.5\n")

        worktree = Path("/tmp/holophyte-scale")
        with patch.object(holophyte.agents, "run_capped") as run:
            run.return_value = (0, "implemented")
            holophyte.agents.agent(self.project, "implement", "make the change",
                                   worktree, timeout=60 * 60)

        self.assertEqual(run.call_args.args[2], 45 * 60)


class WorktreeSetupTests(ConfigTestCase):
    """`[worktree] setup`: the commands a fresh task worktree is prepared with.

    The wart these exist for is a worktree that borrows the main checkout's
    environment -- a venv, a module cache, a generated file -- and so tests the
    branch against somebody else's dependencies. The commands run in the
    worktree, in order, through the same verify-gate machinery a ticket's
    verify command runs through, before any agent turn is dispatched.
    """

    def test_crlf_environment_preserves_values_without_carriage_returns(self):
        self.assertEqual(holophyte.config.parse_environment(
            'PUBLIC=sentinel-crlf\r\nQUOTED="quoted value"\r\n'),
            {"PUBLIC": "sentinel-crlf", "QUOTED": '"quoted value"'})

    def worktree(self):
        """A throwaway directory standing in for a freshly cut task worktree."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_an_absent_worktree_table_runs_nothing(self):
        self.locate()

        # The gate's one subprocess call, resolved where `run_verify` lives:
        # `run_worktree_setup` reaches it through `run_verify`, so a
        # command that ran would trip this sentinel.
        with patch.object(holophyte.gates, "run_capped",
                          side_effect=AssertionError("ran a setup command")):
            self.assertEqual(
                holophyte.claim.run_worktree_setup(self.project, self.worktree()),
                             (True, ""))
        self.assertEqual(holophyte.config.setup_commands(self.project), [])

    def test_the_commands_run_in_the_worktree_in_the_order_written(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["pwd > where.txt", '
                      '"cp where.txt copied.txt"]\n')

        ok, report = holophyte.claim.run_worktree_setup(self.project, wt)

        self.assertTrue(ok)
        self.assertEqual(report, "")
        # `pwd` is the fresh worktree, not the main checkout the loop was
        # pointed at -- which is the whole point of the table.
        self.assertEqual((wt / "where.txt").read_text().strip(),
                         str(wt.resolve()))
        # The second command saw the first one's file, so they ran in order.
        self.assertTrue((wt / "copied.txt").exists())

    def test_a_failing_command_stops_the_setup_and_names_itself(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["echo building; exit 3", '
                      '"touch never.txt"]\n')

        ok, report = holophyte.claim.run_worktree_setup(self.project, wt)

        self.assertFalse(ok)
        self.assertIn("command 1 of 2", report)
        self.assertIn("exit 3", report)
        self.assertIn("building", report)  # the output, not just the status
        # Step two assumed step one worked, so it never ran.
        self.assertFalse((wt / "never.txt").exists())

    def test_a_failure_is_reported_by_the_clause_that_failed(self):
        # The verify gate's fail-loud machinery, reused verbatim: a chain is
        # attributed clause by clause rather than as a bare non-zero exit.
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["echo first && false && echo third"]\n')

        ok, report = holophyte.claim.run_worktree_setup(self.project, wt)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3", report)
        self.assertIn("failing clause: false", report)
        self.assertIn("not executed: clause 3", report)

    def test_a_silent_failure_is_reported_as_silence(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["exit 1"]\n')

        ok, report = holophyte.claim.run_worktree_setup(self.project, wt)

        self.assertFalse(ok)
        self.assertIn("failed silently", report)

    def test_a_command_that_hits_the_cap_fails_instead_of_raising(self):
        # A hung setup is the case that most needs the caller's cleanup: it
        # must arrive as a `(False, report)` like any other failure, not as a
        # `TimeoutExpired` past the branch-and-worktree teardown.
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["make deps", "touch never.txt"]\n')
        expired = subprocess.TimeoutExpired("make deps", 300,
                                            output="resolving packages\n")

        # The cap fires inside `run_capped`, the gate's one subprocess call,
        # resolved in `holophyte.gates` where `run_verify` reads it.
        with patch.object(holophyte.gates, "run_capped", side_effect=expired):
            ok, report = holophyte.claim.run_worktree_setup(self.project, wt)

        self.assertFalse(ok)
        self.assertIn("command 1 of 2", report)
        self.assertIn("timed out after 300s", report)
        self.assertIn("make deps", report)
        self.assertIn("resolving packages", report)  # what it managed to say
        self.assertFalse((wt / "never.txt").exists())

    def test_the_cap_takes_the_command_s_children_down_with_it(self):
        """The escaped-child check has failed once under load (slice 4b
        implementer, first full-suite run on `phase2/split-gates`, while a
        Codex review ran a suite alongside; its twin in `test_verify_gate.py`
        failed once the same day, 2026-09-02) and passed on every rerun. Two
        hypotheses: the scheduler held the test between `communicate()`
        raising and `killpg` running long enough for the child to finish, or
        `/bin/sh` put the `&` job in a group of its own so the kill missed
        it. A failure now carries the kill latency, the watched `killpg` call
        and a `ps` snapshot of what mentions the marker, which tell those
        apart: read the message before widening anything.
        """
        # A real timeout, not a mocked one: the cap has to end the process
        # tree and not just the shell at the top of it. A background child
        # that outlives the reported timeout goes on writing into a worktree
        # the caller is about to delete, on a branch nobody keeps.
        #
        # Margins: a 0.3 s cap lost to the shell's own startup whenever
        # another suite or a review ran alongside -- the cap fired before
        # `echo` had run, and the "what it said before the cap" check failed
        # on green code. The cap is 1 s; the escaping child sleeps 3 s, well
        # past the cap, so only the kill can stop it; the wait for its marker
        # runs past that delay. `started` is the shell's own word that it
        # reached its first line before the kill -- the only case in which
        # the report can be expected to hold that line -- so a machine too
        # loaded to get there is named as such instead of as lost output.
        # Polled rather than looked at once: the writer is a process the test
        # never joined.
        wt = self.worktree()
        started = wt / "started.txt"
        escaped = wt / "escaped.txt"
        self.locate('[worktree]\nsetup = ["echo resolving; touch %s; '
                      '(sleep 3; touch %s) & sleep 30"]\n' % (started, escaped))

        with patch.object(holophyte.config, "VERIFY_TIMEOUT", 1.0), \
                KillWatch(escaped) as watch:
            began = time.monotonic()
            ok, report = holophyte.claim.run_worktree_setup(self.project, wt)
            elapsed = time.monotonic() - began

        self.assertFalse(ok)
        self.assertIn("timed out", report)
        self.assertTrue(wait_for(started.exists, 5.0),
                        "the shell did not reach its first line inside the 1s "
                        "cap: this machine is too loaded to time this run")
        self.assertIn("resolving", report)  # what it said before the cap
        assert_no_escaped_child(escaped, 3.5, watch=watch, elapsed=elapsed,
                                cap=1.0)

    def test_setup_timeout_sec_bounds_the_setup_commands(self):
        # A real timeout again, against the configured cap rather than the
        # module constant: a one-second cap and a command that sleeps longer
        # fails the run naming the timeout. The cap stays at 1 s because the
        # message assertion pins it, and nothing here depends on what the
        # shell printed before the cap, so there is no race to widen.
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["echo installing; sleep 30"]\n'
                      'setup_timeout_sec = 1\n')

        start = time.monotonic()
        ok, report = holophyte.claim.run_worktree_setup(self.project, wt)

        self.assertFalse(ok)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("timed out after 1s", report)
        self.assertIn("sleep 30", report)

    def test_the_default_setup_cap_is_the_verify_cap(self):
        self.locate('[worktree]\nsetup = ["make deps"]\n')

        self.assertEqual(holophyte.config.setup_timeout(self.project),
                         holophyte.config.VERIFY_TIMEOUT)

    def test_an_unusable_setup_timeout_is_a_startup_error(self):
        for value in ("0", "-5", "true", '"10"', "inf"):
            with self.subTest(value=value):
                target = self.locate(f'[worktree]\nsetup_timeout_sec = {value}\n').path

                # The default routes are this host's business, not the table's.
                with patch.object(holophyte.config, "check_default_implementer"), \
                        patch.object(holophyte.config, "check_default_reviewer"), \
                        patch.object(holophyte.cli, "main",
                                     side_effect=AssertionError("claimed work")):
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target)])

                message = str(raised.exception)
                self.assertIn(str(self.project.config_path), message)
                self.assertIn("setup_timeout_sec", message)
                self.assertIn("positive number", message)

    def test_a_carry_that_is_not_a_list_is_a_startup_error_naming_the_key(self):
        for value in ('"console/node_modules"', "3", '["console", 2]',
                      '["../elsewhere"]', '[""]'):
            with self.subTest(value=value):
                target = self.locate(f'[worktree]\ncarry = {value}\n').path

                with patch.object(holophyte.config, "check_default_implementer"), \
                        patch.object(holophyte.config, "check_default_reviewer"), \
                        patch.object(holophyte.cli, "main",
                                     side_effect=AssertionError("claimed work")):
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target)])

                message = str(raised.exception)
                self.assertIn(str(self.project.config_path), message)
                self.assertIn("[worktree] carry", message)

    def test_an_absent_carry_is_an_empty_list(self):
        self.locate('[worktree]\nsetup = ["make deps"]\n')

        self.assertEqual(holophyte.config.carry_directories(self.project), [])

        self.locate('[worktree]\ncarry = ["console/node_modules", ".venv"]\n')
        self.assertEqual(holophyte.config.carry_directories(self.project),
                         ["console/node_modules", ".venv"])

    def test_a_silent_timeout_is_reported_as_silence(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["make deps"]\n')

        with patch.object(holophyte.gates, "run_capped", side_effect=
                          subprocess.TimeoutExpired("make deps", 300)):
            ok, report = holophyte.claim.run_worktree_setup(self.project, wt)

        self.assertFalse(ok)
        self.assertIn("no output before the timeout", report)

    def test_the_setup_records_a_phase_before_it_runs_anything(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["true"]\n')
        conn = object()

        with patch.object(store, "set_phase") as set_phase, \
                patch("holophyte.stop.stop_if_requested"):
            holophyte.claim.run_worktree_setup(self.project, wt, conn, "run-1")

        set_phase.assert_called_once()
        self.assertEqual(set_phase.call_args.args[2], "working")
        self.assertIn("worktree setup", set_phase.call_args.args[3])

    def test_an_unusable_setup_table_is_an_error_not_a_skipped_step(self):
        for config, expected in (
            ('[worktree]\nsetup = "make deps"\n', "must be a list"),
            ('[worktree]\nsetup = [7]\n', "command string"),
            ('[worktree]\nsetup = ["make deps", ""]\n', "is empty"),
            ('[worktree]\nsetup = ["   "]\n', "is empty"),
        ):
            with self.subTest(config=config):
                self.locate(config)

                with self.assertRaises(SystemExit) as raised:
                    holophyte.config.setup_commands(self.project)

                self.assertIn(str(self.project.config_path), str(raised.exception))
                self.assertIn(expected, str(raised.exception))

    def test_the_startup_check_reuses_the_parse_a_run_would_use(self):
        self.locate('[worktree]\nsetup = [7]\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_worktree_setup(self.project)

        self.assertIn("command string", str(raised.exception))

    def test_a_table_that_is_not_a_table_is_a_startup_error_naming_it(self):
        """`worktree = "invalid"` and `agents = 3` used to reach the first
        reader as a string with no `.get()` -- a traceback, not a sentence;
        from the daemon's `PUT /config` (KO-356), a dropped connection."""
        for config, table, check in (
            ('worktree = "invalid"\n', "[worktree]",
             holophyte.config.check_worktree_setup),
            ("agents = 3\n", "[agents]", holophyte.config.review_route),
        ):
            with self.subTest(table=table):
                self.locate(config)

                with self.assertRaises(SystemExit) as raised:
                    check(self.project)

                message = str(raised.exception)
                self.assertIn(str(self.project.config_path), message)
                self.assertIn(f"{table} must be a table", message)

    def test_a_startup_check_of_a_usable_table_passes(self):
        # Startup settles the shape of the table and deliberately not the
        # commands: they are shell, written against a worktree that does not
        # exist yet.
        self.locate('[worktree]\nsetup = ["holophyte-no-such-tool --install"]\n')

        self.assertIsNone(holophyte.config.check_worktree_setup(self.project))

    def test_an_absent_table_checks_nothing(self):
        self.locate()

        self.assertIsNone(holophyte.config.check_worktree_setup(self.project))

    def test_a_run_checks_the_table_before_claiming_anything(self):
        target = self.locate('[worktree]\nsetup = "make deps"\n').path

        with patch.object(holophyte.config, "check_default_implementer"), \
                patch.object(holophyte.config, "check_default_reviewer"), \
                patch.object(holophyte.cli, "main",
                             side_effect=AssertionError("claimed work")) as main:
            with self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([str(target)])

        self.assertIn("must be a list", str(raised.exception))
        main.assert_not_called()

    def test_report_does_not_read_the_setup_table(self):
        # `--report` cuts no worktree, so a table it would never run is not
        # that reading's problem.
        target = self.locate('[worktree]\nsetup = [7]\n').path

        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])

        report.assert_called_once_with(self.project)

    def test_an_absent_branch_prefix_is_task(self):
        for config in (None, '[worktree]\nsetup = ["true"]\n'):
            with self.subTest(config=config):
                self.locate(config)
                self.assertEqual(holophyte.config.branch_prefix(self.project), "task")

    def test_a_named_branch_prefix_is_read_back(self):
        self.locate('[worktree]\nbranch_prefix = "factory"\n')
        self.assertEqual(holophyte.config.branch_prefix(self.project), "factory")

    def test_an_illegal_branch_prefix_is_a_startup_error_before_any_claim(self):
        """Empty, slashed, whitespace or git-refused characters: the run that
        discovered it at `git worktree add` would have claimed a ticket first."""
        for value in ('""', '"a/b"', '"a b"', '"a~b"', '"a:b"', '"a..b"',
                      '".hidden"', '"x.lock"', '"factory."', '"-factory"', '7'):
            with self.subTest(value=value):
                target = self.locate(f'[worktree]\nbranch_prefix = {value}\n').path

                with patch.object(holophyte.config, "check_default_implementer"), \
                        patch.object(holophyte.config, "check_default_reviewer"), \
                        patch.object(holophyte.cli, "main",
                                     side_effect=AssertionError("claimed work")):
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target)])

                message = str(raised.exception)
                self.assertIn(str(self.project.config_path), message)
                self.assertIn("[worktree] branch_prefix", message)
                self.assertNotIn("unknown key", message)


class ReviewRefTests(ConfigTestCase):
    """A configured reviewer reviews the same frozen pair as the default one.

    The default route gets `refs/review/base` and `refs/review/candidate` from
    the checkout `review_runner.stage_candidate()` builds. The configured route
    runs in the task worktree, and the prompt it is handed names those same two
    refs, so the worktree is where they have to appear.
    """

    def repo(self):
        """A throwaway repo with two commits: `self.base`, then `self.head`."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.git(root, "init", "-q", "-b", "main", ".")
        self.git(root, "config", "user.email", "test@example.com")
        self.git(root, "config", "user.name", "test")
        self.base = self.commit(root, "base.txt")
        self.head = self.commit(root, "candidate.txt")
        return root

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, check=True).stdout.strip()

    def commit(self, root, name):
        (root / name).write_text(name)
        self.git(root, "add", name)
        self.git(root, "commit", "-qm", name)
        return self.git(root, "rev-parse", "HEAD")

    def test_the_configured_reviewer_can_resolve_both_refs(self):
        root = self.repo()
        reviewer = root / "reviewer.sh"
        reviewer.write_text("#!/bin/sh\n"
                            "git rev-parse refs/review/base refs/review/candidate\n")
        reviewer.chmod(0o755)
        self.locate(f'[agents]\nreviewer = "{reviewer}"\n')

        reply = holophyte.agents.agent(self.project, "review", "review it", root,
                              base_sha=self.base, candidate_sha=self.head)

        # What the command printed is the pair the round is about, read out of
        # the repo it ran in -- not a ref that does not exist there.
        self.assertEqual(reply.split(), [self.base, self.head])

    def test_a_sha_the_repo_does_not_have_is_refused(self):
        root = self.repo()
        self.locate('[agents]\nreviewer = "true"\n')

        with self.assertRaises(review_runner.ReviewBoundaryError):
            holophyte.agents.agent(self.project, "review", "review it", root,
                          base_sha=self.base, candidate_sha="0" * 40)

        # Nothing was published: a refused round leaves no ref claiming a
        # candidate the repo never had.
        self.assertEqual(
            subprocess.run(["git", "rev-parse", "--verify", "-q",
                            "refs/review/candidate"], cwd=root).returncode, 1)

    def test_a_base_that_is_not_an_ancestor_is_refused(self):
        root = self.repo()
        self.locate('[agents]\nadjudicator = "true"\n')
        self.git(root, "checkout", "-q", "--orphan", "sideways")
        self.git(root, "rm", "-rqf", ".")
        unrelated = self.commit(root, "unrelated.txt")

        with self.assertRaises(review_runner.ReviewBoundaryError):
            holophyte.agents.agent(self.project, "adjudicate", "judge it", root,
                          base_sha=unrelated, candidate_sha=self.head)

    def test_the_default_route_is_left_to_stage_its_own_refs(self):
        # The container route builds its own checkout and names the refs
        # there; the task worktree is not where its reviewer looks.
        self.locate()
        root = self.repo()

        with patch.object(holophyte.agents, "publish_review_refs") as publish, \
                patch.object(review_runner, "run_review") as run_review:
            run_review.return_value = "VERDICT: APPROVE"
            holophyte.agents.agent(self.project, "review", "review it", root,
                          base_sha=self.base, candidate_sha=self.head)

        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()

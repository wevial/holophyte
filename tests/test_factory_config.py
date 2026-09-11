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
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

import holophyte.agents
import holophyte.cli
import holophyte.config
import holophyte.gates
import holophyte.loop
import holophyte.pr
import holophyte.runs
import holophyte.supervisor
import holophyte.target
import review_runner
import store
from provider import LinearProvider

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# `waiting` is a helper, not a test module: discovery never imports it, and
# how this file is imported decides whether `tests/` is on the path at all.
sys.path.insert(0, str(HERE))
# The thin entry point, by path: `test_importing_the_module_names_no_target`
# executes it fresh to show that importing `factory` chooses no target.
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
from procs import (  # noqa: E402 - after the sys.path insert above
    KillWatch,
    assert_no_escaped_child,
)
from waiting import wait_for  # noqa: E402 - after the sys.path insert above


class FakeChild:
    """What the stubbed `Popen` hands back: a pid and nothing else."""
    pid = 4242


class ConfigTestCase(unittest.TestCase):
    """Build a `Target` at a throwaway repository, optionally with a config.

    `Target.locate()` is the only thing that derives a target's paths, so the
    tests go through it rather than assembling a `Target` by hand: a test
    that set the config path itself would pass even if the file were never
    wired into the path `cli()` derives at all.
    """

    def locate(self, config=None):
        """The `Target` for a fresh repository under a fresh home, as `self.tgt`."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.set_home(self.home)
        self.target = self.root / "repo"
        self.target.mkdir()
        self.tgt = holophyte.target.Target.locate(self.target)
        if config is not None:
            self.write_config(config)
        self.stub_supervisor_spawn()
        return self.tgt

    def stub_supervisor_spawn(self):
        """Replace the `Popen` seam the loop path starts a supervisor through.

        Every test that reaches the loop path goes through here: the real
        one would leave a detached `--supervise` running against the
        throwaway home after the test. `self.popen` records the calls, so a
        test about the spawn reads what would have been started.
        """
        patcher = patch.object(holophyte.cli, "SPAWN", return_value=FakeChild())
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)

    def set_home(self, home):
        """Point HOLOPHYTE_HOME at a throwaway directory for this test.

        Every test in this file goes through here: state now lives under a
        home directory, and a test that let the real `~/.holophyte` stand
        would read and write the operator's own stores.
        """
        patcher = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, config):
        """Put `config` where `self.tgt` will look for it.

        Before anything has read it: a `Target` parses its config once, so a
        file written after the first read would be a file nobody reads.
        """
        self.tgt.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.tgt.config_path.write_text(config)


class ConfigLoadingTests(ConfigTestCase):
    def test_an_absent_config_file_loads_as_empty(self):
        target = self.locate().path

        self.assertEqual(self.tgt.config_path,
                         holophyte.target.state_dir(target) / "config.toml")
        self.assertFalse(self.tgt.config_path.exists())
        self.assertEqual(self.tgt.config(), {})

    def test_malformed_toml_aborts_naming_the_file_and_the_problem(self):
        self.locate('[agents]\nimplementer = "unterminated\n')

        with self.assertRaises(SystemExit) as raised:
            self.tgt.config()

        message = str(raised.exception)
        self.assertIn(str(self.tgt.config_path), message)
        # The parser's own complaint, not just "could not read config": the
        # operator has to be told which line to go fix.
        self.assertIn("line 2", message)

    def test_the_config_is_read_for_the_target_the_command_line_names(self):
        # Not at import, and not for the default target: a broken config file
        # sitting next to some other repository is that repository's problem.
        # `cli()` reads the one the run named, before it claims anything.
        target = self.locate().path
        self.write_config("[agents\n")

        with self.assertRaises(SystemExit) as raised:
            holophyte.cli.cli([str(target), "--report"])

        self.assertIn(str(self.tgt.config_path), str(raised.exception))

    def test_two_targets_in_one_process_each_read_their_own_config(self):
        # The point of a value over module state: the `serve` daemon and the
        # supervisor both want two targets live at once, and the config one
        # of them reads must not become the other's.
        self.locate()
        targets = []
        for name in ("one", "two"):
            path = self.root / name / "repo"
            path.mkdir(parents=True)
            target = holophyte.target.Target.locate(path)
            target.config_path.parent.mkdir(parents=True)
            target.config_path.write_text(
                f'[agents]\nimplementer = "harness-{name} run"\n')
            targets.append(target)
        first, second = targets

        self.assertEqual(first.config()["agents"]["implementer"],
                         "harness-one run")
        self.assertEqual(second.config()["agents"]["implementer"],
                         "harness-two run")
        # Reading the second changed nothing about the first, and what each
        # routes to is what its own file says.
        self.assertEqual(first.config()["agents"]["implementer"],
                         "harness-one run")
        self.assertEqual(holophyte.config.agent_command(first, "implement", "go"),
                         ["harness-one", "run", "go"])
        self.assertEqual(holophyte.config.agent_command(second, "implement", "go"),
                         ["harness-two", "run", "go"])

    def test_importing_the_module_names_no_target(self):
        # No module-level target, no module-level config, and nothing under
        # the home: a target is something `cli()` builds from the command
        # line, and importing this module is not a command line.
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
                patch.object(holophyte.target, "load_config",
                             side_effect=AssertionError("config read")) as load, \
                patch.object(holophyte.target.Target, "locate",
                             autospec=True) as locate, \
                patch.object(holophyte.target, "adopt_legacy_state",
                             autospec=True) as adopt:
            with contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli(["--help"])

        self.assertEqual(raised.exception.code, 0)
        load.assert_not_called()
        locate.assert_not_called()
        adopt.assert_not_called()
        self.assertEqual(sorted(home.iterdir()), [])

    def test_a_missing_target_is_a_usage_error_that_touches_nothing(self):
        # No default target: a bare `factory.py` used to name one operator's
        # checkout, a path that exists on one machine. Now it is an argparse
        # error -- usage on stderr, a non-zero exit -- and, like `--help`, it
        # is answered before a target is located or the home is touched.
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        stderr = io.StringIO()
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)}), \
                patch.object(holophyte.target.Target, "locate",
                             autospec=True) as locate, \
                patch.object(holophyte.target, "adopt_legacy_state",
                             autospec=True) as adopt:
            with contextlib.redirect_stderr(stderr), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([])

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("usage:", stderr.getvalue())
        self.assertIn("target", stderr.getvalue())
        locate.assert_not_called()
        adopt.assert_not_called()
        self.assertEqual(sorted(home.iterdir()), [])

    def test_unknown_tables_are_left_alone(self):
        # A config written against a later version still loads, and the table
        # this version does read keeps working beside the ones it does not.
        target = self.locate('[notifier]\nchannel = "#factory"\n\n'
                               '[agents]\nimplementer = "harness run"\n').path

        self.assertEqual(self.tgt.config()["notifier"], {"channel": "#factory"})
        self.assertEqual(holophyte.config.agent_command(self.tgt, "implement", "do it"),
                         ["harness", "run", "do it"])
        # And startup tolerates the table: a report against this config runs.
        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])
        report.assert_called_once_with(self.tgt)


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
        self.assertIn(str(self.tgt.config_path), message)
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
                    holophyte.config.check_config_keys(self.tgt)

                self.assertIn(str(self.tgt.config_path), str(raised.exception))

    def test_a_config_of_only_known_keys_passes(self):
        target = self.locate(
            '[agents]\nimplementer = "harness run"\n'
            '[worktree]\nsetup = ["true"]\nsetup_timeout_sec = 30\n'
            '[supervisor]\nheartbeat_stale_min = 7\n'
            '[loop]\nstop_on_failure = false\n').path

        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])

        report.assert_called_once_with(self.tgt)


class LoopConfigTests(ConfigTestCase):
    """`[loop] stop_on_failure`: a boolean, defaulting to today's stop.
    `[loop] order`: `"identifier"` (the default) or `"priority"`."""

    def test_an_absent_table_stops_on_failure(self):
        self.locate()

        self.assertIs(holophyte.config.loop_config(self.tgt).stop_on_failure, True)

    def test_an_absent_order_is_identifier_order(self):
        self.locate()

        self.assertEqual(holophyte.config.loop_config(self.tgt).order, "identifier")

    def test_priority_is_read_as_priority_order(self):
        self.locate('[loop]\norder = "priority"\n')

        self.assertEqual(holophyte.config.loop_config(self.tgt).order, "priority")

    def test_an_unknown_order_is_a_startup_error_naming_the_key_and_values(self):
        """`"urgent"` names no sort the loop has and `1` is not a string:
        startup refuses both, naming the key and the two values it takes,
        before anything is claimed."""
        for line in ('order = "urgent"', "order = 1", 'order = "Identifier"'):
            with self.subTest(line=line):
                target = self.locate(f"[loop]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[loop]", message)
                self.assertIn("order", message)
                self.assertIn('"identifier"', message)
                self.assertIn('"priority"', message)
                report.assert_not_called()

    def test_false_is_read_as_go_on(self):
        self.locate('[loop]\nstop_on_failure = false\n')

        self.assertIs(holophyte.config.loop_config(self.tgt).stop_on_failure, False)

    def test_a_non_boolean_is_a_startup_error_naming_the_key(self):
        """`"yes"` is a string, `1` an int: neither is the answer TOML's
        `true` is, and neither is quietly read as one. Startup refuses it
        for every mode, before anything is claimed."""
        for line in ('stop_on_failure = "yes"', "stop_on_failure = 1",
                     'stop_on_failure = "false"'):
            with self.subTest(line=line):
                target = self.locate(f"[loop]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[loop]", message)
                self.assertIn("stop_on_failure", message)
                self.assertIn("boolean", message)
                report.assert_not_called()


    def test_review_round_keys_default_to_the_two_round_cap(self):
        """No table: the base is two rounds, one more per 800 changed
        lines, four at most."""
        self.locate()

        cfg = holophyte.config.loop_config(self.tgt)
        self.assertEqual((cfg.review_rounds, cfg.review_rounds_per_lines,
                          cfg.review_rounds_max), (2, 800, 4))

    def test_review_round_keys_are_validated(self):
        """`review_rounds = 0` is a run with no review; a ceiling under the
        base is a cap the formula could never reach; a boolean or a string
        is not a count. Each is a startup error naming the table and the
        key, before anything is claimed (KO-299)."""
        cases = [("review_rounds = 0", "review_rounds"),
                 ("review_rounds = 3\nreview_rounds_max = 2",
                  "review_rounds_max"),
                 ("review_rounds_max = 0", "review_rounds_max"),
                 ("review_rounds_per_lines = -1", "review_rounds_per_lines"),
                 ("review_rounds = true", "review_rounds"),
                 ('review_rounds = "2"', "review_rounds")]
        for lines, key in cases:
            with self.subTest(lines=lines):
                target = self.locate(f"[loop]\n{lines}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[loop]", message)
                self.assertIn(key, message)
                report.assert_not_called()

        # `0` is the documented switch for "never scale", not an error.
        self.locate("[loop]\nreview_rounds_per_lines = 0\n")
        self.assertEqual(
            holophyte.config.loop_config(self.tgt).review_rounds_per_lines, 0)

    def test_workers_defaults_to_one_process(self):
        self.locate()

        self.assertEqual(holophyte.config.loop_config(self.tgt).workers, 1)

    def test_workers_must_be_an_integer_of_at_least_one(self):
        """`"3"` is a string and `0` a pool that could work nothing: each
        is a startup error naming `[loop] workers`, before anything is
        claimed (KO-343)."""
        for line in ('workers = "3"', "workers = 0"):
            with self.subTest(line=line):
                target = self.locate(f"[loop]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[loop] workers", message)
                self.assertIn("at least 1", message)
                report.assert_not_called()

    def test_tick_sec_defaults_to_two_minutes(self):
        self.locate()

        self.assertEqual(holophyte.config.loop_config(self.tgt).tick_sec, 120)

    def test_tick_sec_must_be_an_integer_of_at_least_ten(self):
        """`"120"` is a string and `5` a poll the board could not bear: each
        is a startup error naming `[loop] tick_sec` (KO-353)."""
        for line in ('tick_sec = "120"', "tick_sec = 5"):
            with self.subTest(line=line):
                target = self.locate(f"[loop]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[loop] tick_sec", message)
                self.assertIn("at least 10", message)
                report.assert_not_called()



class StateDirectoryTests(ConfigTestCase):
    """Every per-target artifact lives under one `HOLOPHYTE_HOME/SLUG/`.

    `Target.locate()` derives the directory and the three paths in it
    together, so the tests go through it and look at what it derived.
    """

    def test_config_store_and_lock_share_the_target_directory(self):
        target = self.locate().path
        holo = self.tgt.holo_dir

        self.assertEqual(holo.parent, self.home)
        self.assertTrue(holo.name.startswith("repo-"), holo)
        self.assertEqual(self.tgt.config_path, holo / "config.toml")
        self.assertEqual(self.tgt.store_path, holo / "store.db")
        self.assertEqual(holophyte.supervisor.supervisor_lock_path(self.tgt),
                         holo / "supervisor.lock")
        # The worktree directory is heavy git state, not factory state, and
        # keeps its own sibling address.
        self.assertEqual(self.tgt.worktrees, target.parent / "repo.worktrees")

    def test_two_targets_with_one_basename_get_two_state_directories(self):
        # The whole reason the directory carries a hash: `/a/repo` and
        # `/b/repo` are different repositories with different histories.
        self.locate()
        one = (self.root / "one" / "repo")
        two = (self.root / "two" / "repo")
        one.parent.mkdir()
        two.parent.mkdir()

        first = holophyte.target.Target.locate(one).holo_dir
        second = holophyte.target.Target.locate(two).holo_dir

        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, second.parent)

    def test_the_directory_is_created_on_first_need_and_nothing_else_is(self):
        target = self.locate().path
        holo = self.tgt.holo_dir
        self.assertFalse(holo.exists())

        conn = holophyte.runs.open_store(self.tgt)
        self.addCleanup(conn.close)
        lock = holophyte.supervisor.acquire_supervisor_lock(
            holophyte.supervisor.supervisor_lock_path(self.tgt), self.tgt.path)
        self.addCleanup(holophyte.supervisor.release_supervisor_lock, lock)

        self.assertTrue((holo / "store.db").exists())
        self.assertTrue((holo / "supervisor.lock").exists())
        # Nothing dotted is left beside the target any more.
        beside = sorted(p.name for p in target.parent.iterdir())
        self.assertEqual(beside, ["home", "repo"])

    def test_a_target_with_no_store_gets_no_directory_either(self):
        self.locate()
        out = io.StringIO()

        holophyte.loop.report(self.tgt, out=out)

        self.assertIn("no store at", out.getvalue())
        self.assertFalse(self.tgt.holo_dir.exists())


class LegacyAdoptionTests(ConfigTestCase):
    """The one-time move of pre-`~/.holophyte` state into the new directory.

    KO-165 changed the address without moving what was at the old one, and a
    run against the new empty store shadowed fifteen runs and the target's
    agent routes. These go through `Target.locate()` for that reason:
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
            self.tgt = holophyte.target.Target.locate(self.target)
        self.printed = printed.getvalue()
        return self.tgt

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

        self.assertEqual(self.tgt.store_path.read_bytes(), b"legacy store\n")
        self.assertEqual(self.tgt.config()["agents"]["implementer"],
                         "harness run")
        self.assertFalse(holo.exists())
        self.assertIn(str(holo / "store.db"), self.printed)
        self.assertIn(str(self.tgt.store_path), self.printed)

    def test_the_dotted_siblings_are_adopted_with_their_sidecars(self):
        db = self.legacy_siblings()

        self.locate()

        self.assertEqual(self.tgt.store_path.read_bytes(), b"legacy store\n")
        self.assertEqual(
            self.tgt.holo_dir.joinpath("store.db-wal").read_bytes(), b"wal\n")
        self.assertEqual(
            self.tgt.holo_dir.joinpath("store.db-shm").read_bytes(), b"shm\n")
        self.assertEqual(self.tgt.config()["agents"]["implementer"],
                         "harness run")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["home", "repo"])
        self.assertIn(str(db), self.printed)

    def test_two_stores_are_refused_rather_than_one_shadowing_the_other(self):
        holo = self.legacy_directory()
        new = holophyte.target.state_dir(self.target)
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
        new = holophyte.target.state_dir(self.target)
        new.mkdir(parents=True)
        (new / "config.toml").write_text("[agents]\n")

        self.locate()

        self.assertEqual(self.tgt.store_path.read_bytes(), b"legacy store\n")
        self.assertFalse(holo.exists())
        self.assertIn(str(self.tgt.store_path), self.printed)

    def test_a_file_already_at_the_new_address_is_refused_not_overwritten(self):
        holo = self.legacy_directory()
        new = holophyte.target.state_dir(self.target)
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
        """`Target.locate(..., adopt=False)` derives the paths and nothing else.

        Adoption is a side effect the caller asks for: a value built for a
        target nobody is about to run against -- a daemon enumerating a
        host's targets, a test naming a directory -- must not move that
        target's state, and where the target has two stores must not exit.
        `cli()` asks; nothing else does.
        """
        holo = self.legacy_directory()
        new = holophyte.target.state_dir(self.target)
        new.mkdir(parents=True)
        (new / "store.db").write_bytes(b"new store\n")

        target = holophyte.target.Target.locate(self.target, adopt=False)

        self.assertEqual((holo / "store.db").read_bytes(), b"legacy store\n")
        self.assertEqual(target.store_path.read_bytes(), b"new store\n")

    def test_adoption_happens_once_and_a_later_run_leaves_the_target_alone(self):
        self.legacy_directory()
        self.locate()

        # A second target's worth of legacy state appearing later must not be
        # swept in on top of a state directory that is already the real one.
        (self.root / "repo.holophyte.toml").write_text("[agents]\n")
        self.locate()

        self.assertEqual(self.tgt.store_path.read_bytes(), b"legacy store\n")
        self.assertTrue((self.root / "repo.holophyte.toml").exists())
        self.assertEqual(self.printed, "")


class AgentCommandTests(ConfigTestCase):
    WORKTREE = Path("/tmp/holophyte-config-contract")

    def test_an_absent_config_leaves_todays_routes_byte_identical(self):
        self.locate()

        with patch.object(holophyte.agents, "run_capped") as run:
            run.return_value = (0, "implemented")
            holophyte.agents.agent(self.tgt, "implement", "make the change",
                                   self.WORKTREE)
        with patch.object(review_runner, "run_review") as run_review:
            run_review.return_value = "VERDICT: APPROVE"
            holophyte.agents.agent(self.tgt, "review", "review it", self.WORKTREE,
                          base_sha="1" * 40, candidate_sha="2" * 40)

        self.assertIsNone(
            holophyte.config.agent_command(self.tgt, "implement", "make the change"))
        run.assert_called_once_with(
            ["claude", "-p", "make the change",
             "--model", "opus", "--effort", "high"],
            self.WORKTREE, 1800,
        )
        # The reviewer still goes through the hardened container, not argv.
        self.assertEqual(run_review.call_args.kwargs["profile"],
                         "codex-sol-medium")

    def test_an_implementer_override_replaces_the_argv(self):
        self.locate('[agents]\n'
                      'implementer = "claude --model sonnet --effort medium -p"\n')

        with patch.object(holophyte.agents, "run_capped") as run:
            run.return_value = (0, "implemented")
            result = holophyte.agents.agent(self.tgt, "implement", "make the change",
                                   self.WORKTREE)

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
                patch.object(subprocess, "run") as run:
            run.return_value.stdout = "VERDICT: APPROVE"
            run.return_value.stderr = ""
            result = holophyte.agents.agent(self.tgt, "review", "review it",
                                            self.WORKTREE,
                                   base_sha="1" * 40, candidate_sha="2" * 40)

        self.assertEqual(result, "VERDICT: APPROVE")
        run_review.assert_not_called()
        # The prompt this route is handed talks about refs/review/base and
        # refs/review/candidate, so the worktree it runs in has to have them.
        publish.assert_called_once_with(self.WORKTREE, "1" * 40, "2" * 40)
        run.assert_called_once_with(
            ["my-reviewer", "--diff", "review it"],
            cwd=self.WORKTREE, capture_output=True, text=True, timeout=1800,
        )
        # The adjudicator is a separate key: overriding one role leaves the
        # other on its default route.
        self.assertIsNone(
            holophyte.config.agent_command(self.tgt, "adjudicate", "adjudicate it"))

    def test_the_round_records_the_route_that_actually_ran_it(self):
        self.locate('[agents]\nreviewer = "my-reviewer --diff"\n')

        with patch.object(store, "record_review_round") as record:
            holophyte.runs.record_round(self.tgt, object(), "run-1", 1, "review",
                                 "VERDICT: APPROVE", "echo ok", True, "")
            holophyte.runs.record_round(self.tgt, object(), "run-1", 2, "adjudicate",
                                 "VERDICT: PASS", "echo ok", True, "")

        # The override ran the review round, so the row names it; the
        # adjudicator went through the default container and says so.
        self.assertEqual(record.call_args_list[0].args[4], "my-reviewer --diff")
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
                    holophyte.config.agent_command(self.tgt, "implement",
                                                   "make the change")

                self.assertIn(str(self.tgt.config_path), str(raised.exception))
                self.assertIn(expected, str(raised.exception))


class StartupCheckTests(ConfigTestCase):
    """Configured routes resolve before a ticket is claimed, not mid-round.

    A round dispatches its command after the run holds the project lease and
    the worktree exists, so a name that resolves nowhere used to abandon work
    in flight. `check_agent_commands()` moves that failure to startup.
    """

    def setUp(self):
        # Every test starts on a PATH where both default routes answer and
        # the host's own `claude` and `docker` are shadowed: what these tests
        # say about a configured route must not depend on what the machine
        # running them happens to have installed. A test about a default
        # route that is missing or down calls `stub_path()` again.
        self.stub_path()

    def stub_path(self, *, claude=True, docker="ok", image=True, system=True):
        """Put a PATH in place holding stubs for the default routes.

        `claude` is a no-op script or absent; `docker` is a script whose
        `info` answers as a live daemon ("ok"), as a stopped one ("down"),
        as one that never answers ("hang"), or is absent (None). A live
        daemon reports the review image as built (`image`) or not. `system`
        keeps /usr/bin and /bin behind the stubs for the tests that also name
        a real program; the stubs shadow any real `docker` there.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir = Path(tmp.name)
        if claude:
            self.stub(bindir / "claude", "exit 0\n")
        if docker == "ok":
            self.stub(bindir / "docker",
                      'if [ "$1" = info ]; then echo "Server Version: 27"; '
                      'exit 0; fi\n'
                      'if [ "$1" = image ] && [ "$2" = inspect ]; then '
                      f'exit {0 if image else 1}; fi\n'
                      'exit 1\n')
        elif docker == "down":
            self.stub(bindir / "docker",
                      'echo "Cannot connect to the Docker daemon at '
                      'unix:///var/run/docker.sock. Is the docker daemon '
                      'running?" >&2\nexit 1\n')
        elif docker == "hang":
            self.stub(bindir / "docker", "sleep 30\n")
        path = str(bindir) + (":/usr/bin:/bin" if system else "")
        patcher = patch.dict(os.environ, {"PATH": path})
        patcher.start()
        self.addCleanup(patcher.stop)
        return bindir

    @staticmethod
    def stub(script, body):
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)

    def repo(self, *, origin):
        """Make the fixture target a git repository, with or without `origin`."""
        subprocess.run(["git", "init", "-q"], cwd=self.target, check=True)
        if origin:
            subprocess.run(["git", "remote", "add", "origin",
                            "https://github.com/example/repo.git"],
                           cwd=self.target, check=True)

    def test_pr_mode_with_no_origin_is_a_startup_error_naming_both(self):
        """`[merge] mode = "pr"` pushes to `origin`; a target without one is
        refused before anything is claimed, naming the key and the remote."""
        self.locate('[merge]\nmode = "pr"\n')
        self.repo(origin=False)

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.tgt)

        message = str(raised.exception)
        self.assertIn("[merge] mode", message)
        self.assertIn("origin", message)
        self.assertIn(str(self.tgt.config_path), message)

    def test_pr_mode_probes_gh_auth_and_refuses_a_failed_one(self):
        """With `origin` in place the route is `gh auth status`: a stub that
        answers is a pass, one that refuses is a startup error naming `gh`;
        with no `gh` and no token in the environment, the error names both."""
        bindir = self.stub_path()
        self.locate('[merge]\nmode = "pr"\n')
        self.repo(origin=True)
        self.stub(bindir / "gh", 'if [ "$1" = auth ]; then exit 0; fi\nexit 1\n')
        self.assertIsNone(holophyte.config.check_agent_commands(self.tgt))

        self.stub(bindir / "gh", 'echo "You are not logged into any GitHub '
                                 'hosts" >&2\nexit 1\n')
        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.tgt)
        self.assertIn("gh auth status", str(raised.exception))
        self.assertIn("not logged into", str(raised.exception))

        # No `gh` at all: the program is renamed to one nothing on PATH
        # answers to, since the host running this may well have a real,
        # authenticated `gh` in a system directory the stubs sit ahead of.
        with patch.object(holophyte.pr, "GH", "gh-absent-under-test"), \
                patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}):
            with self.assertRaises(SystemExit) as raised:
                holophyte.config.check_agent_commands(self.tgt)
            self.assertIn("'gh-absent-under-test' on PATH",
                          str(raised.exception))
            self.assertIn("GH_TOKEN or GITHUB_TOKEN", str(raised.exception))
            with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test"}):
                self.assertIsNone(
                    holophyte.config.check_agent_commands(self.tgt))

    def test_a_startup_check_of_a_resolvable_command_passes(self):
        # `sh` is on PATH everywhere the factory runs; a bare name is the
        # documented normal way to write one of these.
        self.locate('[agents]\nimplementer = "sh -c"\n'
                      f'reviewer = "{Path(sys.executable)} -c"\n')

        self.assertIsNone(holophyte.config.check_agent_commands(self.tgt))

    def test_an_absent_agents_table_passes_when_the_default_routes_answer(self):
        self.locate()

        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertIsNone(holophyte.config.check_agent_commands(self.tgt))

        # A built image is nothing to remark on.
        self.assertEqual(printed.getvalue(), "")

    def test_an_unbuilt_review_image_is_reported_not_refused(self):
        # The runner builds the image on the first review that finds it
        # missing, so a fresh host is told what to expect and proceeds.
        self.stub_path(image=False)
        self.locate()

        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertIsNone(holophyte.config.check_agent_commands(self.tgt))

        self.assertIn(review_runner.IMAGE, printed.getvalue())
        self.assertIn(str(review_runner.DOCKERFILE), printed.getvalue())

    def test_a_missing_claude_is_a_startup_error_naming_the_override_key(self):
        self.stub_path(claude=False, system=False)
        target = self.locate().path

        with patch.object(holophyte.cli, "main",
                          side_effect=AssertionError("claimed work")) as main:
            with self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([str(target)])

        message = str(raised.exception)
        self.assertIn("claude", message)
        self.assertIn("[agents] implementer", message)
        main.assert_not_called()

    def test_a_bad_review_route_is_a_startup_error_naming_the_key(self):
        # An effort outside Codex's vocabulary, an empty model, or the pair
        # beside a `reviewer` command that opts out of the container it
        # routes: each is refused before a ticket is claimed, naming the
        # table and the key, on the same path the missing-claude check runs.
        for config, key, reason in (
            ('[agents]\nreview_effort = "max"\n', "review_effort",
             "low, medium, high, xhigh"),
            ('[agents]\nreview_model = ""\n', "review_model", "non-empty"),
            ('[agents]\nreview_model = "gpt-6-astra"\n'
             'reviewer = "sh -c"\n', "review_model", "beside [agents] reviewer"),
        ):
            with self.subTest(key=key, config=config):
                target = self.locate(config).path
                with patch.object(holophyte.cli, "main",
                                  side_effect=AssertionError("claimed work")) \
                        as main:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target)])
                self.assertIn(f"[agents] {key}", str(raised.exception))
                self.assertIn(reason, str(raised.exception))
                self.assertNotIn("unknown key", str(raised.exception))
                main.assert_not_called()

    def test_the_review_route_is_the_configured_pair_or_the_default(self):
        self.locate('[agents]\nreview_model = "gpt-6-astra"\n'
                      'review_effort = "xhigh"\n')
        self.assertEqual(holophyte.config.review_route(self.tgt),
                         ("gpt-6-astra", "xhigh"))
        self.assertIsNone(holophyte.config.check_agent_commands(self.tgt))

        self.locate()
        self.assertEqual(holophyte.config.review_route(self.tgt),
                         ("gpt-5.6-sol", "medium"))

    def test_a_missing_docker_is_a_startup_error_naming_the_override_key(self):
        self.stub_path(docker=None, system=False)
        self.locate()

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.tgt)

        message = str(raised.exception)
        self.assertIn("docker", message)
        self.assertIn("[agents] reviewer and adjudicator", message)

    def test_a_stopped_docker_daemon_is_a_startup_error_before_any_claim(self):
        self.stub_path(docker="down")
        target = self.locate().path

        with patch.object(holophyte.cli, "main",
                          side_effect=AssertionError("claimed work")) as main:
            with self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([str(target)])

        message = str(raised.exception)
        self.assertIn("Docker daemon", message)
        self.assertIn("Is the docker daemon running?", message)
        self.assertIn("[agents] reviewer", message)
        main.assert_not_called()

    def test_a_docker_daemon_that_never_answers_is_capped(self):
        self.stub_path(docker="hang")
        self.locate()

        with patch.object(holophyte.config, "DOCKER_PROBE_TIMEOUT", 1):
            start = time.monotonic()
            with self.assertRaises(SystemExit) as raised:
                holophyte.config.check_agent_commands(self.tgt)

        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("did not answer `docker info` within 1s",
                      str(raised.exception))

    def test_a_configured_reviewer_still_probes_docker_for_the_adjudicator(self):
        # The adjudicator is its own key and falls to the container route
        # when only the reviewer is overridden.
        self.stub_path(docker="down")
        self.locate('[agents]\nreviewer = "sh -c"\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.tgt)

        message = str(raised.exception)
        self.assertIn("[agents] adjudicator not set", message)
        self.assertNotIn("reviewer and adjudicator", message)

    def test_a_configured_route_is_resolved_and_not_probed(self):
        # A docker that is down is not the problem of a target that routes
        # every role somewhere else.
        self.stub_path(claude=False, docker="down")
        self.locate('[agents]\nimplementer = "sh -c"\n'
                      'reviewer = "sh -c"\nadjudicator = "sh -c"\n')

        self.assertIsNone(holophyte.config.check_agent_commands(self.tgt))

    def test_a_program_that_is_not_on_path_is_a_startup_error(self):
        self.locate('[agents]\nreviewer = "holophyte-no-such-reviewer --diff"\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.tgt)

        message = str(raised.exception)
        self.assertIn(str(self.tgt.config_path), message)
        # The key the operator wrote, and the word that did not resolve.
        self.assertIn("reviewer", message)
        self.assertIn("holophyte-no-such-reviewer", message)

    def test_a_file_that_is_not_executable_is_a_startup_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tool = Path(tmp.name) / "tool.sh"
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o644)
        self.locate(f'[agents]\nadjudicator = "{tool} --final"\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.tgt)

        self.assertIn("adjudicator", str(raised.exception))

    def test_a_relative_program_path_is_refused_rather_than_guessed_at(self):
        # It would resolve inside a task worktree that does not exist yet, so
        # startup cannot check the file the round would actually run.
        self.locate('[agents]\nreviewer = "./review.sh --diff"\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.tgt)

        self.assertIn("relative", str(raised.exception))

    def test_the_check_reuses_the_parse_a_round_would_use(self):
        # An unquotable or wrongly-typed command is caught here too, with the
        # same message a round would have raised -- one parser, not two.
        self.locate('[agents]\nimplementer = ["claude", "-p"]\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.tgt)

        self.assertIn("command string", str(raised.exception))

    def test_a_run_checks_its_commands_before_claiming_anything(self):
        target = self.locate(
            '[agents]\nimplementer = "holophyte-no-such-harness -p"\n').path

        with patch.object(holophyte.cli, "main",
                          side_effect=AssertionError("claimed work")) as main:
            with self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([str(target)])

        self.assertIn("holophyte-no-such-harness", str(raised.exception))
        main.assert_not_called()

    def test_report_does_not_require_the_configured_commands(self):
        # `--report` reads the store and calls nobody, so a reviewer that is
        # not installed on the machine reading the table is not its problem.
        target = self.locate(
            '[agents]\nreviewer = "holophyte-no-such-reviewer --diff"\n').path

        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])

        report.assert_called_once_with(self.tgt)

    def test_read_only_modes_do_not_probe_the_default_routes(self):
        # `--report` and `--sweep` dispatch nobody, so a host with no `claude`
        # and Docker stopped can still read the store.
        self.stub_path(claude=False, docker="down", system=False)
        target = self.locate('[board]\nproject_id = "p-1"\nteam = "T"\n').path

        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])
        with patch.object(holophyte.cli, "sweep_report") as sweep_report:
            holophyte.cli.cli([str(target), "--sweep"])

        report.assert_called_once_with(self.tgt)
        # The board is handed down from `cli()`, never reached for by name;
        # building it reads no config and opens no connection.
        sweep_report.assert_called_once_with(self.tgt, act=False, provider=ANY)
        self.assertIsInstance(sweep_report.call_args.kwargs["provider"],
                              LinearProvider)


class BoardConfigTests(StartupCheckTests):
    """`[board] project_id` and `[board] team`: the board is the target's.

    Three startup outcomes: the table names the board; no table, and the
    loop exits naming the key while `--report` still prints, whatever the
    environment holds; a misspelt key is refused like one in any other
    table. The routes are stubbed by the parent's `setUp()` so the loop path
    reaches the board, not a missing `claude`.
    """

    # The retired `HOLO2_*` fallback: set in every test so a stand-in
    # would be caught if it came back.
    RETIRED_ENV = {"HOLO2_PROJECT_ID": "p-env", "HOLO2_TEAM": "Env Team"}

    def start_loop(self, target):
        """Run `cli([target])` to the point the loop would claim, and hand
        back the board it was built with and what startup printed."""
        printed = io.StringIO()
        with patch.object(holophyte.cli, "main") as main, \
                contextlib.redirect_stdout(printed):
            holophyte.cli.cli([str(target)])
        main.assert_called_once()
        return main.call_args.args[1], printed.getvalue()

    def queries_from(self, board):
        """Drive one claim and one state change through `board` with the
        transport captured; the (query, variables) pairs it sent."""
        import linear_provider
        calls = []

        def fake(query, variables=None):
            calls.append((query, variables))
            if "workflowStates" in query:
                return {"workflowStates": {"nodes": [
                    {"id": "state-uuid", "name": "Done", "type": "completed"}]}}
            if "issueUpdate" in query:
                return {"issueUpdate": {"success": True}}
            return {"project": {"issues": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}

        with patch.object(linear_provider, "_gql", fake), \
                contextlib.redirect_stdout(io.StringIO()):
            board.claim_next()
            board.set_state("uuid-KO-1", "Done")
        return calls

    def assert_queries_name(self, calls, project_id, team):
        ready = [v["project"] for q, v in calls if "nin:" in q]
        states = [v["team"] for q, v in calls if "workflowStates" in q]
        self.assertEqual(ready, [project_id])
        self.assertEqual(states, [team])

    def test_the_table_names_the_project_and_team_the_queries_use(self):
        target = self.locate('[board]\nproject_id = "p-1"\nteam = "T"\n').path

        with patch.dict(os.environ, self.RETIRED_ENV):
            board, printed = self.start_loop(target)
            calls = self.queries_from(board)

        self.assert_queries_name(calls, "p-1", "T")
        self.assertEqual(board.team, "T")
        # Nothing announces a fallback: the only startup line is the
        # supervisor the loop started.
        self.assertEqual(
            [line for line in printed.splitlines()
             if not line.startswith("[holo2] started a supervisor")], [])

    def test_no_table_is_a_loop_exit_naming_the_key_despite_the_environment(self):
        """`HOLO2_PROJECT_ID`/`HOLO2_TEAM` set and no table: the loop and
        `--supervise` exit naming the key, nothing announces a fallback, and
        `--report` still prints."""
        target = self.locate().path

        with patch.dict(os.environ, self.RETIRED_ENV):
            printed = io.StringIO()
            with patch.object(holophyte.cli, "main") as main, \
                    contextlib.redirect_stdout(printed):
                with self.assertRaises(SystemExit) as raised:
                    holophyte.cli.cli([str(target)])
            with patch.object(holophyte.cli, "supervise") as supervise, \
                    contextlib.redirect_stdout(printed):
                with self.assertRaises(SystemExit):
                    holophyte.cli.cli([str(target), "--supervise"])
            report = io.StringIO()
            with contextlib.redirect_stdout(report):
                status = holophyte.cli.cli([str(target), "--report"])

        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("[board] project_id", str(raised.exception))
        self.assertIn(str(self.tgt.config_path), str(raised.exception))
        self.assertNotIn("HOLO2_", str(raised.exception))
        self.assertNotIn("[board] table absent", printed.getvalue())
        main.assert_not_called()
        supervise.assert_not_called()
        self.assertIn(status, (None, 0))
        self.assertIn("no store", report.getvalue())

    def test_a_read_only_sweep_runs_without_a_board(self):
        target = self.locate().path

        with patch.dict(os.environ, self.RETIRED_ENV), \
                patch.object(holophyte.cli, "sweep_report") as sweep_report:
            holophyte.cli.cli([str(target), "--sweep"])

        sweep_report.assert_called_once_with(self.tgt, act=False, provider=None)

    def test_a_misspelt_key_is_a_startup_error_naming_it(self):
        target = self.locate('[board]\nprojet_id = "x"\n').path

        with patch.object(holophyte.cli, "main") as main:
            with self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([str(target)])

        message = str(raised.exception)
        self.assertIn("[board]", message)
        self.assertIn("projet_id", message)
        self.assertIn("project_id", message)
        main.assert_not_called()

    def test_half_a_table_is_a_startup_error_naming_the_missing_key(self):
        """A table that names only one half of the board is refused where
        the loop would read it."""
        env = self.RETIRED_ENV
        for config, key in (('[board]\nproject_id = "p-1"\n', "team"),
                            ('[board]\nteam = "T"\n', "project_id"),
                            ('[board]\nproject_id = ""\nteam = "T"\n', "project_id")):
            with self.subTest(config=config):
                target = self.locate(config).path

                with patch.dict(os.environ, env), \
                        patch.object(holophyte.cli, "main") as main:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target)])

                self.assertIn(f"[board] {key}", str(raised.exception))
                main.assert_not_called()


class SupervisorSpawnTests(StartupCheckTests):
    """The loop starts a supervisor for its target when none is watching.

    The spawn is a `Popen` the fixture stubs; what these tests read is its
    argument list and what startup printed. The routes are stubbed by the
    parent's `setUp()` so the loop path reaches the spawn, not a missing
    `claude`.
    """

    BOARD = '[board]\nproject_id = "p-1"\nteam = "T"\n'

    class EmptyBoard:
        """A board with no ready tickets, in the provider's shape.

        Stands in for `LinearProvider` where `cli()` builds it, so the real
        `main()` runs: opens the store, sweeps, asks for a ticket, is told
        there is none, and exits on its "no ready tickets" line. The spawn
        under test sits between the startup checks and that call.
        """

        def __init__(self, project_id, team):
            self.team = team

        def ready_issues(self):
            return []

        def claim_next(self, skip=(), order="identifier"):
            return None

    def start_loop(self, target):
        printed = io.StringIO()
        with patch.object(holophyte.cli, "LinearProvider", self.EmptyBoard), \
                contextlib.redirect_stdout(printed):
            holophyte.cli.cli([str(target)])
        out = printed.getvalue()
        self.assertIn("[holo2] Linear has no ready tickets. done.", out)
        return out

    def hold_lock(self, pid):
        lock = holophyte.supervisor.supervisor_lock_path(self.tgt)
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"host {pid} 1\n")
        return lock

    def test_a_free_lock_starts_a_detached_supervisor_for_the_target(self):
        target = self.locate(self.BOARD).path

        printed = self.start_loop(target)

        self.popen.assert_called_once()
        argv = self.popen.call_args.args[0]
        kwargs = self.popen.call_args.kwargs
        self.assertEqual(argv[-2:], ["--supervise", str(target)])
        self.assertTrue(argv[-3].endswith("factory.py"), argv)
        self.assertTrue(kwargs["start_new_session"])
        log = self.tgt.holo_dir / "supervisor.log"
        self.assertEqual(Path(kwargs["stdout"].name), log)
        self.assertEqual(Path(kwargs["stderr"].name), log)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIn(f"[holo2] started a supervisor for {target} as pid 4242",
                      printed)

    def test_a_lock_held_by_a_live_pid_is_left_alone_and_named(self):
        target = self.locate(self.BOARD).path
        self.hold_lock(os.getpid())

        printed = self.start_loop(target)

        self.popen.assert_not_called()
        self.assertIn(f"[holo2] supervisor pid {os.getpid()} is watching {target}",
                      printed)
        self.assertNotIn("started a supervisor", printed)

    def test_a_lock_held_by_a_dead_pid_does_not_stop_the_spawn(self):
        target = self.locate(self.BOARD).path
        with subprocess.Popen(["true"]) as gone:
            gone.wait()
        self.hold_lock(gone.pid)

        printed = self.start_loop(target)

        self.popen.assert_called_once()
        self.assertIn("started a supervisor", printed)

    def test_spawn_supervisor_false_starts_nothing(self):
        target = self.locate(self.BOARD + "[loop]\nspawn_supervisor = false\n").path

        printed = self.start_loop(target)

        self.popen.assert_not_called()
        self.assertNotIn("supervisor", printed)

    def test_a_non_boolean_spawn_supervisor_is_a_startup_error_naming_it(self):
        target = self.locate(self.BOARD + '[loop]\nspawn_supervisor = "no"\n').path

        with patch.object(holophyte.cli, "main") as main:
            with self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([str(target)])

        self.assertIn("spawn_supervisor", str(raised.exception))
        # `main` stays a mock here: the exit is the assertion, and a real
        # loop reaching it would mean the bad key had been read past.
        main.assert_not_called()
        self.popen.assert_not_called()

    def test_the_other_modes_start_no_supervisor(self):
        target = self.locate(self.BOARD).path
        modes = (["--report"], ["--sweep"], ["--serve", "127.0.0.1:0"],
                 ["--requeue", "KO-1", "--note", "why"],
                 ["--file-ticket", str(self.root / "t.md")])
        for argv in modes:
            with self.subTest(argv=argv), \
                    patch.object(holophyte.cli, "report"), \
                    patch.object(holophyte.cli, "sweep_report"), \
                    patch.object(holophyte.cli, "serve"), \
                    patch.object(holophyte.cli, "requeue"), \
                    patch.object(holophyte.cli, "file_ticket"), \
                    contextlib.redirect_stdout(io.StringIO()):
                holophyte.cli.cli([str(target), *argv])
        self.popen.assert_not_called()

    def test_a_worker_runs_one_ticket_and_starts_no_supervisor(self):
        """`--worker` is a child of the scheduler, which ran the route
        probes and the supervisor spawn for the whole pool: the worker
        goes straight to `worker()` with the board, probing nothing and
        spawning nothing, and exits with the status it returns (KO-343)."""
        target = self.locate(self.BOARD).path

        with patch.object(holophyte.cli, "worker", return_value=3) as worker, \
                patch.object(holophyte.cli, "main") as main, \
                patch.object(holophyte.cli, "check_agent_commands") as probe, \
                patch.object(holophyte.cli, "LinearProvider", self.EmptyBoard), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = holophyte.cli.cli([str(target), "--worker"])

        self.assertEqual(rc, 3)
        worker.assert_called_once()
        self.assertIsInstance(worker.call_args.args[1], self.EmptyBoard)
        main.assert_not_called()
        probe.assert_not_called()
        self.popen.assert_not_called()


class WorktreeSetupTests(ConfigTestCase):
    """`[worktree] setup`: the commands a fresh task worktree is prepared with.

    The wart these exist for is a worktree that borrows the main checkout's
    environment -- a venv, a module cache, a generated file -- and so tests the
    branch against somebody else's dependencies. The commands run in the
    worktree, in order, through the same verify-gate machinery a ticket's
    verify command runs through, before any agent turn is dispatched.
    """

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
                holophyte.loop.run_worktree_setup(self.tgt, self.worktree()),
                             (True, ""))
        self.assertEqual(holophyte.config.setup_commands(self.tgt), [])

    def test_the_commands_run_in_the_worktree_in_the_order_written(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["pwd > where.txt", '
                      '"cp where.txt copied.txt"]\n')

        ok, report = holophyte.loop.run_worktree_setup(self.tgt, wt)

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

        ok, report = holophyte.loop.run_worktree_setup(self.tgt, wt)

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

        ok, report = holophyte.loop.run_worktree_setup(self.tgt, wt)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3", report)
        self.assertIn("failing clause: false", report)
        self.assertIn("not executed: clause 3", report)

    def test_a_silent_failure_is_reported_as_silence(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["exit 1"]\n')

        ok, report = holophyte.loop.run_worktree_setup(self.tgt, wt)

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
            ok, report = holophyte.loop.run_worktree_setup(self.tgt, wt)

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
            ok, report = holophyte.loop.run_worktree_setup(self.tgt, wt)
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
        ok, report = holophyte.loop.run_worktree_setup(self.tgt, wt)

        self.assertFalse(ok)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("timed out after 1s", report)
        self.assertIn("sleep 30", report)

    def test_the_default_setup_cap_is_the_verify_cap(self):
        self.locate('[worktree]\nsetup = ["make deps"]\n')

        self.assertEqual(holophyte.config.setup_timeout(self.tgt),
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
                self.assertIn(str(self.tgt.config_path), message)
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
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[worktree] carry", message)

    def test_an_absent_carry_is_an_empty_list(self):
        self.locate('[worktree]\nsetup = ["make deps"]\n')

        self.assertEqual(holophyte.config.carry_directories(self.tgt), [])

        self.locate('[worktree]\ncarry = ["console/node_modules", ".venv"]\n')
        self.assertEqual(holophyte.config.carry_directories(self.tgt),
                         ["console/node_modules", ".venv"])

    def test_a_silent_timeout_is_reported_as_silence(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["make deps"]\n')

        with patch.object(holophyte.gates, "run_capped", side_effect=
                          subprocess.TimeoutExpired("make deps", 300)):
            ok, report = holophyte.loop.run_worktree_setup(self.tgt, wt)

        self.assertFalse(ok)
        self.assertIn("no output before the timeout", report)

    def test_the_setup_records_a_phase_before_it_runs_anything(self):
        wt = self.worktree()
        self.locate('[worktree]\nsetup = ["true"]\n')
        conn = object()

        with patch.object(store, "set_phase") as set_phase:
            holophyte.loop.run_worktree_setup(self.tgt, wt, conn, "run-1")

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
                    holophyte.config.setup_commands(self.tgt)

                self.assertIn(str(self.tgt.config_path), str(raised.exception))
                self.assertIn(expected, str(raised.exception))

    def test_the_startup_check_reuses_the_parse_a_run_would_use(self):
        self.locate('[worktree]\nsetup = [7]\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_worktree_setup(self.tgt)

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
                    check(self.tgt)

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn(f"{table} must be a table", message)

    def test_a_startup_check_of_a_usable_table_passes(self):
        # Startup settles the shape of the table and deliberately not the
        # commands: they are shell, written against a worktree that does not
        # exist yet.
        self.locate('[worktree]\nsetup = ["holophyte-no-such-tool --install"]\n')

        self.assertIsNone(holophyte.config.check_worktree_setup(self.tgt))

    def test_an_absent_table_checks_nothing(self):
        self.locate()

        self.assertIsNone(holophyte.config.check_worktree_setup(self.tgt))

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

        report.assert_called_once_with(self.tgt)

    def test_an_absent_branch_prefix_is_task(self):
        for config in (None, '[worktree]\nsetup = ["true"]\n'):
            with self.subTest(config=config):
                self.locate(config)
                self.assertEqual(holophyte.config.branch_prefix(self.tgt), "task")

    def test_a_named_branch_prefix_is_read_back(self):
        self.locate('[worktree]\nbranch_prefix = "factory"\n')
        self.assertEqual(holophyte.config.branch_prefix(self.tgt), "factory")

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
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[worktree] branch_prefix", message)
                # The key is known; the refusal is about its value.
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

        reply = holophyte.agents.agent(self.tgt, "review", "review it", root,
                              base_sha=self.base, candidate_sha=self.head)

        # What the command printed is the pair the round is about, read out of
        # the repo it ran in -- not a ref that does not exist there.
        self.assertEqual(reply.split(), [self.base, self.head])

    def test_a_sha_the_repo_does_not_have_is_refused(self):
        root = self.repo()
        self.locate('[agents]\nreviewer = "true"\n')

        with self.assertRaises(review_runner.ReviewBoundaryError):
            holophyte.agents.agent(self.tgt, "review", "review it", root,
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
            holophyte.agents.agent(self.tgt, "adjudicate", "judge it", root,
                          base_sha=unrelated, candidate_sha=self.head)

    def test_the_default_route_is_left_to_stage_its_own_refs(self):
        # The container route builds its own checkout and names the refs
        # there; the task worktree is not where its reviewer looks.
        self.locate()
        root = self.repo()

        with patch.object(holophyte.agents, "publish_review_refs") as publish, \
                patch.object(review_runner, "run_review") as run_review:
            run_review.return_value = "VERDICT: APPROVE"
            holophyte.agents.agent(self.tgt, "review", "review it", root,
                          base_sha=self.base, candidate_sha=self.head)

        publish.assert_not_called()


class ReportConfigTests(ConfigTestCase):
    """`[report] host_label`: a string shown wherever a host is rendered,
    absent by default."""

    def test_an_absent_table_is_no_label(self):
        self.locate()

        self.assertIsNone(holophyte.config.report_config(self.tgt).host_label)

    def test_a_string_is_read_as_the_label(self):
        self.locate('[report]\nhost_label = "writer-1"\n')

        self.assertEqual(holophyte.config.report_config(self.tgt).host_label,
                         "writer-1")

    def test_a_non_string_or_a_mistyped_key_is_a_startup_error_naming_it(self):
        """`3` names no writer and `hots_label` is a key nobody reads: startup
        refuses both, naming the key, before anything is claimed."""
        for line, key in (("host_label = 3", "host_label"),
                          ('host_label = ""', "host_label"),
                          ('hots_label = "x"', "hots_label")):
            with self.subTest(line=line):
                target = self.locate(f"[report]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[report]", message)
                self.assertIn(key, message)
                report.assert_not_called()

    def test_findings_is_none_by_default_and_repo_when_opted_in(self):
        """KO-363: `[report] findings` is `none` when absent -- the store is
        the record and nothing is rendered -- and `repo` for a target that
        wants the rendered file beside its code."""
        self.locate()
        self.assertEqual(holophyte.config.report_config(self.tgt).findings,
                         "none")

        self.locate('[report]\nfindings = "repo"\n')
        self.assertEqual(holophyte.config.report_config(self.tgt).findings,
                         "repo")

    def test_a_findings_mode_nobody_defined_is_a_startup_error_naming_it(self):
        """`"yes"` is not an answer (KO-363), and neither are the modes the
        key had before it: startup fails naming `[report] findings`."""
        for line in ('findings = "yes"', 'findings = "window"',
                     'findings = "off"', "findings = false"):
            with self.subTest(line=line):
                target = self.locate(f"[report]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn("[report]", message)
                self.assertIn("findings", message)
                report.assert_not_called()


class ConsoleConfigTests(ConfigTestCase):
    """`[console] daemons`: the other daemons as `HOST:PORT` strings, each
    held to `--serve`'s address rule, none twice; empty by default."""

    def test_an_absent_table_is_no_daemons(self):
        self.locate()

        self.assertEqual(holophyte.config.console_config(self.tgt).daemons, ())

    def test_a_list_of_addresses_is_read_in_order(self):
        self.locate('[console]\ndaemons = ["writer-2:7710", "writer-3:7711"]\n')

        self.assertEqual(holophyte.config.console_config(self.tgt).daemons,
                         ("writer-2:7710", "writer-3:7711"))

    def test_a_bad_entry_is_a_startup_error_naming_the_key_and_the_entry(self):
        """`"nope"` names no port, `""` nothing, and a duplicate would draw
        one host twice: startup refuses each, naming `[console] daemons` and
        the entry, before anything is served."""
        for line, entry in (('daemons = ["nope"]', "'nope'"),
                            ('daemons = [""]', "''"),
                            ('daemons = ["writer-2:7710", "writer-2:7710"]',
                             "'writer-2:7710'"),
                            ('daemons = "writer-2:7710"', "'writer-2:7710'")):
            with self.subTest(line=line):
                target = self.locate(f"[console]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[console] daemons", message)
                self.assertIn(entry, message)
                report.assert_not_called()

    def test_an_unknown_key_is_a_startup_error(self):
        target = self.locate("[console]\nother = 1\n").path

        with patch.object(holophyte.cli, "report") as report:
            with self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli([str(target), "--report"])

        message = str(raised.exception)
        self.assertIn("[console]", message)
        self.assertIn("other", message)
        self.assertIn("unknown key", message)
        report.assert_not_called()


class MergeConfigTests(ConfigTestCase):
    """`[merge] approve`: `"auto"` (the default) or `"human"`; `[merge] mode`:
    `"local"` (the default) or `"pr"`; `[merge] pr_rounds`: an integer of at
    least 1 (default 5); `[merge] pr_merge_method`: `"merge"` (the default),
    `"squash"` or `"rebase"`; nothing else."""

    def test_an_absent_table_is_auto_and_local(self):
        self.locate()

        self.assertEqual(holophyte.config.merge_config(self.tgt),
                         ("auto", "local", 5, "merge", 180, "ticket", "",
                          "park", ()))

    def test_after_is_read_as_a_list_of_commands(self):
        """`after` is the console build the daemon's bundle depends on, in
        order; absent, nothing runs after a merge (KO-347)."""
        self.locate('[merge]\nafter = ["bun --cwd=console run build",'
                    ' "sh -c true"]\n')

        self.assertEqual(holophyte.config.merge_config(self.tgt).after,
                         ("bun --cwd=console run build", "sh -c true"))

    def test_pr_text_and_pr_style_are_read(self):
        """`pr_text = "written"` with the target's own instructions; absent,
        `"ticket"` and no instructions, the form the PR has always had."""
        self.locate('[merge]\nmode = "pr"\npr_text = "written"\n'
                    'pr_style = "Title starts with [Feature Name]."\n')

        merge = holophyte.config.merge_config(self.tgt)
        self.assertEqual(merge.pr_text, "written")
        self.assertEqual(merge.pr_style, "Title starts with [Feature Name].")

    def test_human_threads_is_read(self):
        """`human_threads = "act"` lets the shepherd act on a person's
        thread; absent, it is `"park"`, KO-327's rule."""
        self.locate('[merge]\nmode = "pr"\nhuman_threads = "act"\n')

        self.assertEqual(
            holophyte.config.merge_config(self.tgt).human_threads, "act")

    def test_pr_merge_method_is_read(self):
        """A squash-only repository names its method; absent, it is
        `"merge"`, the merge commit the shepherd has always asked for."""
        self.locate('[merge]\nmode = "pr"\npr_merge_method = "squash"\n')

        self.assertEqual(
            holophyte.config.merge_config(self.tgt).pr_merge_method, "squash")

    def test_pr_poll_sec_is_read(self):
        """The least interval between two loop-started shepherd rounds on
        one pull request; absent, three minutes (KO-362)."""
        self.locate('[merge]\nmode = "pr"\npr_poll_sec = 60\n')

        self.assertEqual(
            holophyte.config.merge_config(self.tgt).pr_poll_sec, 60)

    def test_pr_poll_sec_must_be_an_integer_of_at_least_ten(self):
        """`"180"` is a string and `5` a poll of GitHub for a reviewer's
        next keystroke: each is a startup error naming
        `[merge] pr_poll_sec` (KO-362)."""
        for line in ('pr_poll_sec = "180"', "pr_poll_sec = 5"):
            with self.subTest(line=line):
                target = self.locate(f"[merge]\nmode = \"pr\"\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[merge] pr_poll_sec", message)
                self.assertIn("at least 10", message)
                report.assert_not_called()

    def test_pr_rounds_is_read(self):
        self.locate('[merge]\nmode = "pr"\npr_rounds = 2\n')

        self.assertEqual(holophyte.config.merge_config(self.tgt).pr_rounds, 2)

    def test_pr_is_read(self):
        self.locate('[merge]\nmode = "pr"\n')

        self.assertEqual(holophyte.config.merge_config(self.tgt).mode, "pr")

    def test_human_is_read(self):
        self.locate('[merge]\napprove = "human"\n')

        self.assertEqual(holophyte.config.merge_config(self.tgt).approve,
                         "human")

    def test_any_other_value_or_key_is_a_startup_error_naming_it(self):
        """`"later"` names no gate and `approve_by` is a key nobody reads:
        startup refuses both, naming the key, before anything is claimed."""
        for line, key in (('approve = "later"', "approve"),
                          ("approve = true", "approve"),
                          ('mode = "github"', "mode"),
                          ("pr_rounds = 0", "pr_rounds"),
                          ("pr_rounds = true", "pr_rounds"),
                          ('pr_rounds = "5"', "pr_rounds"),
                          ('pr_merge_method = "fast-forward"',
                           "pr_merge_method"),
                          ('pr_text = "agent"', "pr_text"),
                          ("pr_style = true", "pr_style"),
                          ('human_threads = "reply"', "human_threads"),
                          ('after = "bun run build"', "after"),
                          ("after = [1]", "after"),
                          ('approve_by = "human"', "approve_by")):
            with self.subTest(line=line):
                target = self.locate(f"[merge]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[merge]", message)
                self.assertIn(key, message)
                report.assert_not_called()


if __name__ == "__main__":
    unittest.main()

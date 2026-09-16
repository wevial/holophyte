"""Per-target config by table: [loop], [supervisor], [report], [console], [merge].

Run: python3 -m unittest discover -s tests -p 'test_config_tables*' -v
"""
import sys
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import holophyte.config
from holophyte import config_tables

# `config_fixture` is a helper, not a test module: discovery never imports it,
# and how this file is imported decides whether `tests/` is on the path at all.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert


def refused(case, toml):
    """`cli --report` on a target whose config is `toml`: the startup
    refusal's message, which names the config path and is never followed
    by the report."""
    target = case.locate(toml).path
    with patch.object(holophyte.cli, "report") as report, \
            case.assertRaises(SystemExit) as raised:
        holophyte.cli.cli([str(target), "--report"])
    message = str(raised.exception)
    case.assertIn(str(case.tgt.config_path), message)
    report.assert_not_called()
    return message


class LoopConfigTests(ConfigTestCase):
    """`[loop] stop_on_failure`: a boolean, defaulting to today's stop.
    `[loop] order`: `"identifier"` (the default) or `"priority"`."""

    def test_an_absent_table_stops_on_failure(self):
        self.locate()

        self.assertIs(config_tables.loop_config(self.tgt).stop_on_failure, True)

    def test_an_absent_order_is_identifier_order(self):
        self.locate()

        self.assertEqual(config_tables.loop_config(self.tgt).order, "identifier")

    def test_priority_is_read_as_priority_order(self):
        self.locate('[loop]\norder = "priority"\n')

        self.assertEqual(config_tables.loop_config(self.tgt).order, "priority")

    def test_an_unknown_order_is_a_startup_error_naming_the_key_and_values(self):
        """`"urgent"` names no sort the loop has and `1` is not a string:
        startup refuses both, naming the key and the two values it takes,
        before anything is claimed."""
        for line in ('order = "urgent"', "order = 1", 'order = "Identifier"'):
            with self.subTest(line=line):
                message = refused(self, f"[loop]\n{line}\n")
                self.assertIn("[loop]", message)
                self.assertIn("order", message)
                self.assertIn('"identifier"', message)
                self.assertIn('"priority"', message)

    def test_false_is_read_as_go_on(self):
        self.locate('[loop]\nstop_on_failure = false\n')

        self.assertIs(config_tables.loop_config(self.tgt).stop_on_failure, False)

    def test_a_non_boolean_is_a_startup_error_naming_the_key(self):
        """`"yes"` is a string, `1` an int: neither is the answer TOML's
        `true` is, and neither is quietly read as one. Startup refuses it
        for every mode, before anything is claimed."""
        for line in ('stop_on_failure = "yes"', "stop_on_failure = 1",
                     'stop_on_failure = "false"'):
            with self.subTest(line=line):
                message = refused(self, f"[loop]\n{line}\n")
                self.assertIn("[loop]", message)
                self.assertIn("stop_on_failure", message)
                self.assertIn("boolean", message)


    def test_review_round_keys_default_to_the_two_round_cap(self):
        """No table: the base is two rounds, one more per 800 changed
        lines, four at most."""
        self.locate()

        cfg = config_tables.loop_config(self.tgt)
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
                message = refused(self, f"[loop]\n{lines}\n")
                self.assertIn("[loop]", message)
                self.assertIn(key, message)

        # `0` is the documented switch for "never scale", not an error.
        self.locate("[loop]\nreview_rounds_per_lines = 0\n")
        self.assertEqual(
            config_tables.loop_config(self.tgt).review_rounds_per_lines, 0)

    def test_workers_defaults_to_one_process(self):
        self.locate()

        self.assertEqual(config_tables.loop_config(self.tgt).workers, 1)

    def test_workers_must_be_an_integer_of_at_least_one(self):
        """`"3"` is a string and `0` a pool that could work nothing: each
        is a startup error naming `[loop] workers`, before anything is
        claimed (KO-343)."""
        for line in ('workers = "3"', "workers = 0"):
            with self.subTest(line=line):
                message = refused(self, f"[loop]\n{line}\n")
                self.assertIn("[loop] workers", message)
                self.assertIn("at least 1", message)

    def test_tick_sec_defaults_to_two_minutes(self):
        self.locate()

        self.assertEqual(config_tables.loop_config(self.tgt).tick_sec, 120)

    def test_tick_sec_must_be_an_integer_of_at_least_ten(self):
        """`"120"` is a string and `5` a poll the board could not bear: each
        is a startup error naming `[loop] tick_sec` (KO-353)."""
        for line in ('tick_sec = "120"', "tick_sec = 5"):
            with self.subTest(line=line):
                message = refused(self, f"[loop]\n{line}\n")
                self.assertIn("[loop] tick_sec", message)
                self.assertIn("at least 10", message)


class RunCapTests(ConfigTestCase):
    """`[supervisor] run_cap`: the run's hard ceiling, in multiples of its
    box -- 3.0 when absent, a number from 1.5 to 5.0 when set."""

    def test_an_absent_key_is_the_default_ceiling(self):
        self.locate()

        self.assertEqual(config_tables.sweep_config(self.tgt).run_cap, 3.0)

    def test_a_cap_inside_the_range_is_read(self):
        self.locate("[supervisor]\nrun_cap = 2\n")

        self.assertEqual(config_tables.sweep_config(self.tgt).run_cap, 2)

    def test_a_cap_outside_the_range_is_a_startup_error(self):
        """1 lets a run barely turn twice; 6 is no ceiling at all. Each is
        refused at startup, for every mode, naming the key and the range --
        before anything is claimed."""
        for line in ("run_cap = 1", "run_cap = 6"):
            with self.subTest(line=line):
                message = refused(self, f"[supervisor]\n{line}\n")
                for needle in ("[supervisor]", "run_cap", "1.5", "5.0"):
                    self.assertIn(needle, message)

    def test_the_key_is_a_known_supervisor_key(self):
        """A set `run_cap` is not the unknown-key typo refusal: the config
        loads and the value is what the reader hands back."""
        target = self.locate("[supervisor]\nrun_cap = 2\n").path

        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])

        report.assert_called_once_with(self.tgt)


class BoardAskSecTests(ConfigTestCase):
    """`[supervisor] board_ask_sec`: the least wait between two asks of the
    board's ready listing -- ten minutes when absent, an integer of at
    least sixty when set (KO-434)."""

    def test_an_absent_key_is_ten_minutes(self):
        self.locate()

        self.assertEqual(config_tables.sweep_config(self.tgt).board_ask_ms,
                         600 * 1000)

    def test_a_minute_is_the_floor_and_is_read(self):
        self.locate("[supervisor]\nboard_ask_sec = 60\n")

        self.assertEqual(config_tables.sweep_config(self.tgt).board_ask_ms,
                         60 * 1000)

    def test_under_a_minute_or_not_an_integer_is_a_startup_error(self):
        """`board_ask_sec = 30` is the polling KO-434 ended, `59.5` is no
        interval a sleep can take, and `"600"` is a string: each is a
        startup error naming `[supervisor] board_ask_sec`, before anything
        is claimed."""
        for line in ("board_ask_sec = 30", "board_ask_sec = 59.5",
                     'board_ask_sec = "600"', "board_ask_sec = true"):
            with self.subTest(line=line):
                target = self.locate(f"[supervisor]\n{line}\n").path

                with patch.object(holophyte.cli, "report") as report:
                    with self.assertRaises(SystemExit) as raised:
                        holophyte.cli.cli([str(target), "--report"])

                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[supervisor] board_ask_sec", message)
                self.assertIn("at least 60", message)
                report.assert_not_called()


class ReportConfigTests(ConfigTestCase):
    """`[report] host_label`: a string shown wherever a host is rendered,
    absent by default."""

    def test_an_absent_table_is_no_label(self):
        self.locate()

        self.assertIsNone(config_tables.report_config(self.tgt).host_label)

    def test_a_string_is_read_as_the_label(self):
        self.locate('[report]\nhost_label = "writer-1"\n')

        self.assertEqual(config_tables.report_config(self.tgt).host_label,
                         "writer-1")

    def test_a_non_string_or_a_mistyped_key_is_a_startup_error_naming_it(self):
        """`3` names no writer and `hots_label` is a key nobody reads: startup
        refuses both, naming the key, before anything is claimed."""
        for line, key in (("host_label = 3", "host_label"),
                          ('host_label = ""', "host_label"),
                          ('hots_label = "x"', "hots_label")):
            with self.subTest(line=line):
                message = refused(self, f"[report]\n{line}\n")
                self.assertIn("[report]", message)
                self.assertIn(key, message)

    def test_findings_is_none_by_default_and_repo_when_opted_in(self):
        """KO-363: `[report] findings` is `none` when absent -- the store is
        the record and nothing is rendered -- and `repo` for a target that
        wants the rendered file beside its code."""
        self.locate()
        self.assertEqual(config_tables.report_config(self.tgt).findings,
                         "none")

        self.locate('[report]\nfindings = "repo"\n')
        self.assertEqual(config_tables.report_config(self.tgt).findings,
                         "repo")

    def test_a_findings_mode_nobody_defined_is_a_startup_error_naming_it(self):
        """`"yes"` is not an answer (KO-363), and neither are the modes the
        key had before it: startup fails naming `[report] findings`."""
        for line in ('findings = "yes"', 'findings = "window"',
                     'findings = "off"', "findings = false"):
            with self.subTest(line=line):
                message = refused(self, f"[report]\n{line}\n")
                self.assertIn("[report]", message)
                self.assertIn("findings", message)


class BoardLabelTests(ConfigTestCase):
    """`[board] label` (KO-432): absent is `None` and the board is every
    ready issue, as it has always been; a non-empty string is the name the
    ready listing filters on; `3` and `""` are startup errors naming the
    key -- `""` would hide the whole queue without saying so."""

    BOARD = '[board]\nproject_id = "p-1"\nteam = "T"\n'

    def test_a_label_is_read_and_reaches_the_provider_the_loop_gets(self):
        """Absent, `label` is `None`; set, `cli()` hands it to the board
        it builds -- the provider whose ready listing the label filters."""
        self.locate(self.BOARD)
        self.assertIsNone(config_tables.board_config(self.tgt).label)

        target = self.locate(self.BOARD + 'label = "holophyte"\n').path
        with patch.object(holophyte.cli, "check_agent_commands"), \
                patch.object(holophyte.cli, "check_worktree_setup"), \
                patch.object(holophyte.cli, "main") as main:
            holophyte.cli.cli([str(target)])
        self.assertEqual(main.call_args.args[1]._label, "holophyte")

    def test_a_bad_label_is_a_startup_error_naming_the_key(self):
        """`label = 3` and `label = ""` are refused where `cli()` resolves
        the board -- the loop's path; `--report` builds no board -- before
        a route is probed or a ticket is claimed."""
        for line in ("label = 3", 'label = ""'):
            with self.subTest(line=line):
                target = self.locate(self.BOARD + line + "\n").path
                with patch.object(holophyte.cli, "check_agent_commands"
                                  ) as routes, \
                        patch.object(holophyte.cli, "main") as main, \
                        self.assertRaises(SystemExit) as raised:
                    holophyte.cli.cli([str(target)])
                routes.assert_not_called()
                main.assert_not_called()
                message = str(raised.exception)
                self.assertIn(str(self.tgt.config_path), message)
                self.assertIn("[board] label", message)


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
                message = refused(self, f"[console]\n{line}\n")
                self.assertIn("[console] daemons", message)
                self.assertIn(entry, message)

    def test_an_unknown_key_is_a_startup_error(self):
        message = refused(self, "[console]\nother = 1\n")

        self.assertIn("[console]", message)
        self.assertIn("other", message)
        self.assertIn("unknown key", message)


class MergeConfigTests(ConfigTestCase):
    """Merge defaults, overrides, and startup validation."""
    def test_an_absent_table_is_auto_and_local(self):
        self.locate()

        self.assertEqual(config_tables.merge_config(self.tgt),
                         ("auto", "local", 5, "merge", 180, 300, "", "park", (),
                          ("devin-ai-integration", "coderabbitai",
                           "greptile-apps", "github-actions")))

    def test_after_is_read_as_a_list_of_commands(self):
        """`after` is the console build the daemon's bundle depends on, in
        order; absent, nothing runs after a merge (KO-347)."""
        self.locate('[merge]\nafter = ["bun --cwd=console run build",'
                    ' "sh -c true"]\n')

        self.assertEqual(config_tables.merge_config(self.tgt).after,
                         ("bun --cwd=console run build", "sh -c true"))

    def test_pr_text_is_retired_at_startup(self):
        message = refused(self, '[merge]\npr_text = "ticket"\n')
        self.assertIn("[merge] pr_text was retired: "
                      "pull request bodies are always written", message)

    def test_pr_style_is_read(self):
        self.locate('[merge]\nmode = "pr"\n'
                    'pr_style = "Title starts with [Feature Name]."\n')
        self.assertEqual(config_tables.merge_config(self.tgt).pr_style,
                         "Title starts with [Feature Name].")

    def test_human_threads_is_read(self):
        """`human_threads = "act"` lets the babysitter act on a person's
        thread; absent, it is `"park"`, KO-327's rule."""
        self.locate('[merge]\nmode = "pr"\nhuman_threads = "act"\n')

        self.assertEqual(
            config_tables.merge_config(self.tgt).human_threads, "act")

    def test_pr_merge_method_is_read(self):
        """A squash-only repository names its method; absent, it is
        `"merge"`, the merge commit the babysitter has always asked for."""
        self.locate('[merge]\nmode = "pr"\npr_merge_method = "squash"\n')

        self.assertEqual(
            config_tables.merge_config(self.tgt).pr_merge_method, "squash")

    def test_pr_poll_sec_is_read(self):
        """The least interval between two loop-started babysit rounds on
        one pull request; absent, three minutes (KO-362)."""
        self.locate('[merge]\nmode = "pr"\npr_poll_sec = 60\n')

        self.assertEqual(
            config_tables.merge_config(self.tgt).pr_poll_sec, 60)

    def test_pr_poll_sec_must_be_an_integer_of_at_least_ten(self):
        """`"180"` is a string and `5` a poll of GitHub for a reviewer's
        next keystroke: each is a startup error naming
        `[merge] pr_poll_sec` (KO-362)."""
        for line in ('pr_poll_sec = "180"', "pr_poll_sec = 5"):
            with self.subTest(line=line):
                message = refused(self, f"[merge]\nmode = \"pr\"\n{line}\n")
                self.assertIn("[merge] pr_poll_sec", message)
                self.assertIn("at least 10", message)

    def test_pr_quiet_sec_is_read(self):
        """How long a green, thread-free pull request must have stood
        before the babysitter merges it; absent, five minutes (KO-429)."""
        self.locate('[merge]\nmode = "pr"\npr_quiet_sec = 60\n')

        self.assertEqual(
            config_tables.merge_config(self.tgt).pr_quiet_sec, 60)

    def test_pr_rounds_is_read(self):
        self.locate('[merge]\nmode = "pr"\npr_rounds = 2\n')

        self.assertEqual(config_tables.merge_config(self.tgt).pr_rounds, 2)

    def test_pr_is_read(self):
        self.locate('[merge]\nmode = "pr"\n')

        self.assertEqual(config_tables.merge_config(self.tgt).mode, "pr")

    def test_human_is_read(self):
        self.locate('[merge]\napprove = "human"\n')

        self.assertEqual(config_tables.merge_config(self.tgt).approve,
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
                          ('pr_quiet_sec = "300"', "pr_quiet_sec"),
                          ("pr_quiet_sec = -1", "pr_quiet_sec"),
                          ('human_threads = "reply"', "human_threads"),
                          ('bot_authors = "x"', "bot_authors"),
                          ('bot_authors = [1]', "bot_authors"),
                          ('after = "bun run build"', "after"),
                          ("after = [1]", "after"),
                          ('approve_by = "human"', "approve_by")):
            with self.subTest(line=line):
                message = refused(self, f"[merge]\n{line}\n")
                self.assertIn("[merge]", message)
                self.assertIn(key, message)

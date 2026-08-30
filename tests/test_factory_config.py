"""Per-target config: `<repo>.holophyte.toml`, `[agents]` and `[worktree]`.

Run: python3 -m unittest discover -s tests -p 'test_factory_config*' -v
"""
import contextlib
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)


class ConfigTestCase(unittest.TestCase):
    """Retarget the module at a throwaway target, optionally with a config.

    `retarget()` is the only thing that moves TARGET/CONFIG_PATH, so the tests
    use it rather than patching the globals: a test that set the config by
    hand would pass even if the file were never wired into the retarget path
    at all.
    """

    def retarget(self, config=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(factory.retarget, factory.DEFAULT_TARGET)
        target = Path(tmp.name) / "repo"
        target.mkdir()
        if config is not None:
            (target.parent / "repo.holophyte.toml").write_text(config)
        factory.retarget(target)
        return target


class ConfigLoadingTests(ConfigTestCase):
    def test_an_absent_config_file_loads_as_empty(self):
        target = self.retarget()

        self.assertEqual(factory.CONFIG_PATH,
                         target.parent / "repo.holophyte.toml")
        self.assertFalse(factory.CONFIG_PATH.exists())
        self.assertEqual(factory.config(), {})

    def test_malformed_toml_aborts_naming_the_file_and_the_problem(self):
        self.retarget('[agents]\nimplementer = "unterminated\n')

        with self.assertRaises(SystemExit) as raised:
            factory.config()

        message = str(raised.exception)
        self.assertIn("repo.holophyte.toml", message)
        # The parser's own complaint, not just "could not read config": the
        # operator has to be told which line to go fix.
        self.assertIn("line 2", message)

    def test_the_config_is_read_for_the_target_the_command_line_names(self):
        # Not at import, and not for the default target: a broken config file
        # sitting next to some other repository is that repository's problem.
        # `cli()` reads the one the run named, before it claims anything.
        target = self.retarget()
        (target.parent / "repo.holophyte.toml").write_text("[agents\n")

        with self.assertRaises(SystemExit) as raised:
            factory.cli([str(target), "--report"])

        self.assertIn("repo.holophyte.toml", str(raised.exception))

    def test_help_does_not_read_any_config(self):
        # `--help` exits before a target is worked with at all, so a malformed
        # config for the default target cannot break it -- nor can it break
        # importing this module, which every test here already relies on.
        with patch.object(factory, "load_config",
                          side_effect=AssertionError("config read")) as load:
            with contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                factory.cli(["--help"])

        self.assertEqual(raised.exception.code, 0)
        load.assert_not_called()

    def test_unknown_tables_are_left_alone(self):
        # A config written against a later version still loads, and the table
        # this version does read keeps working beside the ones it does not.
        self.retarget('[supervisor]\nstale_heartbeat_min = 7\n\n'
                      '[agents]\nimplementer = "harness run"\n')

        self.assertEqual(factory.config()["supervisor"],
                         {"stale_heartbeat_min": 7})
        self.assertEqual(factory.agent_command("implement", "do it"),
                         ["harness", "run", "do it"])


class AgentCommandTests(ConfigTestCase):
    WORKTREE = Path("/tmp/holophyte-config-contract")

    def test_an_absent_config_leaves_todays_routes_byte_identical(self):
        self.retarget()

        with patch.object(factory.subprocess, "run") as run:
            run.return_value.stdout = "implemented"
            run.return_value.stderr = ""
            factory.agent("implement", "make the change", self.WORKTREE)
        with patch.object(factory.review_runner, "run_review") as run_review:
            run_review.return_value = "VERDICT: APPROVE"
            factory.agent("review", "review it", self.WORKTREE,
                          base_sha="1" * 40, candidate_sha="2" * 40)

        self.assertIsNone(factory.agent_command("implement", "make the change"))
        run.assert_called_once_with(
            ["claude", "-p", "make the change",
             "--model", "opus", "--effort", "high"],
            cwd=self.WORKTREE, capture_output=True, text=True, timeout=1800,
        )
        # The reviewer still goes through the hardened container, not argv.
        self.assertEqual(run_review.call_args.kwargs["profile"],
                         "codex-sol-medium")

    def test_an_implementer_override_replaces_the_argv(self):
        self.retarget('[agents]\n'
                      'implementer = "claude --model sonnet --effort medium -p"\n')

        with patch.object(factory.subprocess, "run") as run:
            run.return_value.stdout = "implemented"
            run.return_value.stderr = ""
            result = factory.agent("implement", "make the change", self.WORKTREE)

        self.assertEqual(result, "implemented")
        # The goal lands as the command's last argument — one argv element, so
        # a task title full of quotes cannot rewrite the command.
        run.assert_called_once_with(
            ["claude", "--model", "sonnet", "--effort", "medium", "-p",
             "make the change"],
            cwd=self.WORKTREE, capture_output=True, text=True, timeout=1800,
        )

    def test_a_reviewer_override_replaces_the_container_route(self):
        self.retarget('[agents]\nreviewer = "my-reviewer --diff"\n')

        with patch.object(factory.review_runner, "run_review") as run_review, \
                patch.object(factory, "publish_review_refs") as publish, \
                patch.object(factory.subprocess, "run") as run:
            run.return_value.stdout = "VERDICT: APPROVE"
            run.return_value.stderr = ""
            result = factory.agent("review", "review it", self.WORKTREE,
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
        self.assertIsNone(factory.agent_command("adjudicate", "adjudicate it"))

    def test_the_round_records_the_route_that_actually_ran_it(self):
        self.retarget('[agents]\nreviewer = "my-reviewer --diff"\n')

        with patch.object(factory.store, "record_review_round") as record:
            factory.record_round(object(), "run-1", 1, "review",
                                 "VERDICT: APPROVE", "echo ok", True, "")
            factory.record_round(object(), "run-1", 2, "adjudicate",
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
                self.retarget(config)

                with self.assertRaises(SystemExit) as raised:
                    factory.agent_command("implement", "make the change")

                self.assertIn("repo.holophyte.toml", str(raised.exception))
                self.assertIn(expected, str(raised.exception))


class StartupCheckTests(ConfigTestCase):
    """Configured routes resolve before a ticket is claimed, not mid-round.

    A round dispatches its command after the run holds the project lease and
    the worktree exists, so a name that resolves nowhere used to abandon work
    in flight. `check_agent_commands()` moves that failure to startup.
    """

    def test_a_startup_check_of_a_resolvable_command_passes(self):
        # `sh` is on PATH everywhere the factory runs; a bare name is the
        # documented normal way to write one of these.
        self.retarget('[agents]\nimplementer = "sh -c"\n'
                      f'reviewer = "{Path(sys.executable)} -c"\n')

        self.assertIsNone(factory.check_agent_commands())

    def test_an_absent_agents_table_checks_nothing(self):
        self.retarget()

        self.assertIsNone(factory.check_agent_commands())

    def test_a_program_that_is_not_on_path_is_a_startup_error(self):
        self.retarget('[agents]\nreviewer = "holophyte-no-such-reviewer --diff"\n')

        with self.assertRaises(SystemExit) as raised:
            factory.check_agent_commands()

        message = str(raised.exception)
        self.assertIn("repo.holophyte.toml", message)
        # The key the operator wrote, and the word that did not resolve.
        self.assertIn("reviewer", message)
        self.assertIn("holophyte-no-such-reviewer", message)

    def test_a_file_that_is_not_executable_is_a_startup_error(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tool = Path(tmp.name) / "tool.sh"
        tool.write_text("#!/bin/sh\nexit 0\n")
        tool.chmod(0o644)
        self.retarget(f'[agents]\nadjudicator = "{tool} --final"\n')

        with self.assertRaises(SystemExit) as raised:
            factory.check_agent_commands()

        self.assertIn("adjudicator", str(raised.exception))

    def test_a_relative_program_path_is_refused_rather_than_guessed_at(self):
        # It would resolve inside a task worktree that does not exist yet, so
        # startup cannot check the file the round would actually run.
        self.retarget('[agents]\nreviewer = "./review.sh --diff"\n')

        with self.assertRaises(SystemExit) as raised:
            factory.check_agent_commands()

        self.assertIn("relative", str(raised.exception))

    def test_the_check_reuses_the_parse_a_round_would_use(self):
        # An unquotable or wrongly-typed command is caught here too, with the
        # same message a round would have raised -- one parser, not two.
        self.retarget('[agents]\nimplementer = ["claude", "-p"]\n')

        with self.assertRaises(SystemExit) as raised:
            factory.check_agent_commands()

        self.assertIn("command string", str(raised.exception))

    def test_a_run_checks_its_commands_before_claiming_anything(self):
        target = self.retarget(
            '[agents]\nimplementer = "holophyte-no-such-harness -p"\n')

        with patch.object(factory, "main",
                          side_effect=AssertionError("claimed work")) as main:
            with self.assertRaises(SystemExit) as raised:
                factory.cli([str(target)])

        self.assertIn("holophyte-no-such-harness", str(raised.exception))
        main.assert_not_called()

    def test_report_does_not_require_the_configured_commands(self):
        # `--report` reads the store and calls nobody, so a reviewer that is
        # not installed on the machine reading the table is not its problem.
        target = self.retarget(
            '[agents]\nreviewer = "holophyte-no-such-reviewer --diff"\n')

        with patch.object(factory, "report") as report:
            factory.cli([str(target), "--report"])

        report.assert_called_once_with()


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
        self.retarget()

        with patch.object(factory, "run_verify",
                          side_effect=AssertionError("ran a setup command")):
            self.assertEqual(factory.run_worktree_setup(self.worktree()),
                             (True, ""))
        self.assertEqual(factory.setup_commands(), [])

    def test_the_commands_run_in_the_worktree_in_the_order_written(self):
        wt = self.worktree()
        self.retarget('[worktree]\nsetup = ["pwd > where.txt", '
                      '"cp where.txt copied.txt"]\n')

        ok, report = factory.run_worktree_setup(wt)

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
        self.retarget('[worktree]\nsetup = ["echo building; exit 3", '
                      '"touch never.txt"]\n')

        ok, report = factory.run_worktree_setup(wt)

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
        self.retarget('[worktree]\nsetup = ["echo first && false && echo third"]\n')

        ok, report = factory.run_worktree_setup(wt)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3", report)
        self.assertIn("failing clause: false", report)
        self.assertIn("not executed: clause 3", report)

    def test_a_silent_failure_is_reported_as_silence(self):
        wt = self.worktree()
        self.retarget('[worktree]\nsetup = ["exit 1"]\n')

        ok, report = factory.run_worktree_setup(wt)

        self.assertFalse(ok)
        self.assertIn("failed silently", report)

    def test_the_setup_records_a_phase_before_it_runs_anything(self):
        wt = self.worktree()
        self.retarget('[worktree]\nsetup = ["true"]\n')
        conn = object()

        with patch.object(factory.store, "set_phase") as set_phase:
            factory.run_worktree_setup(wt, conn, "run-1")

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
                self.retarget(config)

                with self.assertRaises(SystemExit) as raised:
                    factory.setup_commands()

                self.assertIn("repo.holophyte.toml", str(raised.exception))
                self.assertIn(expected, str(raised.exception))

    def test_the_startup_check_reuses_the_parse_a_run_would_use(self):
        self.retarget('[worktree]\nsetup = [7]\n')

        with self.assertRaises(SystemExit) as raised:
            factory.check_worktree_setup()

        self.assertIn("command string", str(raised.exception))

    def test_a_startup_check_of_a_usable_table_passes(self):
        # Startup settles the shape of the table and deliberately not the
        # commands: they are shell, written against a worktree that does not
        # exist yet.
        self.retarget('[worktree]\nsetup = ["holophyte-no-such-tool --install"]\n')

        self.assertIsNone(factory.check_worktree_setup())

    def test_an_absent_table_checks_nothing(self):
        self.retarget()

        self.assertIsNone(factory.check_worktree_setup())

    def test_a_run_checks_the_table_before_claiming_anything(self):
        target = self.retarget('[worktree]\nsetup = "make deps"\n')

        with patch.object(factory, "main",
                          side_effect=AssertionError("claimed work")) as main:
            with self.assertRaises(SystemExit) as raised:
                factory.cli([str(target)])

        self.assertIn("must be a list", str(raised.exception))
        main.assert_not_called()

    def test_report_does_not_read_the_setup_table(self):
        # `--report` cuts no worktree, so a table it would never run is not
        # that reading's problem.
        target = self.retarget('[worktree]\nsetup = [7]\n')

        with patch.object(factory, "report") as report:
            factory.cli([str(target), "--report"])

        report.assert_called_once_with()


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
        self.retarget(f'[agents]\nreviewer = "{reviewer}"\n')

        reply = factory.agent("review", "review it", root,
                              base_sha=self.base, candidate_sha=self.head)

        # What the command printed is the pair the round is about, read out of
        # the repo it ran in -- not a ref that does not exist there.
        self.assertEqual(reply.split(), [self.base, self.head])

    def test_a_sha_the_repo_does_not_have_is_refused(self):
        root = self.repo()
        self.retarget('[agents]\nreviewer = "true"\n')

        with self.assertRaises(factory.review_runner.ReviewBoundaryError):
            factory.agent("review", "review it", root,
                          base_sha=self.base, candidate_sha="0" * 40)

        # Nothing was published: a refused round leaves no ref claiming a
        # candidate the repo never had.
        self.assertEqual(
            subprocess.run(["git", "rev-parse", "--verify", "-q",
                            "refs/review/candidate"], cwd=root).returncode, 1)

    def test_a_base_that_is_not_an_ancestor_is_refused(self):
        root = self.repo()
        self.retarget('[agents]\nadjudicator = "true"\n')
        self.git(root, "checkout", "-q", "--orphan", "sideways")
        self.git(root, "rm", "-rqf", ".")
        unrelated = self.commit(root, "unrelated.txt")

        with self.assertRaises(factory.review_runner.ReviewBoundaryError):
            factory.agent("adjudicate", "judge it", root,
                          base_sha=unrelated, candidate_sha=self.head)

    def test_the_default_route_is_left_to_stage_its_own_refs(self):
        # The container route builds its own checkout and names the refs
        # there; the task worktree is not where its reviewer looks.
        self.retarget()
        root = self.repo()

        with patch.object(factory, "publish_review_refs") as publish, \
                patch.object(factory.review_runner, "run_review") as run_review:
            run_review.return_value = "VERDICT: APPROVE"
            factory.agent("review", "review it", root,
                          base_sha=self.base, candidate_sha=self.head)

        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()

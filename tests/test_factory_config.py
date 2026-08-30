"""Per-target config: `<repo>.holophyte.toml` and its `[agents]` command table.

Run: python3 -m unittest discover -s tests -p 'test_factory_config*' -v
"""
import importlib.util
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

    `retarget()` is the only thing that moves TARGET/CONFIG, so the tests use
    it rather than patching the globals: a test that set CONFIG by hand would
    pass even if the config were never wired into the retarget path at all.
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
        self.assertEqual(factory.CONFIG, {})

    def test_malformed_toml_aborts_naming_the_file_and_the_problem(self):
        with self.assertRaises(SystemExit) as raised:
            self.retarget('[agents]\nimplementer = "unterminated\n')

        message = str(raised.exception)
        self.assertIn("repo.holophyte.toml", message)
        # The parser's own complaint, not just "could not read config": the
        # operator has to be told which line to go fix.
        self.assertIn("line 2", message)

    def test_unknown_tables_are_left_alone(self):
        # A config written against a later version still loads, and the table
        # this version does read keeps working beside the ones it does not.
        self.retarget('[supervisor]\nstale_heartbeat_min = 7\n\n'
                      '[agents]\nimplementer = "harness run"\n')

        self.assertEqual(factory.CONFIG["supervisor"],
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
                patch.object(factory.subprocess, "run") as run:
            run.return_value.stdout = "VERDICT: APPROVE"
            run.return_value.stderr = ""
            result = factory.agent("review", "review it", self.WORKTREE,
                                   base_sha="1" * 40, candidate_sha="2" * 40)

        self.assertEqual(result, "VERDICT: APPROVE")
        run_review.assert_not_called()
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


if __name__ == "__main__":
    unittest.main()

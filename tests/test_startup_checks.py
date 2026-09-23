"""Startup checks: configured routes resolve before a ticket is claimed.

Run: python3 -m unittest discover -s tests -p 'test_startup_checks*' -v
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

import holophyte.cli
import holophyte.config
import holophyte.pr
import holophyte.project
import holophyte.supervisor_lock
import review_runner
from provider import LinearProvider

# `config_fixture` is a helper, not a test module: discovery never imports it,
# and how this file is imported decides whether `tests/` is on the path at all.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert


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
            holophyte.config.check_agent_commands(self.project)

        message = str(raised.exception)
        self.assertIn("[merge] mode", message)
        self.assertIn("origin", message)
        self.assertIn(str(self.project.config_path), message)

    def test_pr_mode_probes_gh_auth_and_refuses_a_failed_one(self):
        """With `origin` in place the route is `gh auth status`: a stub that
        answers is a pass, one that refuses is a startup error naming `gh`;
        with no `gh` and no token in the environment, the error names both."""
        bindir = self.stub_path()
        self.locate('[merge]\nmode = "pr"\n')
        self.repo(origin=True)
        self.stub(bindir / "gh", 'if [ "$1" = auth ]; then exit 0; fi\nexit 1\n')
        self.assertIsNone(holophyte.config.check_agent_commands(self.project))

        self.stub(bindir / "gh", 'echo "You are not logged into any GitHub '
                                 'hosts" >&2\nexit 1\n')
        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.project)
        self.assertIn("gh auth status", str(raised.exception))
        self.assertIn("not logged into", str(raised.exception))

        # No `gh` at all: the program is renamed to one nothing on PATH
        # answers to, since the host running this may well have a real,
        # authenticated `gh` in a system directory the stubs sit ahead of.
        with patch.object(holophyte.pr, "GH", "gh-absent-under-test"), \
                patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}):
            with self.assertRaises(SystemExit) as raised:
                holophyte.config.check_agent_commands(self.project)
            self.assertIn("'gh-absent-under-test' on PATH",
                          str(raised.exception))
            self.assertIn("GH_TOKEN or GITHUB_TOKEN", str(raised.exception))
            with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_test"}):
                self.assertIsNone(
                    holophyte.config.check_agent_commands(self.project))

    def test_a_startup_check_of_a_resolvable_command_passes(self):
        # `sh` is on PATH everywhere the factory runs; a bare name is the
        # documented normal way to write one of these.
        self.locate('[agents]\nimplementer = "sh -c"\n'
                      f'reviewer = "{Path(sys.executable)} -c"\n')

        self.assertIsNone(holophyte.config.check_agent_commands(self.project))

    def test_an_absent_agents_table_passes_when_the_default_routes_answer(self):
        self.locate()

        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertIsNone(holophyte.config.check_agent_commands(self.project))

        # A built image is nothing to remark on.
        self.assertEqual(printed.getvalue(), "")

    def test_an_unbuilt_review_image_is_reported_not_refused(self):
        # The runner builds the image on the first review that finds it
        # missing, so a fresh host is told what to expect and proceeds.
        self.stub_path(image=False)
        self.locate()

        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            self.assertIsNone(holophyte.config.check_agent_commands(self.project))

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
        self.assertEqual(holophyte.config.review_route(self.project),
                         ("gpt-6-astra", "xhigh"))
        self.assertIsNone(holophyte.config.check_agent_commands(self.project))

        self.locate()
        self.assertEqual(holophyte.config.review_route(self.project),
                         ("gpt-5.6-sol", "medium"))

    def test_a_missing_docker_is_a_startup_error_naming_the_override_key(self):
        self.stub_path(docker=None, system=False)
        self.locate()

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.project)

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
                holophyte.config.check_agent_commands(self.project)

        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("did not answer `docker info` within 1s",
                      str(raised.exception))

    def test_a_configured_reviewer_still_probes_docker_for_the_adjudicator(self):
        # The adjudicator is its own key and falls to the container route
        # when only the reviewer is overridden.
        self.stub_path(docker="down")
        self.locate('[agents]\nreviewer = "sh -c"\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.project)

        message = str(raised.exception)
        self.assertIn("[agents] adjudicator not set", message)
        self.assertNotIn("reviewer and adjudicator", message)

    def test_a_configured_route_is_resolved_and_not_probed(self):
        # A docker that is down is not the problem of a target that routes
        # every role somewhere else.
        self.stub_path(claude=False, docker="down")
        self.locate('[agents]\nimplementer = "sh -c"\n'
                      'reviewer = "sh -c"\nadjudicator = "sh -c"\n')

        self.assertIsNone(holophyte.config.check_agent_commands(self.project))

    def test_a_program_that_is_not_on_path_is_a_startup_error(self):
        self.locate('[agents]\nreviewer = "holophyte-no-such-reviewer --diff"\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.project)

        message = str(raised.exception)
        self.assertIn(str(self.project.config_path), message)
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
            holophyte.config.check_agent_commands(self.project)

        self.assertIn("adjudicator", str(raised.exception))

    def test_a_relative_program_path_is_refused_rather_than_guessed_at(self):
        # It would resolve inside a task worktree that does not exist yet, so
        # startup cannot check the file the round would actually run.
        self.locate('[agents]\nreviewer = "./review.sh --diff"\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.project)

        self.assertIn("relative", str(raised.exception))

    def test_the_check_reuses_the_parse_a_round_would_use(self):
        # An unquotable or wrongly-typed command is caught here too, with the
        # same message a round would have raised -- one parser, not two.
        self.locate('[agents]\nimplementer = ["claude", "-p"]\n')

        with self.assertRaises(SystemExit) as raised:
            holophyte.config.check_agent_commands(self.project)

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

        report.assert_called_once_with(self.project)

    def test_read_only_modes_do_not_probe_the_default_routes(self):
        # `--report` and `--sweep` dispatch nobody, so a host with no `claude`
        # and Docker stopped can still read the store.
        self.stub_path(claude=False, docker="down", system=False)
        target = self.locate('[board]\nproject_id = "p-1"\nteam = "T"\n').path

        with patch.object(holophyte.cli, "report") as report:
            holophyte.cli.cli([str(target), "--report"])
        with patch.object(holophyte.cli, "sweep_report") as sweep_report:
            holophyte.cli.cli([str(target), "--sweep"])

        report.assert_called_once_with(self.project)
        # The board is handed down from `cli()`, never reached for by name;
        # building it reads no config and opens no connection.
        sweep_report.assert_called_once_with(self.project, act=False, provider=ANY)
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
        self.assertIn(str(self.project.config_path), str(raised.exception))
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

        sweep_report.assert_called_once_with(self.project, act=False, provider=None)

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

        def __init__(self, project_id, team, label=None):
            self.team = team

        def ready_issues(self):
            return []

        def claim_next(self, skip=(), order="identifier"):
            return None

    def start_loop(self, target):
        import store

        located = holophyte.project.Project.locate(target)
        located.store_path.parent.mkdir(parents=True, exist_ok=True)
        store.open(located.store_path, migrate="owner").close()
        printed = io.StringIO()
        with patch.object(holophyte.cli, "LinearProvider", self.EmptyBoard), \
                contextlib.redirect_stdout(printed):
            holophyte.cli.cli([str(target)])
        out = printed.getvalue()
        self.assertIn("[holo2] Linear has no ready tickets. done.", out)
        return out

    def hold_lock(self, pid):
        lock = holophyte.supervisor_lock.supervisor_lock_path(self.project)
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
        log = self.project.holo_dir / "supervisor.log"
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


if __name__ == "__main__":
    unittest.main()

"""The main watch in act mode: the host sweep leases a firing and starts a
real `factory.py --automation-firing` worker, whose GitHub calls go through
the factory's `gh api` path to a `gh` on PATH that logs each method and path.

Run: python3 -m unittest discover -s tests -p 'test_automation_act.py' -v
"""
import contextlib
import json
import os
import stat
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.automation.step as step
import store.automations
from holophyte.config.project import Project
from holophyte.host.registry import HOST_FILE
from holophyte.isolation import launcher
from tests.host_fixture import git
from tests.main_watch_fixture import job, sweep_pass
from tests.test_automation_switches import SwitchFixture
from tests.test_sweep_host import MINUTE, T0

S = "5e1f0c2a9b7d4e3f8a6c1b0d2e4f6a8c0b1d3e5f"
FACTORY = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "factory.py")

GH = """#!PYTHON
import json, os, sys
from pathlib import Path
home = Path(os.environ["ACT_GH_DIR"])
args = sys.argv[1:]
method = args[args.index("--method") + 1] if "--method" in args else "GET"
path, name = args[-1].split("?")[0], Path.cwd().name
with open(home / "calls.log", "a") as log:
    log.write(f"{name} {method} {path}\\n")
answers = home / f"{name}.json"
table = json.loads(answers.read_text()) if answers.exists() else {}
for written, text in table.get(f"WRITE BEFORE {method} {path}", {}).items():
    Path(written).write_text(text)
exec(table.get(f"EXEC BEFORE {method} {path}", ""))
found = table.get(f"{method} {path}")
if found is None:
    sys.exit(print("gh: Not Found (HTTP 404)", file=sys.stderr) or 1)
print(json.dumps(found))
"""


class ActGitHub:
    """Answers `METHOD PATH` per checkout directory; logs `DIRECTORY METHOD PATH`.
    `WRITE BEFORE METHOD PATH` maps files to the text written as that call
    arrives, and `EXEC BEFORE METHOD PATH` is Python run then: a change made
    while the call is in flight."""

    def __init__(self, case, directory):
        self.dir = directory
        self.dir.mkdir()
        script = self.dir / "gh"
        script.write_text(GH.replace("PYTHON", sys.executable))
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        environ = patch.dict(os.environ, {
            "ACT_GH_DIR": str(self.dir),
            "PATH": f"{self.dir}{os.pathsep}{os.environ['PATH']}"})
        environ.start()
        case.addCleanup(environ.stop)

    def red(self, path, repo, run=7, attempt=1):
        base = f"repos/{repo}"
        answers = {
            f"GET {base}/commits/main": {"sha": S},
            f"GET {base}/commits/{S}/check-runs": {"total_count": 2,
                                                   "check_runs": [
                job("unit", "failure", 11, run),
                job("docs", "success", 12, run)]},
            f"GET {base}/rules/branches/main": [{
                "type": "required_status_checks", "parameters": {
                    "required_status_checks": [{"context": "unit"}]}}],
            f"GET {base}/branches/main": {"name": "main"},
            f"GET {base}/actions/runs/{run}": {"id": run,
                                               "run_attempt": attempt},
            f"POST {base}/actions/runs/{run}/rerun-failed-jobs": {},
        }
        (self.dir / f"{path.name}.json").write_text(json.dumps(answers))

    def calls(self, path, method=None):
        log = self.dir / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line.split(" ", 1)[1] for line in lines
                if line.split(" ")[0] == path.name
                and (method is None or line.split(" ")[1] == method)]

    def answer(self, path, key, value):
        answers = self.dir / f"{path.name}.json"
        table = json.loads(answers.read_text())
        if value is None:
            table.pop(key)
        else:
            table[key] = value
        answers.write_text(json.dumps(table))

    def writes(self, path):
        return [call for call in self.calls(path)
                if not call.startswith("GET ")]


class ActFixture(SwitchFixture):
    def setUp(self):
        super().setUp()
        self.act_github = ActGitHub(self, self.root / "act-gh")
        self.repos = {name: f"o/{name}" for name in self.NAMES}

    def origin(self, name, repo):
        self.repos[name] = repo
        git(self.paths[name], "remote", "set-url", "origin",
            f"https://github.com/{repo}.git")

    def act(self, name, revision=1, run=7, attempt=1):
        config = Project.locate(self.paths[name], adopt=False).config_path
        with config.open("a") as handle:
            handle.write('\n[automations.main_watch]\nmode = "act"\n'
                         f"revision = {revision}\n")
        self.act_github.red(self.paths[name], self.repos[name], run, attempt)

    def rerun_posts(self, name, run=7):
        return [call for call in self.act_github.calls(self.paths[name],
                                                       "POST")
                if call.endswith(f"/actions/runs/{run}/rerun-failed-jobs")]

    def sweep(self, spawn):
        """One pass at the wall clock, its workers started through `spawn`."""
        with patch.object(step, "SPAWN", spawn), \
                patch.object(step, "systemd_runner", lambda: None):
            return sweep_pass(int(time.time() * 1000))

    def recorded_pass(self):
        """One pass whose `SPAWN` records each worker's argv and starts none."""
        argvs = []

        def spawn(argv, **_kwargs):
            argvs.append(argv)
            return SimpleNamespace(pid=0)
        self.sweep(spawn)
        return argvs

    def started_pass(self):
        """One pass that really starts its workers; it waits for them to end."""
        children = []

        def spawn(argv, **kwargs):
            children.append(subprocess.Popen(argv, **kwargs))
            return children[-1]
        self.sweep(spawn)
        return [child.wait(timeout=120) for child in children]

    def worker(self, name, firing_id):
        return subprocess.run(
            [sys.executable, FACTORY, "--automation-firing", str(firing_id),
             str(self.paths[name])], capture_output=True, text=True,
            timeout=120)

    def only(self, name):
        [firing] = self.firings(name)
        return firing

    def leased(self, action, revision=1):
        conn = self.conn("alpha")
        project = conn.execute("SELECT id FROM projects").fetchone()[0]
        now = int(time.time() * 1000)
        return store.automations.reserve_firing(
            conn, project, "main_watch", revision, "main", f"{action} cause",
            mode="act", reason="r", detail={
                "action": action, "sha": S, "workflow_run_id": 7,
                "run_attempt": 1}, now=now, lease_until=now + 60_000)


class ActTests(ActFixture):
    def test_an_act_firing_reruns_main_once(self):
        self.act("alpha")

        self.assertEqual(self.started_pass(), [0])

        self.assertEqual(self.rerun_posts("alpha"),
                         ["POST repos/o/alpha/actions/runs/7/rerun-failed-jobs"])
        firing = self.only("alpha")
        self.assertEqual((firing.mode, firing.state), ("act", "confirmed"))
        self.assertEqual(firing.detail["rerun_attempt"], 2)

        self.assertEqual(self.recorded_pass(), [])
        self.assertEqual(self.recorded_pass(), [])

        self.assertEqual(self.firings("alpha"), [firing])
        self.assertEqual(len(self.rerun_posts("alpha")), 1)

    def test_the_sweep_spawns_and_one_worker_takes_the_firing(self):
        self.act("alpha")

        [argv] = self.recorded_pass()

        firing = self.only("alpha")
        self.assertEqual(firing.state, "leased")
        self.assertIsNotNone(firing.leaseUntil)
        self.assertIsNone(firing.workerPid)
        flag = argv.index("--automation-firing")
        self.assertEqual(argv[flag + 1], str(firing.id))
        workers = [subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
                   for _ in range(2)]
        self.assertEqual([child.wait(timeout=120) for child in workers],
                         [0, 0])
        self.assertEqual(len(self.rerun_posts("alpha")), 1)
        self.assertEqual(self.only("alpha").state, "confirmed")

    def test_an_unapproved_revision_stays_in_dry_run(self):
        self.act("alpha", revision=0)

        self.assertEqual(self.recorded_pass(), [])

        firing = self.only("alpha")
        self.assertEqual((firing.state, firing.mode), ("proposed", "dry_run"))
        self.assertIn("rule revision 1 is not approved ([automations.main_watch]"
                      " revision = 0)", firing.reason)
        self.assertEqual(self.act_github.writes(self.paths["alpha"]), [])

    def test_a_stop_after_the_lease_cancels_the_dispatch(self):
        for name in self.NAMES:
            self.act(name)
        self.assertEqual(len(self.recorded_pass()), 2)

        self.assertEqual(self.holo("automations", "off", "stop")[0], 0)
        alpha = self.worker("alpha", self.only("alpha").id)
        self.assertEqual(self.holo("automations", "on", "go")[0], 0)
        self.assertEqual(self.holo("automation", "pause", "main_watch",
                                   "stop", "-p", "beta")[0], 0)
        beta = self.worker("beta", self.only("beta").id)

        for name, ran in (("alpha", alpha), ("beta", beta)):
            self.assertEqual(ran.returncode, 0, ran.stderr)
            firing = self.only(name)
            self.assertEqual(firing.state, "canceled")
            self.assertIn("stop", firing.reason)
            self.assertEqual(self.act_github.writes(self.paths[name]), [])
        self.assertIn("off on this host", self.only("alpha").reason)
        self.assertIn("paused", self.only("beta").reason)

    def test_a_stop_during_a_failed_attempt_read_cancels_the_firing(self):
        self.act("alpha")
        self.recorded_pass()
        read = "GET repos/o/alpha/actions/runs/7"
        self.act_github.answer(self.paths["alpha"], read, None)
        self.act_github.answer(
            self.paths["alpha"], f"WRITE BEFORE {read}",
            {str(self.home / "automations.json"):
             json.dumps({"off": True, "note": "stop"})})

        ran = self.worker("alpha", self.only("alpha").id)

        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(self.rerun_posts("alpha"), [])
        firing = self.only("alpha")
        self.assertEqual(firing.state, "canceled")
        self.assertIn("stop", firing.reason)

    def test_no_pause_commits_between_the_final_recheck_and_the_settle(self):
        self.act("alpha")
        self.recorded_pass()
        outcome = self.root / "pause-during-rerun"
        self.act_github.answer(
            self.paths["alpha"],
            "EXEC BEFORE POST repos/o/alpha/actions/runs/7/rerun-failed-jobs",
            "import sqlite3\n"
            f"conn = sqlite3.connect({str(self.target('alpha').store_path)!r},"
            " timeout=0.5)\n"
            "try:\n"
            "    conn.execute(\"INSERT INTO automationPauses (automation,"
            " note, actor, createdAt) VALUES ('main_watch', 'stop',"
            " 'operator', 0)\")\n"
            "    conn.commit()\n"
            f"    Path({str(outcome)!r}).write_text('committed')\n"
            "except sqlite3.OperationalError as bad:\n"
            f"    Path({str(outcome)!r}).write_text(str(bad))\n")

        ran = self.worker("alpha", self.only("alpha").id)

        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(outcome.read_text(), "database is locked")
        self.assertEqual(self.only("alpha").state, "confirmed")
        self.assertEqual(self.pauses("alpha"), [])

    def test_a_failed_later_read_still_starts_the_leased_firing(self):
        self.act("alpha")
        repo, path = "repos/o/alpha", self.paths["alpha"]
        self.act_github.answer(path, f"GET {repo}/commits/{S}/check-runs", {
            "total_count": 2, "check_runs": [
                job("unit", "failure", 11, 7), job("lint", "failure", 21, 8)]})
        self.act_github.answer(path, f"GET {repo}/rules/branches/main", [{
            "type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "unit"},
                                           {"context": "lint"}]}}])

        [argv] = self.recorded_pass()

        [firing] = [row for row in self.firings("alpha") if row.state == "leased"]
        self.assertEqual(firing.detail["workflow_run_id"], 7)
        self.assertEqual(argv[argv.index("--automation-firing") + 1],
                         str(firing.id))

    def test_a_worker_that_fails_to_start_does_not_stop_the_next(self):
        self.act("alpha")
        repo, path = "repos/o/alpha", self.paths["alpha"]
        self.act_github.answer(path, f"GET {repo}/commits/{S}/check-runs", {
            "total_count": 2, "check_runs": [
                job("unit", "failure", 11, 7), job("lint", "failure", 21, 8)]})
        self.act_github.answer(path, f"GET {repo}/rules/branches/main", [{
            "type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "unit"},
                                           {"context": "lint"}]}}])
        self.act_github.answer(path, f"GET {repo}/actions/runs/8",
                               {"id": 8, "run_attempt": 1})
        argvs = []

        def spawn(argv, **_kwargs):
            argvs.append(argv)
            if len(argvs) == 1:
                raise OSError("no fork")
            return SimpleNamespace(pid=0)

        out = self.sweep(spawn)

        firings = {row.id: row for row in self.firings("alpha")}
        started = [int(argv[argv.index("--automation-firing") + 1])
                   for argv in argvs]
        self.assertEqual(sorted(started), sorted(firings))
        self.assertEqual(len(started), 2)
        self.assertIn(f"firing {started[0]} leased, its worker did not start:"
                      " no fork", out)
        self.assertIn(f"firing {started[1]} leased to a worker", out)

    def test_a_rerun_a_person_made_first_is_refused_cleanly(self):
        self.act("alpha")
        self.recorded_pass()
        self.act_github.red(self.paths["alpha"], "o/alpha", attempt=2)

        ran = self.worker("alpha", self.only("alpha").id)

        self.assertEqual(ran.returncode, 0, ran.stderr)
        firing = self.only("alpha")
        self.assertEqual(firing.state, "refused")
        self.assertIn("attempt 2", firing.reason)
        self.assertEqual(self.rerun_posts("alpha"), [])

    def test_a_report_only_repository_is_matched_by_origin_not_name(self):
        with open(self.home / HOST_FILE, "a") as registry:
            registry.write('\n[automations]\nreport_only = '
                           '["Example/Report-Only"]\n')
        self.origin("alpha", "example/report-only")
        self.origin("beta", "example/repo")
        for name in self.NAMES:
            self.act(name)

        [argv] = self.recorded_pass()

        alpha = self.only("alpha")
        self.assertEqual((alpha.state, alpha.mode), ("proposed", "dry_run"))
        self.assertIn("report-only repository example/report-only",
                      alpha.reason)
        self.assertEqual(argv[-1], str(self.paths["beta"]))
        self.assertEqual(argv[argv.index("--automation-firing") + 1],
                         str(self.only("beta").id))

    def test_a_dry_run_proposal_is_leased_once_act_is_allowed(self):
        host = self.home / HOST_FILE
        listing = '\n[automations]\nreport_only = ["o/beta"]\n'
        host.write_text(host.read_text() + listing)
        self.act("alpha", revision=0)
        self.act("beta")
        self.assertEqual(self.recorded_pass(), [])
        proposed = {name: self.only(name) for name in self.NAMES}
        self.assertEqual({row.state for row in proposed.values()},
                         {"proposed"})

        config = Project.locate(self.paths["alpha"], adopt=False).config_path
        config.write_text(config.read_text().replace("revision = 0",
                                                     "revision = 1"))
        host.write_text(host.read_text().replace(listing, ""))
        argvs = self.recorded_pass()

        for name in self.NAMES:
            firing = self.only(name)
            self.assertEqual((firing.id, firing.mode, firing.state),
                             (proposed[name].id, "act", "leased"))
            self.assertEqual(firing.detail["run_attempt"], 1)
        self.assertEqual(
            sorted(argv[argv.index("--automation-firing") + 1]
                   for argv in argvs),
            sorted(str(proposed[name].id) for name in self.NAMES))
        self.assertEqual(self.recorded_pass(), [])

    def test_a_repository_listed_after_the_lease_is_not_rerun(self):
        self.act("alpha")
        self.recorded_pass()
        with open(self.home / HOST_FILE, "a") as registry:
            registry.write('\n[automations]\nreport_only = ["O/Alpha"]\n')

        ran = self.worker("alpha", self.only("alpha").id)

        self.assertEqual(ran.returncode, 0, ran.stderr)
        firing = self.only("alpha")
        self.assertEqual(firing.state, "canceled")
        self.assertIn("report-only repository o/alpha", firing.reason)
        self.assertEqual(self.act_github.writes(self.paths["alpha"]), [])


class PolicyTests(ActFixture):
    def test_an_action_outside_the_policy_is_refused_before_github(self):
        self.act("alpha")
        refused = {}
        for action in ("merge", "move_ready"):
            firing_id = self.leased(action)
            ran = self.worker("alpha", firing_id)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            refused[action] = store.automations.firing(self.conn("alpha"),
                                                       firing_id)

        self.assertEqual({row.state for row in refused.values()}, {"refused"})
        self.assertIn("NEVER", refused["merge"].reason)
        self.assertIn("main_watch's list", refused["move_ready"].reason)
        self.assertEqual(self.act_github.calls(self.paths["alpha"]), [])


class StaleRuleTests(ActFixture):
    def test_a_firing_from_a_past_rule_revision_is_canceled(self):
        """The maintainer still approves revision 0, the firing's, but the
        running rule is at revision 1, which no one approved."""
        self.act("alpha", revision=0)
        firing_id = self.leased("rerun_failed_jobs", revision=0)

        ran = self.worker("alpha", firing_id)

        self.assertEqual(ran.returncode, 0, ran.stderr)
        firing = store.automations.firing(self.conn("alpha"), firing_id)
        self.assertEqual(firing.state, "canceled")
        self.assertIn("rule revision 1", firing.reason)
        self.assertEqual(self.act_github.calls(self.paths["alpha"]), [])

    def test_a_config_that_no_longer_reads_cancels_the_firing(self):
        self.act("alpha")
        firing_id = self.leased("rerun_failed_jobs")
        config = Project.locate(self.paths["alpha"], adopt=False).config_path
        config.write_text(config.read_text().replace("revision = 1\n", ""))

        ran = self.worker("alpha", firing_id)

        self.assertEqual(ran.returncode, 0, ran.stderr)
        firing = store.automations.firing(self.conn("alpha"), firing_id)
        self.assertEqual(firing.state, "canceled")
        self.assertIn("needs revision", firing.reason)
        self.assertEqual(self.act_github.calls(self.paths["alpha"]), [])


HOUR = 60 * MINUTE
FAILING = "for i in $(seq 1 80); do echo smoke line $i; done; exit 3"
CREDENTIAL = "CLAUDE_CODE_OAUTH_TOKEN"
SETUP = "pip install -e ."


class SmokeTests(ActFixture):
    def smoke(self, name, mode, command, every_hours=6):
        """A native board, `[automations.smoke]`, and a local bare origin
        whose main is one commit; that commit's sha."""
        path = self.paths[name]
        self.target(name).config_path.write_text(
            f'[board]\nkind = "native"\nteam = "team-{name}"\n'
            f'prefix = "SM"\n\n[automations.smoke]\nmode = "{mode}"\n'
            f"revision = 1\ncommand = {json.dumps(command)}\n"
            f"every_hours = {every_hours}\n")
        origin = self.root / f"{name}-origin.git"
        git(self.root, "init", "-q", "--bare", str(origin))
        (path / "README").write_text("smoke\n")
        git(path, "add", "README")
        git(path, "commit", "-q", "-m", "main")
        git(path, "push", "-q", str(origin), "HEAD:main")
        git(path, "remote", "set-url", "origin", str(origin))
        return git(path, "rev-parse", "HEAD")

    def smoke_firings(self, name):
        return [firing for firing in store.automations.firings(self.conn(name))
                if firing.automation == "smoke"]

    def contained(self, name):
        """The implementer in a container that needs `CREDENTIAL`, and a
        worktree setup that runs there."""
        with self.target(name).config_path.open("a") as handle:
            handle.write('\n[agents]\nimplementer_isolation = "container"\n'
                         f'implementer_credential = {{ env = "{CREDENTIAL}" }}'
                         f"\n\n[worktree]\nsetup = [{json.dumps(SETUP)}]\n")
        git(self.paths[name], "config", "user.name", "Smoke")
        git(self.paths[name], "config", "user.email", "smoke@example.com")

    def uncredentialed(self, image=None):
        """No `CREDENTIAL` in the environment; `docker run` succeeds, and
        the image check raises `image` when one is given."""
        stack = contextlib.ExitStack()
        stack.enter_context(patch.dict(os.environ))
        os.environ.pop(CREDENTIAL, None)
        stack.enter_context(patch.object(launcher, "image_ready",
                                         side_effect=image))
        stack.enter_context(patch.object(launcher.review_runner,
                                         "_remove_container"))
        run = stack.enter_context(patch.object(launcher, "run_capped",
                                               return_value=(0, "installed")))
        return stack, run

    def filed(self, name):
        return self.conn(name).execute(
            "SELECT linearIdentifier, title, boardColumn, body FROM tickets"
            " ORDER BY id").fetchall()

    def test_a_failing_smoke_check_in_act_files_one_todo_ticket(self):
        sha = self.smoke("alpha", "act", FAILING)

        sweep_pass(T0)

        [(key, title, column, body)] = self.filed("alpha")
        self.assertEqual((title, column),
                         (f"Smoke check fails on main at {sha[:12]}", "ready"))
        lines = body.splitlines()
        self.assertIn("> smoke line 80", lines)
        self.assertIn("> smoke line 21", lines)
        self.assertNotIn("> smoke line 20", lines)
        self.assertIn("exited with status 3", body)
        [firing] = self.smoke_firings("alpha")
        self.assertEqual((firing.mode, firing.state, firing.detail["ticket"]),
                         ("act", "confirmed", key))

    def test_a_secret_in_the_command_stays_off_the_board(self):
        self.smoke("alpha", "act", "API_TOKEN=smoke-secret-value sh -c"
                   " 'echo API_TOKEN=$API_TOKEN; exit 3'")

        sweep_pass(T0)

        [(_key, _title, _column, body)] = self.filed("alpha")
        self.assertIn("API_TOKEN=", body)
        self.assertNotIn("smoke-secret-value", body)

    def test_a_failure_while_its_ticket_is_open_files_no_second(self):
        self.smoke("alpha", "act", FAILING)
        sweep_pass(T0)
        [(key, *_rest)] = self.filed("alpha")

        sweep_pass(T0 + 6 * HOUR)

        self.assertEqual(len(self.filed("alpha")), 1)
        newest = self.smoke_firings("alpha")[0]
        self.assertEqual(newest.reason, f"already filed {key}")

    def test_a_dry_run_failure_is_proposed_and_files_nothing(self):
        sha = self.smoke("alpha", "dry_run", FAILING)

        sweep_pass(T0)

        self.assertEqual(self.filed("alpha"), [])
        [firing] = self.smoke_firings("alpha")
        self.assertEqual((firing.mode, firing.state), ("dry_run", "proposed"))
        self.assertEqual(firing.reason,
                         f"would file Smoke check fails on main at {sha[:12]}")

    def test_a_kill_switch_set_during_the_run_files_nothing(self):
        self.smoke("alpha", "act", "printf '{\"off\": true, \"note\":"
                   " \"halt\"}' > \"$HOLOPHYTE_HOME/automations.json\"; exit 1")

        sweep_pass(T0)

        self.assertEqual(self.filed("alpha"), [])
        [firing] = self.smoke_firings("alpha")
        self.assertEqual((firing.mode, firing.state), ("dry_run", "proposed"))
        self.assertIn("automations are off on this host: halt", firing.reason)

    def test_a_kill_switch_set_by_one_project_stops_the_next_one(self):
        self.smoke("alpha", "act", "printf '{\"off\": true}'"
                   " > \"$HOLOPHYTE_HOME/automations.json\"")
        self.smoke("beta", "act", "touch \"$HOLOPHYTE_HOME/beta-ran\"")

        sweep_pass(T0)

        self.assertEqual(len(self.smoke_firings("alpha")), 1)
        self.assertEqual(self.smoke_firings("beta"), [])
        self.assertFalse((self.home / "beta-ran").exists())

    def test_a_pass_files_nothing_and_a_run_not_due_records_nothing(self):
        sha = self.smoke("alpha", "act", "true")
        sweep_pass(T0)
        [passed] = self.smoke_firings("alpha")
        self.assertEqual(passed.reason, f"smoke passed at {sha[:12]}")
        config = self.target("alpha").config_path
        config.write_text(config.read_text().replace(
            'command = "true"', f"command = {json.dumps(FAILING)}"))

        sweep_pass(T0 + 6 * HOUR - MINUTE)

        self.assertEqual(self.smoke_firings("alpha"), [passed])
        self.assertEqual(self.filed("alpha"), [])
        sweep_pass(T0 + 6 * HOUR)
        self.assertEqual(len(self.filed("alpha")), 1)


    def test_a_contained_setup_runs_without_the_implementer_credential(self):
        sha = self.smoke("alpha", "dry_run", "true")
        self.contained("alpha")
        stack, docker = self.uncredentialed()

        with stack:
            sweep_pass(T0)

        [call] = docker.call_args_list
        argv, host_env = call.args[0], call.kwargs["env"]
        self.assertEqual((argv[:2], argv[-3:]),
                         (["docker", "run"], ["/bin/sh", "-c", SETUP]))
        self.assertNotIn(f"--env={CREDENTIAL}", argv)
        self.assertNotIn(CREDENTIAL, host_env)
        [firing] = self.smoke_firings("alpha")
        self.assertEqual((firing.mode, firing.reason),
                         ("dry_run", f"smoke passed at {sha[:12]}"))

    def test_a_credential_the_smoke_command_prints_stays_off_the_board(self):
        self.smoke("alpha", "act", f'echo "${CREDENTIAL}"; exit 3')
        self.contained("alpha")
        stack, docker = self.uncredentialed()

        with stack:
            os.environ[CREDENTIAL] = "smoke-oauth-token-value"
            sweep_pass(T0)

        self.assertNotIn(f"--env={CREDENTIAL}", docker.call_args.args[0])
        [(_key, _title, _column, body)] = self.filed("alpha")
        self.assertIn("exited with status 3", body)
        self.assertNotIn("smoke-oauth-token-value", body)

    def test_a_smoke_run_that_raises_is_recorded_once_per_interval(self):
        sha = self.smoke("alpha", "dry_run", "true")
        self.contained("alpha")
        stack, docker = self.uncredentialed(image=RuntimeError(
            "implementer image x is not built; run: docker build\nsecond"))

        with stack:
            sweep_pass(T0)
            sweep_pass(T0 + 6 * HOUR - MINUTE)

        [firing] = self.smoke_firings("alpha")
        self.assertEqual(
            firing.reason, f"smoke could not run at {sha[:12]}: implementer"
            " image x is not built; run: docker build")
        docker.assert_not_called()

    def test_a_refused_setup_files_nothing_and_says_the_run_could_not_start(
            self):
        sha = self.smoke("alpha", "act", "true")
        with self.target("alpha").config_path.open("a") as handle:
            handle.write('\n[worktree]\nsetup = ["false"]\n')

        sweep_pass(T0)

        self.assertEqual(self.filed("alpha"), [])
        [firing] = self.smoke_firings("alpha")
        self.assertEqual(
            firing.reason, f"smoke could not run at {sha[:12]}: [holo2]"
            " worktree setup command 1 of 1 FAILED: false")

    def test_a_refused_setup_config_is_recorded_once_per_interval(self):
        sha = self.smoke("alpha", "dry_run", "true")
        with self.target("alpha").config_path.open("a") as handle:
            handle.write('\n[worktree]\nsetup = ["true"]\n'
                         "setup_timeout_sec = 0\n")

        sweep_pass(T0)
        sweep_pass(T0 + MINUTE)

        [firing] = self.smoke_firings("alpha")
        self.assertTrue(firing.reason.startswith(
            f"smoke could not run at {sha[:12]}: [holo2] "), firing.reason)
        self.assertIn("setup_timeout_sec must be a finite positive number",
                      firing.reason)

    def test_a_signal_during_the_run_records_no_firing(self):
        self.smoke("alpha", "dry_run", "true")

        with patch("holophyte.automation.smoke.run_worktree_setup",
                   side_effect=SystemExit(143)):
            sweep_pass(T0)

        self.assertEqual(self.smoke_firings("alpha"), [])

    def test_an_implementer_launch_after_a_smoke_check_needs_its_credential(
            self):
        self.smoke("alpha", "dry_run", "true")
        self.contained("alpha")
        target = self.target("alpha")
        stack, docker = self.uncredentialed()

        with stack:
            sweep_pass(T0)
            docker.reset_mock()
            with self.assertRaisesRegex(
                    RuntimeError,
                    f"implementer credential variable {CREDENTIAL} is not set"):
                launcher.launch(launcher.route_for(target), target.path, None,
                                ["claude", "-p", "task"], project=target)

        docker.assert_not_called()


if __name__ == "__main__":
    import unittest
    unittest.main()

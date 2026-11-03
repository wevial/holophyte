"""The main watch in act mode: the host sweep leases a firing and starts a
real `factory.py --automation-firing` worker, whose GitHub calls go through
the factory's `gh api` path to a `gh` on PATH that logs each method and path.

Run: python3 -m unittest discover -s tests -p 'test_automation_act.py' -v
"""
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
from tests.host_fixture import git
from tests.main_watch_fixture import job, sweep_pass
from tests.test_automation_switches import SwitchFixture

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
found = table.get(f"{method} {path}")
if found is None:
    sys.exit(print("gh: Not Found (HTTP 404)", file=sys.stderr) or 1)
print(json.dumps(found))
"""


class ActGitHub:
    """Answers `METHOD PATH` per checkout directory; logs `DIRECTORY METHOD PATH`.
    `WRITE BEFORE METHOD PATH` maps files to the text written as that call
    arrives, a change made while the call is in flight."""

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

if __name__ == "__main__":
    import unittest
    unittest.main()

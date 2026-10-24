"""A bug scout's `run` helpers: a clone of their own, setup before the turn,
the run goal, the project's isolation, the refs left untouched, the read-only
tool profile of every other turn and the stored reproduction level, run by
`factory.py --scout ID PROJECT` against stub CLIs over a real git repository.

Run: python3 -m unittest discover -s tests -p 'test_scout_run_helpers.py' -v
"""
import json
import os
import stat
import sys
import threading
import time
from itertools import takewhile
from pathlib import Path
from unittest.mock import patch

from holophyte.agents.probes import PROBE_GOAL
from holophyte.config.project import Project
from holophyte.isolation import launcher
from holophyte.scout.scout_process import lead_path
from store.scouts import create_scout, move_scout
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_lead import CODEX_LEAD, REPORT
from tests.test_scout_process import ScoutFixture

RUN_Q, READ_Q = "Does the retry test fail on main?", "Where is the retry loop?"
SETUP_RAN = '[worktree]\nsetup = ["touch setup-ran"]\n'
SETUP_FAILS = '[worktree]\nsetup = ["echo broken >&2; exit 3"]\n'
LOOK = "test -e setup-ran && echo present || echo absent"
READ_ONLY = "Read and search only: do not edit files, commit or push."
WRITE_TOOLS = {"Bash", "Edit", "Write", "NotebookEdit"}
DOCKER = """#!{python}
import json, subprocess, sys
with open({log!r}, "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[-1] == {goal!r}:
    print({probe!r})
    sys.exit({probe_exit})
if {helper!r} and sys.argv[-1].startswith("Scout helper turn"):
    (mounted,) = [arg.split(":")[0] for arg in sys.argv
                  if arg.endswith(":/workspace:rw")]
    sys.exit(subprocess.run([{helper!r}, sys.argv[-1]], cwd=mounted).returncode)
sys.exit(1 if sys.argv[1:2] == ["inspect"] else 0)
"""


def plan(*tasks):
    return fenced({"tasks": [{"question": question, "paths": [], "mode": mode}
                             for question, mode in tasks]})


def read_only(argv):
    if Path(argv[0]).name == "codex":
        at = argv.index("--sandbox") if "--sandbox" in argv else None
        return at is not None and argv[at + 1] == "read-only"
    if "--disallowedTools" not in argv:
        return False
    at = argv.index("--disallowedTools") + 1
    tools = list(takewhile(lambda arg: not arg.startswith("-"), argv[at:]))
    after = at + len(tools)
    return (WRITE_TOOLS <= set(tools) and after < len(argv) - 1
            and argv[after].startswith("--"))


class ScoutRunHelperTests(ScoutFixture):
    def scout(self, kind, config="", name="alpha", githooks=False, **routes):
        path = self.project(name, config)
        if githooks:
            (path / ".githooks").mkdir()
            (path / ".githooks" / "pre-commit").write_text("#!/bin/sh\n")
            git(path, "add", ".githooks")
            git(path, "-c", "user.name=w", "-c", "user.email=w@test",
                "commit", "-qm", "githooks")
        info = Path(git(path, "rev-parse", "--path-format=absolute",
                        "--git-common-dir")) / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text("setup-ran\n")
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "why does the retry spin?", kind,
                                30, **routes)
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        return path, conn, scout_id

    def run_scout(self, path, scout_id):
        code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 0, printed)

    def helper(self, question):
        (logged,) = [helper for helper in self.stubs.helpers()
                     if helper["question"] == question]
        return logged

    def task(self, conn, scout_id, question):
        return conn.execute("SELECT phase, excerpt FROM scoutTasks"
                            " WHERE scoutId = ? AND question = ?",
                            (scout_id, question)).fetchone()

    def report(self, conn, scout_id):
        (report,) = conn.execute("SELECT report FROM scoutReports"
                                 " WHERE scoutId = ?", (scout_id,)).fetchone()
        return json.loads(report)

    def test_only_the_run_helper_worktree_runs_the_setup(self):
        path, conn, scout_id = self.scout("bug", SETUP_RAN)
        self.stubs.answer(plan=plan((RUN_Q, "run"), (READ_Q, "read")),
                          report=fenced(REPORT), helper_sh=[LOOK])
        self.run_scout(path, scout_id)
        self.assertEqual(self.helper(RUN_Q)["sh"], [[0, "present"]])
        self.assertEqual(self.helper(READ_Q)["sh"], [[0, "absent"]])
        self.assertFalse((path / "setup-ran").exists())
        lead = lead_path(Project.locate(path, adopt=False), scout_id)
        self.assertTrue(lead.is_dir())
        self.assertFalse((lead / "setup-ran").exists())

    def test_a_failed_setup_fails_its_task_and_launches_no_turn(self):
        path, conn, scout_id = self.scout("bug", SETUP_FAILS)
        self.stubs.answer(plan=plan((RUN_Q, "run"), (READ_Q, "read")),
                          report=fenced(REPORT))
        self.run_scout(path, scout_id)
        phase, excerpt = self.task(conn, scout_id, RUN_Q)
        self.assertEqual(phase, "failed")
        for text in ("worktree setup command 1 of 1 FAILED",
                     "echo broken >&2; exit 3", "broken"):
            self.assertIn(text, excerpt)
        self.assertEqual([call.get("question")
                          for call in self.stubs.turns("helper")], [READ_Q])
        self.assertEqual(self.task(conn, scout_id, READ_Q)[0], "done")
        self.assertEqual(self.report(conn, scout_id)["summary"],
                         REPORT["summary"])

    def test_a_run_helper_leaves_no_branch_and_main_where_it_was(self):
        path, conn, scout_id = self.scout("bug")
        listed = git(path, "for-each-ref", "refs/heads", "refs/tags")
        self.stubs.answer(plan=plan((RUN_Q, "run")), report=fenced(REPORT),
                          helper_sh=[
                              "echo edited >> notes.txt",
                              "git -c user.name=h -c user.email=h@test"
                              " commit -qam edit",
                              "git branch helper-branch",
                              "git update-ref refs/heads/main HEAD"])
        self.run_scout(path, scout_id)
        self.assertEqual([status for status, _ in self.helper(RUN_Q)["sh"]],
                         [0, 0, 0, 0])
        self.assertEqual(git(path, "for-each-ref", "refs/heads", "refs/tags"),
                         listed)
        self.assertEqual(git(path, "show", "main:notes.txt"), "second")
        summaries = " ".join(summary for (summary,) in conn.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_refs'", (scout_id,)))
        self.assertIn("refs/heads/helper-branch", summaries)
        self.assertIn("refs/heads/main", summaries)

    def test_a_run_helper_cannot_push_when_setup_selects_githooks(self):
        path, conn, scout_id = self.scout("bug", SETUP_RAN, githooks=True)
        remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", str(remote))
        self.stubs.answer(plan=plan((RUN_Q, "run")), report=fenced(REPORT),
                          helper_sh=[
                              "echo edited >> notes.txt",
                              "git -c user.name=h -c user.email=h@test"
                              " commit -qam edit",
                              f"git push -q {remote} HEAD:refs/heads/pushed"])
        self.run_scout(path, scout_id)
        statuses = [status for status, _ in self.helper(RUN_Q)["sh"]]
        self.assertEqual(statuses[:2], [0, 0])
        self.assertNotEqual(statuses[2], 0)
        self.assertEqual(git(remote, "for-each-ref"), "")

    def test_a_run_helper_turn_keeps_refs_another_writer_moves_meanwhile(self):
        path, conn, scout_id = self.scout("bug")
        shared = self.root / "turning"
        shared.mkdir()
        self.stubs.answer(plan=plan((RUN_Q, "run")), report=fenced(REPORT),
                          helper_shared=str(shared), helper_sleep=4)
        landed = {}

        def writer():
            deadline = time.monotonic() + 30
            while not any(shared.iterdir()) and time.monotonic() < deadline:
                time.sleep(0.05)
            git(path, "-c", "user.name=w", "-c", "user.email=w@test", "commit",
                "-q", "--allow-empty", "-m", "landed")
            git(path, "branch", "task/holo-1-landed")
            landed["main"] = git(path, "rev-parse", "main")
            landed["during"] = any(shared.iterdir())

        thread = threading.Thread(target=writer)
        thread.start()
        self.run_scout(path, scout_id)
        thread.join()
        self.assertTrue(landed["during"])
        self.assertEqual(git(path, "rev-parse", "main"), landed["main"])
        self.assertEqual(git(path, "rev-parse", "task/holo-1-landed"),
                         landed["main"])

    def test_a_research_scout_runs_its_run_task_as_a_read_task(self):
        path, conn, scout_id = self.scout("research", SETUP_RAN)
        self.stubs.answer(plan=plan((RUN_Q, "run")), report=fenced(REPORT),
                          helper_sh=[LOOK])
        self.run_scout(path, scout_id)
        self.assertEqual(self.helper(RUN_Q)["sh"], [[0, "absent"]])
        (turn,) = self.stubs.turns("helper")
        self.assertIn(READ_ONLY, turn["argv"][-1])
        self.assertNotIn("may edit files", turn["argv"][-1])

    def container_scout(self, probe="ready", probe_exit=0, config="",
                        helper_sh=None):
        """A container bug scout with one run task, run against a stub
        `docker` that answers the helper probe with `probe` and
        `probe_exit`, and with `helper_sh` runs the stub helper in the
        mounted workspace; each `docker run` argv it logged, and the scout."""
        path, conn, scout_id = self.scout(
            "bug", '[agents]\nimplementer_isolation = "container"\n' + config,
            helper_route="haiku")
        git(path, "config", "user.name", "scout-test")
        git(path, "config", "user.email", "scout@test")
        bin_dir, log = self.root / "docker-bin", self.root / "docker.jsonl"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(DOCKER.format(
            python=sys.executable, log=str(log), goal=PROBE_GOAL, probe=probe,
            probe_exit=probe_exit,
            helper=str(self.stubs.directory / "claude") if helper_sh else ""))
        docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
        self.stubs.answer(plan=plan((RUN_Q, "run")), report=fenced(REPORT),
                          helper_sh=helper_sh or [])
        with patch.object(launcher, "image_ready", lambda route: None), \
                patch.object(os, "defpath", f"{bin_dir}{os.pathsep}{os.defpath}"):
            self.run_scout(path, scout_id)
        runs = [argv for argv in map(json.loads, log.read_text().splitlines())
                if argv[:1] == ["run"]]
        goals = [argv for argv in runs
                 if any(arg.startswith("Scout helper turn\n") for arg in argv)]
        return path, conn, scout_id, runs, goals

    def test_a_container_project_runs_the_run_helper_in_the_image(self):
        _, conn, scout_id, runs, goals = self.container_scout()
        self.assertEqual(len(goals), 1)
        self.assertIn("claude", goals[0])
        self.assertEqual(self.stubs.turns("helper"), [])
        self.assertEqual(self.task(conn, scout_id, RUN_Q)[0], "done")

    def test_a_container_run_helper_s_refs_are_reported_and_its_push_refused(self):
        remote = self.root / "remote.git"
        git(self.root, "init", "-q", "--bare", str(remote))
        path, conn, scout_id, _, goals = self.container_scout(
            config=SETUP_RAN, helper_sh=[
                "echo edited >> notes.txt", "git commit -qam edit",
                "git branch helper-branch",
                "git update-ref refs/heads/main HEAD",
                f"git push -q {remote} HEAD:refs/heads/pushed"])
        self.assertEqual(len(goals), 1)
        statuses = [status for status, _ in self.helper(RUN_Q)["sh"]]
        self.assertEqual(statuses[:4], [0, 0, 0, 0])
        self.assertNotEqual(statuses[4], 0)
        self.assertEqual(git(remote, "for-each-ref"), "")
        self.assertEqual(git(path, "for-each-ref", "--format=%(refname)",
                             "refs/heads", "refs/tags"), "refs/heads/main")
        self.assertEqual(git(path, "show", "main:notes.txt"), "second")
        summaries = " ".join(summary for (summary,) in conn.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_refs'", (scout_id,)))
        self.assertIn("refs/heads/helper-branch", summaries)
        self.assertIn("refs/heads/main", summaries)

    def test_a_container_run_helper_commits_in_a_project_with_env_source(self):
        source = self.root / "scout.env"
        source.write_text("TOKEN=value\n")
        path, conn, scout_id, _, goals = self.container_scout(
            config=f'[worktree]\nenv_source = "{source}"\n'
                   'env_allow = ["TOKEN"]\n',
            helper_sh=["echo edited >> notes.txt", "git commit -qam edit"])
        self.assertEqual([status for status, _ in self.helper(RUN_Q)["sh"]],
                         [0, 0])
        self.assertEqual(self.task(conn, scout_id, RUN_Q)[0], "done")
        self.assertEqual(self.report(conn, scout_id)["reproduction"]["level"],
                         "test")

    def test_a_run_route_down_in_the_container_fails_its_task_unlaunched(self):
        _, conn, scout_id, runs, goals = self.container_scout(
            "claude: not found", 127)
        self.assertTrue(any(PROBE_GOAL in argv and "claude" in argv
                            for argv in runs))
        self.assertEqual(goals, [])
        phase, excerpt = self.task(conn, scout_id, RUN_Q)
        self.assertEqual(phase, "failed")
        self.assertIn("helper route haiku is down in the container", excerpt)
        self.assertEqual(self.report(conn, scout_id)["summary"],
                         REPORT["summary"])

    def test_a_container_run_helper_runs_when_the_host_helper_is_missing(self):
        _, conn, scout_id, _, goals = self.container_scout(
            config=CODEX_LEAD
            + f'[harnesses]\nclaude = "{self.root / "missing" / "claude"}"\n')
        self.assertEqual(len(goals), 1)
        self.assertEqual(self.task(conn, scout_id, RUN_Q)[0], "done")
        reproduction = self.report(conn, scout_id)["reproduction"]
        self.assertEqual(reproduction["level"], "test")
        self.assertNotIn("claimed_level", reproduction)

    def test_a_setup_that_cannot_launch_fails_its_task_and_the_report_is_stored(self):
        path, conn, scout_id = self.scout(
            "bug", '[agents]\nimplementer_isolation = "container"\n'
            '[worktree]\nsetup = ["true"]\n')
        self.stubs.answer(plan=plan((RUN_Q, "run")), report=fenced(REPORT))

        def missing(route):
            raise RuntimeError("implementer image is not built")

        with patch.object(launcher, "image_ready", missing):
            self.run_scout(path, scout_id)
        phase, excerpt = self.task(conn, scout_id, RUN_Q)
        self.assertEqual(phase, "failed")
        self.assertIn("implementer image is not built", excerpt)
        self.assertEqual(self.stubs.turns("helper"), [])
        self.assertEqual(self.report(conn, scout_id)["summary"],
                         REPORT["summary"])

    def test_a_finished_run_helper_keeps_the_lead_s_level(self):
        path, conn, scout_id = self.scout("bug")
        self.stubs.answer(plan=plan((RUN_Q, "run")), report=fenced(REPORT))
        self.run_scout(path, scout_id)
        self.assertEqual(self.task(conn, scout_id, RUN_Q)[0], "done")
        reproduction = self.report(conn, scout_id)["reproduction"]
        self.assertEqual(reproduction["level"], "test")
        self.assertNotIn("claimed_level", reproduction)

    def test_a_claimed_level_with_no_finished_run_helper_is_not_reproduced(self):
        path, conn, scout_id = self.scout("bug", SETUP_FAILS)
        self.stubs.answer(plan=plan((RUN_Q, "run")), report=fenced(REPORT))
        self.run_scout(path, scout_id)
        self.assertEqual(self.task(conn, scout_id, RUN_Q)[0], "failed")
        reproduction = self.report(conn, scout_id)["reproduction"]
        self.assertEqual((reproduction["level"], reproduction["claimed_level"],
                          reproduction["why"]),
                         ("not_reproduced", "test", "no run helper finished"))

    def test_only_a_bug_scout_s_run_helper_gets_write_tools(self):
        both = [(READ_Q, "read"), (RUN_Q, "run")]
        scouts = (("recon", CODEX_LEAD, "haiku", [(READ_Q, "read")]),
                  ("bug", "", "luna", both), ("bug", CODEX_LEAD, "haiku", both))
        for number, (kind, config, helper, tasks) in enumerate(scouts):
            with self.subTest(kind=kind, helper=helper):
                (self.stubs.directory / "calls.jsonl").unlink(missing_ok=True)
                path, conn, scout_id = self.scout(
                    kind, config, f"scout{number}", helper_route=helper)
                self.stubs.answer(plan=plan(*tasks), report=fenced(REPORT))
                self.run_scout(path, scout_id)
                leads = [call for call in self.stubs.calls()
                         if call["turn"] in ("plan", "synthesis", "report")]
                self.assertTrue(leads)
                for call in leads:
                    self.assertTrue(read_only(call["argv"]), call["argv"])
                helpers = {call["question"]: call["argv"]
                           for call in self.stubs.turns("helper")}
                self.assertEqual(set(helpers), {question for question, _ in tasks})
                self.assertTrue(read_only(helpers[READ_Q]), helpers[READ_Q])
                if RUN_Q in helpers:
                    self.assertNotIn("--sandbox", helpers[RUN_Q])
                    self.assertNotIn("--disallowedTools", helpers[RUN_Q])

"""`holo patrol`'s main checks, check-held pull requests and host health:
GitHub through a patched `pr_status.rest`, the host through the real `df`,
real processes under `/proc` and `systemctl`/`journalctl` scripts on PATH.

Run: python3 -m unittest discover -s tests -p 'test_patrol_host.py' -v
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.pr.pr_status
import store
import store.tickets
from holophyte.cli.status import SWEEP_STATE
from holophyte.config.project import Project, worktree_path
from holophyte.host import host_health
from holophyte.host.registry import Host
from holophyte.loop.gates import InfraFailure
from holophyte.story.witness_running import mark_running
from tests.host_fixture import git
from tests.phase_fixture import park_run
from tests.test_patrol import MIN, NOW, PatrolFixture, projects_text

PULL = "https://github.com/o/n/pull/{}"
SKIPPED_UNITS = "  units: skipped (systemctl is not on this host)"
UNREADABLE_KERNEL = "  kernel log: not readable"
UNITS_SCRIPT = """#!/bin/sh
for unit; do :; done
case "$unit" in
  holophyte-loop@q) echo failed; exit 3 ;;
  holophyte-sweep.timer) echo inactive; exit 3 ;;
  *) echo active ;;
esac
"""
KERNEL_LINES = ("nvme nvme0: I/O 12 QID 3 timeout, aborting",
                "blk_update_request: I/O error, dev nvme0n1",
                "usb 1-1: new device")
HINT = ("Hint: You are currently not seeing messages from other users and the"
        " system.")


def runs(**conclusions):
    return [{"name": name, "status": "completed", "conclusion": conclusion}
            for name, conclusion in conclusions.items()]


class GithubFixture(PatrolFixture):
    def setUp(self):
        super().setUp()
        self.required = ["unit"]
        self.check_runs = {"main": runs(unit="success")}
        self.pulls = {}
        self.enterContext(patch.object(holophyte.pr.pr_status, "rest",
                                       self.rest))

    def rest(self, target, pull, method, path, payload=None):
        if path.endswith("/rules/branches/main"):
            return [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": context}
                                           for context in self.required]}}]
        if path.endswith("/branches/main"):
            return {"name": "main", "protected": False}
        found = re.search(r"/commits/([^/]+)/check-runs", path)
        if found:
            batch = self.check_runs[found[1]]
            return {"total_count": len(batch), "check_runs": batch}
        found = re.search(r"/pulls/(\d+)$", path)
        if found:
            state, head = self.pulls[int(found[1])]
            return {"state": state, "head": {"sha": head}}
        raise AssertionError(f"unexpected GET {path}")

    def github_project(self, name):
        conn = self.project(name)
        git(self.conns[name][2], "remote", "add", "origin",
            "https://github.com/o/n.git")
        return conn

    def seen(self, open_threads):
        return (None, open_threads, open_threads, 0, "failure", None, None,
                None)

    def parked(self, name, key, number, open_threads):
        conn = self.conns[name][0]
        ticket = self.ticket(name, key)
        store.tickets.transition(conn, ticket, "in_flight")
        run = self.claim(name, ticket, NOW - 9 * MIN)
        park_run(conn, run, "awaiting_merge_approval", park_kind="ci",
                 pr_url=PULL.format(number), pr_seen=self.seen(open_threads),
                 now=NOW - 9 * MIN)
        store.tickets.transition(conn, ticket, "blocked_on_operator")
        return run

    def flying(self, name, key, number, open_threads):
        conn = self.conns[name][0]
        run = self.live(name, key, NOW - MIN)
        store.set_pull_request(conn, run, PULL.format(number))
        store.record_pr_seen(conn, run, self.seen(open_threads))
        return run


class MainChecksTests(GithubFixture):
    def test_main_names_each_red_run_and_whether_main_requires_it(self):
        self.github_project("q")
        self.check_runs["main"] = runs(unit="failure", docs="failure",
                                       lint="success")
        row = self.patrol("q")
        self.assertEqual(row["main"], {"state": "failure", "failed": [
            {"name": "unit", "required": True},
            {"name": "docs", "required": False}]})
        self.assertFalse(row["clean"])
        self.assertIn("  main failure: unit (required), docs\n",
                      self.holo("-p", "q")[1])
        self.check_runs["main"] = runs(unit="success", docs="success",
                                       lint="success")
        self.assertEqual(self.patrol("q")["main"], {"state": "success"})
        self.assertEqual(projects_text(self.holo("-p", "q")[1]),
                         "[q] nothing to do\n")

    def test_a_project_without_an_origin_skips_main(self):
        self.project("p")
        self.assertEqual(self.patrol("p")["main"]["state"], "skipped")

    def test_an_unreadable_github_is_unknown_and_keeps_the_store_sections(self):
        self.github_project("q")
        self.live("q", "R1", NOW - MIN)

        def outage(target, pull, method, path, payload=None):
            raise InfraFailure("GitHub answered 502")
        with patch.object(holophyte.pr.pr_status, "rest", outage):
            row = self.patrol("q")
            code, text = self.holo("-p", "q")
        self.assertEqual(row["main"], {"state": "unknown",
                                       "detail": "GitHub answered 502"})
        self.assertFalse(row["clean"])
        self.assertEqual([run["ticket"] for run in row["in_flight"]], ["R1"])
        self.assertEqual(code, 0)
        self.assertIn("  github: unreadable (GitHub answered 502)\n", text)
        self.assertIn("  in flight R1 run ", text)


class HeldPullTests(GithubFixture):
    def held_project(self):
        self.github_project("q")
        self.runs = {"K1": self.parked("q", "K1", 7, 0),
                     "K2": self.flying("q", "K2", 8, 2),
                     "K3": self.parked("q", "K3", 9, 0)}
        self.pulls = {7: ("open", "S7"), 8: ("open", "S8"), 9: ("closed", "S9")}
        self.check_runs.update(S7=runs(unit="failure", docs="failure"),
                               S8=runs(unit="failure"),
                               S9=runs(unit="failure"))

    def test_open_pulls_held_by_a_red_check_are_listed_with_their_threads(self):
        self.held_project()
        row = self.patrol("q")
        self.assertEqual(row["pull_requests"], [
            {"ticket": "K2", "run": self.runs["K2"], "url": PULL.format(8),
             "number": 8, "failed": [{"name": "unit", "required": True}],
             "open_threads": 2, "checks_only": False},
            {"ticket": "K1", "run": self.runs["K1"], "url": PULL.format(7),
             "number": 7, "failed": [{"name": "unit", "required": True},
                                     {"name": "docs", "required": False}],
             "open_threads": 0, "checks_only": True}])
        text = self.holo("-p", "q")[1]
        self.assertIn("  pr K1 #7 held by checks: unit (required), docs"
                      " (checks only)\n", text)
        self.assertIn("  pr K2 #8 held by checks: unit (required)\n", text)
        self.assertNotIn("#9", text)

    def test_an_optional_red_check_holds_only_when_main_requires_none(self):
        self.held_project()
        self.check_runs["S7"] = runs(unit="success", docs="failure")
        self.assertNotIn(7, [pull["number"]
                             for pull in self.patrol("q")["pull_requests"]])
        self.required = []
        held = {pull["number"]: pull for pull in self.patrol("q")["pull_requests"]}
        self.assertEqual(held[7]["failed"], [{"name": "docs", "required": False}])

    def test_an_ignored_ticket_hides_its_held_pull_request(self):
        self.held_project()
        self.configure("q", 'ignore = ["K1"]')
        row = self.patrol("q")
        self.assertEqual([pull["ticket"] for pull in row["pull_requests"]],
                         ["K2"])
        self.assertEqual(row["ignored"], ["K1"])

    def test_a_paused_ticket_on_a_red_pull_request_is_not_held(self):
        self.held_project()
        store.release(self.conns["q"][0], self.runs["K1"], "paused")
        self.assertNotIn("K1", [pull["ticket"]
                                for pull in self.patrol("q")["pull_requests"]])

    def test_a_pull_answer_without_a_state_is_unreadable(self):
        self.held_project()
        healthy = self.rest

        def rest(target, pull, method, path, payload=None):
            return {} if path.endswith("/pulls/7") else healthy(
                target, pull, method, path, payload)
        with patch.object(holophyte.pr.pr_status, "rest", rest):
            row = self.patrol("q")
        self.assertEqual([pull["number"] for pull in row["unreadable_pulls"]],
                         [7])
        self.assertFalse(row["clean"])

    def test_an_unreadable_pull_request_is_reported_not_dropped(self):
        self.held_project()
        healthy = self.rest
        for failing in ("/pulls/7", "/commits/S7/check-runs"):
            def rest(target, pull, method, path, payload=None):
                if failing in path:
                    raise InfraFailure("GitHub answered 502")
                return healthy(target, pull, method, path, payload)
            with self.subTest(failing=failing), \
                    patch.object(holophyte.pr.pr_status, "rest", rest):
                row = self.patrol("q")
                self.assertEqual(
                    [(pull["ticket"], pull["number"], pull["detail"])
                     for pull in row["unreadable_pulls"]],
                    [("K1", 7, "GitHub answered 502")])
                self.assertEqual([pull["number"]
                                  for pull in row["pull_requests"]], [8])
                self.assertFalse(row["clean"])
                self.assertIn("  pr K1 #7 github: unreadable"
                              " (GitHub answered 502)\n", self.holo("-p", "q")[1])


class TmpUsageTests(unittest.TestCase):
    def test_used_percent_and_free_bytes_match_the_real_df(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(tempfile, "tempdir", directory):
            usage = host_health.tmp_usage()
            df = subprocess.run(["df", "-P", directory], capture_output=True,
                                text=True, check=True).stdout
            block = os.statvfs(directory).f_frsize
        fields = df.splitlines()[-1].split()
        self.assertAlmostEqual(usage["used_percent"], int(fields[-2][:-1]),
                               delta=1)
        self.assertAlmostEqual(usage["free_bytes"], int(fields[-3]) * 1024,
                               delta=block)


class HostFixtureParts(PatrolFixture):
    def setUp(self):
        super().setUp()
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        self.enterContext(patch.object(tempfile, "tempdir", str(self.tmp)))
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / SWEEP_STATE).write_text(json.dumps(
            {"started": NOW - MIN, "ended": NOW - MIN + 1000}))

    def on_path(self, scripts, keep=("git",)):
        """PATH holding only `scripts` (name to text) and the `keep` tools."""
        directory = Path(tempfile.mkdtemp(dir=self.root))
        for tool in keep:
            (directory / tool).symlink_to(shutil.which(tool))
        for name, text in scripts.items():
            (directory / name).write_text(text)
            (directory / name).chmod(0o755)
        os.environ["PATH"] = str(directory)

    def roomy_tmp(self):
        self.enterContext(patch.object(host_health, "TMP_FULL_PERCENT", 101))

    def sleeper(self, cwd, *args):
        """A real `sleep 600` in `cwd`, or a sleeping Python naming `args`."""
        argv = ([sys.executable, "-c", "import time; time.sleep(600)", *args]
                if args else ["sleep", "600"])
        child = subprocess.Popen(argv, cwd=cwd)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child.pid


@unittest.skipUnless(Path("/proc").is_dir(), "needs /proc")
class LeftoverTests(HostFixtureParts):
    def merged_worktree(self, name):
        conn, project_id, path = self.conns[name]
        target = Project.locate(path)
        ticket = self.ticket(name, "M1")
        store.tickets.transition(conn, ticket, "in_flight")
        run = store.claim(conn, project_id, ticket)
        store.set_branch(conn, run, "task/m1-done")
        for phase in ("merge_gate", "merging"):
            store.set_phase(conn, run, phase)
        store.release(conn, run, "merged")
        store.tickets.walk_ticket(conn, ticket, "merged")
        tree = worktree_path(target, "task/m1-done")
        tree.mkdir(parents=True)
        return tree

    def test_processes_in_removed_witness_and_finished_worktrees_are_listed(self):
        witness = self.tmp / "witness-x" / "tree"
        witness.mkdir(parents=True)
        first = self.sleeper(witness)
        shutil.rmtree(self.tmp / "witness-x")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        second = self.sleeper(elsewhere)
        self.project("p")
        third = self.sleeper(self.merged_worktree("p"))
        found = host_health.leftovers(Host.locate().projects())
        pids = [process["pid"] for process in found["processes"]]
        self.assertTrue(found["readable"])
        self.assertEqual(sorted(set(pids) & {first, second, third}),
                         sorted([first, third]))
        self.assertEqual(
            {process["pid"]: process["args"] for process in found["processes"]
             }[first], "sleep 600")

    def test_a_live_witness_pass_keeps_only_its_existing_trees(self):
        self.project("p")
        mark_running(Project.locate(self.conns["p"][2]), "S", ["K"])
        live = self.tmp / "witness-live" / "tree"
        live.mkdir(parents=True)
        kept = self.sleeper(live)
        removed = self.tmp / "witness-gone" / "tree"
        removed.mkdir(parents=True)
        gone = self.sleeper(removed)
        shutil.rmtree(self.tmp / "witness-gone")
        pids = {process["pid"] for process in host_health.leftovers(
            Host.locate().projects())["processes"]}
        self.assertEqual(pids & {kept, gone}, {gone})

    def test_a_live_witness_pass_counts_for_a_project_whose_config_breaks(self):
        self.project("p")
        target = Project.locate(self.conns["p"][2])
        mark_running(target, "S", ["K"])
        live = self.tmp / "witness-live" / "tree"
        live.mkdir(parents=True)
        kept = self.sleeper(live)
        target.config_path.write_text("[board\n")
        (entry,) = Host.locate().projects()
        self.assertIsNotNone(entry.error)
        pids = {process["pid"] for process in host_health.leftovers(
            [entry])["processes"]}
        self.assertNotIn(kept, pids)

    def test_a_removed_cwd_under_a_kept_worktree_parent_is_listed(self):
        self.project("p")
        parent = Project.locate(self.conns["p"][2]).worktrees / "feature"
        (parent / "foo").mkdir(parents=True)
        gone = self.sleeper(parent / "foo")
        shutil.rmtree(parent / "foo")
        pids = {process["pid"] for process in host_health.leftovers(
            Host.locate().projects())["processes"]}
        self.assertIn(gone, pids)

    def test_an_argument_path_is_judged_where_it_really_lies(self):
        self.project("p")
        (self.tmp / "witness-x").mkdir()
        link = self.root / "tmp-link"
        link.symlink_to(self.tmp)
        tempfile.tempdir = str(link)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        through_link = self.sleeper(elsewhere,
                                    f"{link}/witness-x/server.py")
        outside = self.sleeper(elsewhere, f"{self.tmp}/witness-x/../safe.py")
        pids = {process["pid"] for process in host_health.leftovers(
            Host.locate().projects())["processes"]}
        self.assertEqual(pids & {through_link, outside}, {through_link})

    def test_a_host_without_proc_skips_the_part(self):
        with patch.object(host_health, "PROC", self.root / "no-proc"):
            self.assertEqual(host_health.leftovers([]),
                             {"readable": False, "detail": "skipped (no /proc)",
                              "processes": []})


class UnitTests(HostFixtureParts):
    def test_failed_units_and_an_inactive_sweep_timer_are_the_findings(self):
        self.project("p")
        self.project("q")
        self.on_path({"systemctl": UNITS_SCRIPT})
        text = self.holo("--json")[1]
        units = json.loads(text)["host"]["units"]
        self.assertEqual(units["units"], [
            {"unit": "holophyte-sweep.timer", "state": "inactive"},
            {"unit": "holophyte-serve.socket", "state": "active"},
            {"unit": "holophyte-loop@p", "state": "active"},
            {"unit": "holophyte-loop@q", "state": "failed"}])
        self.assertEqual(units["findings"], [
            {"unit": "holophyte-sweep.timer", "state": "inactive"},
            {"unit": "holophyte-loop@q", "state": "failed"}])
        self.assertEqual(
            [line for line in self.holo()[1].splitlines()
             if line.startswith("  unit")],
            ["  unit holophyte-sweep.timer inactive",
             "  unit holophyte-loop@q failed"])

    def test_without_systemctl_the_part_is_skipped_and_no_finding(self):
        self.project("p")
        self.roomy_tmp()
        self.on_path({})
        text = self.holo("--json")[1]
        host = json.loads(text)["host"]
        self.assertEqual((host["units"]["detail"], host["units"]["findings"],
                          host["clean"]),
                         ("skipped (systemctl is not on this host)", [], True))
        self.assertIn(SKIPPED_UNITS, self.holo()[1].splitlines())


class KernelTests(HostFixtureParts):
    def journal(self, body):
        self.on_path({"journalctl": "#!/bin/sh\n" + body})

    def test_io_errors_and_nvme_timeouts_are_kept_in_order(self):
        self.journal("".join(f"echo '{line}'\n" for line in KERNEL_LINES))
        self.assertEqual(host_health.kernel_errors(),
                         {"readable": True, "errors": list(KERNEL_LINES[:2])})

    def test_a_failing_or_hinting_journal_is_unreadable_and_no_finding(self):
        self.project("p")
        self.roomy_tmp()
        for body in ("exit 1\n",
                     f"echo '{KERNEL_LINES[1]}'\necho '{HINT}' >&2\n"):
            with self.subTest(body=body):
                self.journal(body)
                self.assertFalse(host_health.kernel_errors()["readable"])
                host = json.loads(self.holo("--json")[1])["host"]
                self.assertTrue(host["clean"])
                self.assertIn(UNREADABLE_KERNEL, self.holo()[1].splitlines())


class CleanHostTests(HostFixtureParts):
    def test_a_clean_host_is_one_line_then_its_skipped_parts_in_both_forms(self):
        self.project("p")
        self.roomy_tmp()
        self.on_path({})
        expected = ["[host] nothing to do", SKIPPED_UNITS, UNREADABLE_KERNEL]
        for argv in ((), ("-p", "p")):
            with self.subTest(argv=argv):
                text = self.holo(*argv)[1]
                self.assertEqual(text.splitlines()[:3], expected)
                self.assertEqual(text.splitlines()[3], "[p] nothing to do")
                host = json.loads(self.holo("--json", *argv)[1])["host"]
                self.assertEqual(sorted(host), sorted(
                    ["attention", "tmp", "leftovers", "units", "kernel",
                     "clean"]))
                self.assertTrue(host["clean"])


@unittest.skipUnless(Path("/proc").is_dir(), "needs /proc")
class HostFindingTests(HostFixtureParts):
    def test_each_finding_is_its_own_host_line(self):
        self.project("p")
        self.enterContext(patch.object(host_health, "TMP_FULL_PERCENT", 0))
        witness = self.tmp / "witness-x" / "tree"
        witness.mkdir(parents=True)
        pid = self.sleeper(witness)
        shutil.rmtree(self.tmp / "witness-x")
        self.on_path({"journalctl": "#!/bin/sh\n" + "".join(
            f"echo '{line}'\n" for line in KERNEL_LINES)})
        host = json.loads(self.holo("--json")[1])["host"]
        lines = self.holo()[1].splitlines()
        self.assertFalse(host["clean"])
        self.assertEqual(lines[0], "[host]")
        self.assertRegex(lines[1], rf"^  /tmp {host['tmp']['used_percent']}%"
                         r" used, \d+(\.\d)? (B|KiB|MiB|GiB|TiB) free$")
        leftover = (rf"  leftover pid {pid} \(\d+ s\):"
                    rf" {re.escape(str(witness))} sleep 600")
        self.assertEqual(len([line for line in lines
                              if re.fullmatch(leftover, line)]), 1)
        self.assertIn("  kernel 2 I/O errors in the last hour:"
                      f" {KERNEL_LINES[1]}", lines)

"""The [scout] table's routes, defaults and refusals, and factory.py --scout-probe."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from holophyte.agents.probe_log import last_probes
from holophyte.config.checks import check_config
from holophyte.config.project import Project
from holophyte.scout.scout_config import scout_config

ROOT = Path(__file__).resolve().parents[1]
STUB = """#!/bin/sh
printf '%s\\t%s\\t%s\\t%s\\n' "$(basename "$0")" "$*" "$(pwd)" \
  "$(git rev-parse HEAD 2>/dev/null)" >> "$SCOUT_PROBE_LOG"
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class ScoutFixture(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.home = self.root / "home"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        patched = mock.patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.home)})
        patched.start()
        self.addCleanup(patched.stop)

    def project(self, config=""):
        project = Project.locate(self.repo)
        project.config_path.parent.mkdir(parents=True, exist_ok=True)
        project.config_path.write_text(config)
        return project


class ScoutConfigTests(ScoutFixture):
    def test_no_scout_table_reads_the_built_in_routes_and_budget(self):
        config = scout_config(self.project())
        self.assertEqual(list(config.leads), ["opus", "sol"])
        self.assertEqual(list(config.helpers), ["luna", "haiku"])
        self.assertEqual((config.lead, config.helper), ("opus", "luna"))
        self.assertEqual((config.budget_min, config.swarm, config.helper_turns,
                          config.waves), (30, 4, 12, 2))
        self.assertEqual(
            config.helpers["luna"].turn("GOAL"),
            ["codex", "exec", "-m", "gpt-6-luna", "-c",
             "model_reasoning_effort=low",
             "--dangerously-bypass-approvals-and-sandbox", "GOAL"])
        self.assertIn("--model opus --effort high",
                      shlex.join(config.leads["opus"].turn("GOAL")))

    def test_only_the_lead_turn_and_resume_turn_off_the_cli_subagents(self):
        config = scout_config(self.project())
        seats = {**config.leads, **config.helpers}
        for name, seat in seats.items():
            for argv in (seat.turn("GOAL"), seat.resume("S")):
                with self.subTest(route=name, argv=argv):
                    claude = "--disallowedTools" in argv
                    codex = shlex.join(argv).count("--disable multi_agent")
                    self.assertEqual(claude, name == "opus")
                    self.assertEqual(codex, int(name == "sol"))
                    if claude:
                        at = argv.index("--disallowedTools")
                        self.assertEqual(argv[at + 1:at + 3], ["Agent", "Task"])
                        self.assertTrue(argv[at + 3].startswith("--"), argv)

    def test_a_configured_leads_table_replaces_the_built_in_leads(self):
        config = scout_config(self.project(
            '[scout]\nlead = "deep"\n[scout.leads.deep]\nharness = "codex"\n'
            'model = "gpt-test"\neffort = "xhigh"\n'))
        self.assertEqual(list(config.leads), ["deep"])
        self.assertEqual(list(config.helpers), ["luna", "haiku"])
        self.assertEqual(
            config.leads["deep"].turn("GOAL")[:6],
            ["codex", "exec", "-m", "gpt-test", "-c",
             "model_reasoning_effort=xhigh"])


class ScoutRefusalTests(ScoutFixture):
    CASES = (
        ('[scout.leads.c]\nharness = "cursor"\nmodel = "m"\n',
         ("[scout.leads.c] harness", "not scout-lead")),
        ('[scout.helpers.h]\nharness = "devin"\nmodel = "m"\n',
         ("[scout.helpers.h] harness", "not scout-helper")),
        ('[scout.helpers.h]\nharness = "claude"\nmodel = "m"\neffort = "low"\n'
         'colour = "red"\n',
         ("[scout.helpers.h] colour: unknown key",)),
        ('[scout]\nlead = "nope"\n', ("[scout] lead", "'nope'", "opus, sol")),
        ('[scout]\nswarm = 5\nhelper_turns = 4\n',
         ("[scout] swarm", "helper_turns = 4")),
        ('[scout]\nwaves = 3\n', ("[scout] waves", "got 3")),
        ('[scout]\nbudget = 1\n', ("[scout] budget: unknown key",)),
    )

    def test_check_config_refuses_each_bad_table_naming_it_and_the_key(self):
        for config, named in self.CASES:
            with self.subTest(config=config):
                with self.assertRaises(SystemExit) as refused:
                    check_config(self.project(config))
                for words in named:
                    self.assertIn(words, str(refused.exception))


class ScoutProbeModeTests(ScoutFixture):
    def setUp(self):
        super().setUp()
        git(self.repo, "init", "--quiet", "--initial-branch", "main")
        (self.repo / "README").write_text("scout\n")
        git(self.repo, "add", "README")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.com",
            "commit", "--quiet", "-m", "first")
        self.main = git(self.repo, "rev-parse", "main")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "probe.log"
        for name, answer in (("claude", "echo ready\n"), ("codex", "exit 3\n")):
            stub = self.bin / name
            stub.write_text(STUB + answer)
            stub.chmod(0o755)

    def probe(self):
        env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                   SCOUT_PROBE_LOG=str(self.log))
        return subprocess.run(
            [sys.executable, str(ROOT / "factory.py"), "--scout-probe",
             str(self.repo)], capture_output=True, text=True, env=env,
            cwd=self.root, timeout=120)

    def logged(self):
        rows = [line.split("\t") for line in self.log.read_text().splitlines()]
        return {(name, args): (cwd, head) for name, args, cwd, head in rows}

    def test_default_routes_probe_and_a_failing_codex_exits_one(self):
        project = self.project()
        result = self.probe()
        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stdout.splitlines()
        for seat in ("scout-lead:opus", "scout-helper:haiku"):
            self.assertTrue(any(f"{seat} probe passed" in line
                                for line in lines), result.stdout)
        for seat in ("scout-lead:sol", "scout-helper:luna"):
            self.assertTrue(any(f"{seat} probe failed (exit 3)" in line
                                for line in lines), result.stdout)
        snapshot = last_probes(project)
        self.assertEqual(set(snapshot), {"scout-lead:opus", "scout-lead:sol",
                                         "scout-helper:luna",
                                         "scout-helper:haiku"})
        for seat, ok in (("scout-lead:opus", True), ("scout-helper:haiku", True),
                         ("scout-lead:sol", False), ("scout-helper:luna", False)):
            with self.subTest(seat=seat):
                entry = snapshot[seat]["primary"]
                self.assertIs(entry["ok"], ok)
                self.assertEqual(entry["why"], None if ok else "exit 3")
        calls = self.logged()
        self.assertEqual(len(calls), 4)
        luna = [args for name, args in calls if name == "codex"
                and "-m gpt-6-luna" in args]
        self.assertEqual(len(luna), 1)
        self.assertIn("model_reasoning_effort=low", luna[0])
        self.assertTrue(any(name == "claude" and "--model haiku --effort low" in args
                            for name, args in calls))
        for cwd, head in calls.values():
            self.assertNotEqual(Path(cwd).resolve(), self.repo.resolve())
            self.assertEqual(head, self.main)

    def test_scout_entries_land_beside_an_existing_seat_entry(self):
        project = self.project(
            '[scout]\nlead = "one"\nhelper = "two"\n'
            '[scout.leads.one]\nharness = "claude"\nmodel = "opus"\n'
            'effort = "high"\n'
            '[scout.helpers.two]\nharness = "claude"\nmodel = "haiku"\n'
            'effort = "low"\n')
        implementer = {"primary": {"ok": True, "at_ms": 1, "duration_ms": 2,
                                   "why": None}}
        (project.holo_dir / "route-probes.json").write_text(
            json.dumps({"implementer": implementer}))
        result = self.probe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        snapshot = last_probes(project)
        self.assertEqual(snapshot["implementer"], implementer)
        self.assertEqual(set(snapshot), {"implementer", "scout-lead:one",
                                         "scout-helper:two"})
        for seat in ("scout-lead:one", "scout-helper:two"):
            self.assertIs(snapshot[seat]["primary"]["ok"], True)


if __name__ == "__main__":
    unittest.main()

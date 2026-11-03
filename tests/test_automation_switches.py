"""The host's automation kill switch and one automation's pause, run through
`holo` in-process against real stores and the host sweep, with main's GitHub
reads answered by a `gh` on PATH.

Run: python3 -m unittest discover -s tests -p 'test_automation_switches.py' -v
"""
import contextlib
import io
import json
import os
from unittest.mock import patch

from holophyte.config.project import Project
from holophyte.holo import cli as holo_cli
from tests.main_watch_fixture import automations, sweep_pass
from tests.test_main_watch import MainWatchFixture
from tests.test_sweep_host import MINUTE, T0


class SwitchFixture(MainWatchFixture):
    NAMES = ("alpha", "beta")

    def setUp(self):
        super().setUp()
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("HOLO_PROJECT", None)

    def repo(self, directory, team=None, name=None):
        return super().repo(directory, team, name or directory)

    def holo(self, *argv):
        """`holo ARGV`'s exit code and its stderr, a SystemExit's text included."""
        return self.holo_out(*argv)[:2]

    def holo_out(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = holo_cli.main(list(argv))
            except SystemExit as stop:
                code = stop.code
        if isinstance(code, str):
            err.write(code)
            code = 1
        return code or 0, err.getvalue(), out.getvalue()

    def result(self, *argv):
        code, _, out = self.holo_out(*argv, "--json")
        return code, json.loads(out)

    def opted_in_red(self, *names):
        for name in names:
            automations(self.paths[name], "dry_run")
            self.red(name)

    def pauses(self, name):
        return self.conn(name).execute(
            "SELECT automation, note, actor, clearedAt, clearNote"
            " FROM automationPauses ORDER BY id").fetchall()

    def files(self):
        return {name: (self.home / name).read_bytes()
                for name in ("automations.json", "host-actions.jsonl")
                if (self.home / name).exists()}


class KillSwitchTests(SwitchFixture):
    def test_the_kill_switch_stops_every_project_until_it_is_on(self):
        self.opted_in_red(*self.NAMES)

        self.assertEqual(self.holo("automations", "off", "investigating")[0], 0)
        sweep_pass(T0)

        record = json.loads((self.home / "automations.json").read_text())
        self.assertEqual((record["off"], record["note"]), (True, "investigating"))
        [line] = (self.home / "host-actions.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(line)["note"], "investigating")
        for name in self.NAMES:
            self.assertEqual(self.firings(name), [])
            self.assertEqual(self.github.head_reads(self.paths[name]), [])

        self.assertEqual(self.holo("automations", "on", "done")[0], 0)
        sweep_pass(T0 + MINUTE)

        for name in self.NAMES:
            [firing] = self.firings(name)
            self.assertEqual(firing.state, "proposed")

    def test_off_while_off_and_on_while_on_are_refused(self):
        code, _ = self.holo("automations", "on", "x")
        self.assertEqual(code, 1)
        self.assertEqual(self.files(), {})

        self.holo("automations", "off", "investigating")
        written = self.files()
        code, err = self.holo("automations", "off", "again")
        self.assertEqual(code, 1)
        self.assertIn("investigating", err)
        self.assertEqual(self.files(), written)

        self.holo("automations", "on", "done")
        written = self.files()
        self.assertEqual(self.holo("automations", "on", "x")[0], 1)
        self.assertEqual(self.files(), written)

    def test_a_project_given_to_the_host_switch_is_refused(self):
        code, err = self.holo("automations", "off", "maintenance", "-p", "alpha")

        self.assertEqual(code, 2)
        self.assertIn("-p", err)
        self.assertEqual(self.files(), {})

    def test_the_switch_prints_a_json_result_and_a_json_refusal(self):
        code, result = self.result("automations", "off", "investigating")
        self.assertEqual(code, 0)
        self.assertEqual((result["action"], result["ok"]),
                         ("automations off", True))
        self.assertIn("investigating", result["detail"])

        code, result = self.result("automations", "off", "again")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertIn("investigating", result["detail"])

    def test_an_unreadable_kill_switch_counts_as_off(self):
        self.opted_in_red("alpha")
        path = self.home / "automations.json"
        path.write_text("{not json")

        printed = sweep_pass(T0)

        named = [line for line in printed.splitlines() if str(path) in line]
        self.assertEqual(len(named), 1, printed)
        self.assertEqual(self.firings("alpha"), [])
        self.assertEqual(self.github.head_reads(self.paths["alpha"]), [])


class PauseTests(SwitchFixture):
    def test_a_pause_outlives_config_and_only_resume_clears_it(self):
        self.opted_in_red("alpha")

        code, _ = self.holo("automation", "pause", "main_watch", "flaky runner",
                            "-p", "alpha")
        self.assertEqual(code, 0)
        sweep_pass(T0)
        config = Project.locate(self.paths["alpha"], adopt=False).config_path
        config.write_text(config.read_text())
        sweep_pass(T0 + MINUTE)

        self.assertEqual(self.pauses("alpha"),
                         [("main_watch", "flaky runner", "operator", None, None)])
        self.assertEqual(self.firings("alpha"), [])
        self.assertEqual(self.github.head_reads(self.paths["alpha"]), [])

        code, _ = self.holo("automation", "resume", "main_watch", "fixed",
                            "-p", "alpha")
        self.assertEqual(code, 0)
        sweep_pass(T0 + 2 * MINUTE)

        [(_, _, _, cleared, note)] = self.pauses("alpha")
        self.assertIsNotNone(cleared)
        self.assertEqual(note, "fixed")
        [firing] = self.firings("alpha")
        self.assertEqual(firing.state, "proposed")

    def test_an_unknown_name_or_a_missing_pause_is_refused(self):
        code, err = self.holo("automation", "pause", "nightly", "x",
                              "-p", "alpha")
        self.assertEqual(code, 2)
        self.assertIn("main_watch", err)

        code, _ = self.holo("automation", "resume", "main_watch", "x",
                            "-p", "alpha")
        self.assertEqual(code, 1)
        self.assertEqual(self.pauses("alpha"), [])

    def test_a_pause_prints_a_json_result(self):
        code, result = self.result("automation", "pause", "main_watch",
                                   "flaky runner", "-p", "alpha")

        self.assertEqual(code, 0)
        self.assertEqual((result["action"], result["ok"]),
                         ("automation pause", True))
        self.assertEqual([row[1] for row in self.pauses("alpha")],
                         ["flaky runner"])

    def test_pausing_a_paused_automation_names_the_standing_pause(self):
        self.holo("automation", "pause", "main_watch", "flaky runner",
                  "-p", "alpha")

        code, err = self.holo("automation", "pause", "main_watch", "again",
                              "-p", "alpha")

        self.assertEqual(code, 1)
        self.assertIn("flaky runner", err)
        self.assertEqual([row[1] for row in self.pauses("alpha")],
                         ["flaky runner"])


if __name__ == "__main__":
    import unittest
    unittest.main()

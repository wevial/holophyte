"""`holo start` starts a project's loop unit after recording it; `holo stop`
holds the project and, with `--now`, aborts its live runs; neither stops the
unit. `systemctl` is a fake first on PATH that records its argv and what the
store held when it ran.

Run: python3 -m unittest discover -s tests -p 'test_holo_units.py' -v
"""
import io
import os
import sys
import time
from unittest.mock import patch

import store
import store.read
import store.tickets
from holophyte.config.project import Project
from holophyte.host.supervisor import reconcile_parked_pull_requests
from provider import board_for
from tests.test_holo_grammar import T0, Home, factory, holo

FAKE = """#!{python}
import sqlite3, sys
with open({calls!r}, "a") as out:
    out.write(" ".join(sys.argv[1:]) + "\\n")
conn = sqlite3.connect({db!r})
row = conn.execute("SELECT action FROM interventions"
                   " WHERE action != 'migrate' ORDER BY id DESC LIMIT 1").fetchone()
with open({seen!r}, "a") as out:
    out.write((row[0] if row else "none") + "\\n")
sys.stderr.write({error!r})
sys.exit({code})
"""


class UnitTests(Home):
    def setUp(self):
        super().setUp()
        self.path = self.repo("alpha", "ALPHA")
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "host.toml").write_text(
            f'[[project]]\npath = "{self.path}"\n')
        self.project = Project.locate(self.path)
        self.conn = store.open(str(self.project.store_path))
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "native:ALPHA", self.path)
        self.tickets = 0
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fake_systemctl()
        patcher = patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_systemctl(self, code=0, error=""):
        script = self.bin / "systemctl"
        script.write_text(FAKE.format(
            python=sys.executable, calls=str(self.root / "calls"),
            db=str(self.project.store_path), seen=str(self.root / "seen"),
            error=error, code=code))
        script.chmod(0o755)

    def calls(self):
        path = self.root / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def seen(self):
        return (self.root / "seen").read_text().splitlines()

    def holo(self, *words):
        return holo(*words, "-p", "alpha", home=self.home)

    def ticket(self):
        self.tickets += 1
        n = self.tickets
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{n}",
            linear_identifier=f"ALPHA-{n}", title=f"ticket {n}",
            acceptance_criteria=[f"Given ticket {n}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60_000,
            board_column="ready")

    def live_run(self):
        ticket = self.ticket()
        store.tickets.transition(self.conn, ticket, "in_flight")
        return store.claim(self.conn, self.project_id, ticket, now=T0)

    def interventions(self):
        return self.conn.execute(
            'SELECT "action", runId, note, guidance FROM interventions'
            " WHERE action != 'migrate' ORDER BY id").fetchall()

    def admission(self):
        return self.conn.execute(
            "SELECT admission, holdNote FROM projects WHERE id = ?",
            (self.project_id,)).fetchone()

    def stop_requests(self):
        return self.conn.execute(
            "SELECT id, stopRequested FROM runs ORDER BY id").fetchall()


class StartTests(UnitTests):
    def test_start_records_the_launch_before_systemctl_starts_the_unit(self):
        self.ticket()

        done = self.holo("start")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls(), ["--user start holophyte-loop@alpha"])
        self.assertEqual(self.seen(), ["launch_loop"])
        action, run, note, _ = self.interventions()[-1]
        self.assertEqual((action, run), ("launch_loop", None))
        self.assertIn("holo start", note)
        self.assertIn("holophyte-loop@alpha", done.stdout)
        self.assertIn("1 ticket ready", done.stdout)

    def test_a_failed_systemctl_exits_1_with_its_error_and_keeps_the_record(self):
        self.fake_systemctl(code=1, error="Failed to connect to bus\n")

        done = self.holo("start")

        self.assertEqual(done.returncode, 1)
        self.assertIn("Failed to connect to bus", done.stderr)
        self.assertEqual(self.calls(), ["--user start holophyte-loop@alpha"])
        self.assertEqual(self.interventions()[-1][0], "launch_loop")

    def test_a_held_project_without_a_note_is_refused_naming_the_hold(self):
        store.hold(self.conn, self.project_id, "disk replacement")
        before = self.interventions()

        done = self.holo("start")

        self.assertEqual(done.returncode, 1)
        self.assertIn("held: disk replacement", done.stderr)
        self.assertEqual(self.interventions(), before)
        self.assertEqual(self.admission(), ("held", "disk replacement"))
        self.assertEqual(self.calls(), [])

    def test_a_note_releases_the_hold_before_the_launch(self):
        store.hold(self.conn, self.project_id, "disk replacement")

        done = self.holo("start", "back after maintenance")

        self.assertEqual(done.returncode, 0, done.stderr)
        (released, _, note, _), (launched, _, _, _) = self.interventions()[-2:]
        self.assertEqual((released, note, launched),
                         ("release_hold", "back after maintenance", "launch_loop"))
        self.assertEqual(self.admission(), ("enabled", None))
        self.assertEqual(self.calls(), ["--user start holophyte-loop@alpha"])

    def test_a_registered_project_with_no_serve_name_is_refused_naming_it(self):
        config = self.project.config_path
        config.write_text(config.read_text().replace('name = "alpha"',
                                                     'name = ""'))

        done = holo("start", "-p", str(self.path), home=self.home)

        self.assertEqual(done.returncode, 1)
        self.assertIn(f"the {self.home / 'host.toml'} entry {self.path} has no"
                      " [serve] name", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_foreground_is_the_loop_entry_and_never_the_unit(self):
        store.tickets.set_admission(self.conn, self.project_id, "disabled",
                                    "retired for now")

        done = self.holo("start", "--foreground")
        loop = factory(str(self.path), home=self.home)

        self.assertEqual(done.returncode, loop.returncode, done.stderr)
        line = f"[holo2] project {self.path} disabled: retired for now"
        self.assertIn(line, done.stdout)
        self.assertEqual(done.stdout, loop.stdout)
        self.assertEqual(self.calls(), [])


class StopTests(UnitTests):
    def test_stop_holds_the_project_and_names_the_run_it_waits_for(self):
        run = self.live_run()

        done = self.holo("stop", "pausing intake")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.admission(), ("held", "pausing intake"))
        self.assertIn(f"run {run} (ALPHA-1)", done.stdout)
        self.assertEqual(self.stop_requests(), [(run, None)])
        self.assertEqual(self.calls(), [])

    def test_the_sweep_launches_no_loop_for_a_stopped_project(self):
        self.ticket()
        out = io.StringIO()
        now = int(time.time() * 1000)

        reconcile_parked_pull_requests(self.project, self.conn, now,
                                       board_for(self.project), out)
        self.assertEqual(self.calls(), ["--user start holophyte-loop@alpha"],
                         out.getvalue())
        done = self.holo("stop", "pausing intake")
        reconcile_parked_pull_requests(self.project, self.conn, now + 60_000,
                                       board_for(self.project), out)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.calls(), ["--user start holophyte-loop@alpha"])

    def test_stop_on_a_project_with_no_store_yet_holds_it(self):
        self.conn.close()
        path = self.project.store_path
        for each in path.parent.glob(path.name + "*"):
            each.unlink()

        done = self.holo("stop", "maintenance")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("no run is live", done.stdout)
        conn = store.read.open_readonly(path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute(
            "SELECT admission, holdNote FROM projects").fetchall(),
            [("held", "maintenance")])
        self.assertEqual(self.calls(), [])

    def test_stop_holds_a_disabled_project_as_the_hold_verb_does(self):
        store.tickets.set_admission(self.conn, self.project_id, "disabled",
                                    "retired for now")

        done = self.holo("stop", "new reason")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.admission(), ("held", "new reason"))
        self.assertEqual(self.interventions()[-1][::2], ("hold", "new reason"))

    def test_stop_now_holds_and_aborts_each_live_run_with_the_note(self):
        first, second = self.live_run(), self.live_run()

        done = self.holo("stop", "--now", "stop for the deploy")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.admission(), ("held", "stop for the deploy"))
        aborts = {run: guidance for action, run, _, guidance
                  in self.interventions() if action == "abort"}
        self.assertEqual(aborts, {first: "stop for the deploy",
                                  second: "stop for the deploy"})
        requested = dict(self.conn.execute(
            "SELECT runId, id FROM interventions WHERE action = 'abort'"))
        self.assertEqual(self.stop_requests(),
                         [(first, requested[first]), (second, requested[second])])
        self.assertIn(f"run {first} (ALPHA-1)", done.stdout)
        self.assertIn(f"run {second} (ALPHA-2)", done.stdout)
        self.assertEqual(self.calls(), [])

    def test_stop_without_a_note_or_with_a_blank_one_exits_2_and_changes_nothing(self):
        self.live_run()
        self.live_run()
        before = (self.admission(), self.interventions(), self.stop_requests())
        for words in (("stop",), ("stop", "--now"), ("stop", "  "),
                      ("stop", "--now", "  ")):
            with self.subTest(words=words):
                done = self.holo(*words)

                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertEqual((self.admission(), self.interventions(),
                                  self.stop_requests()), before)

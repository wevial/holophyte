"""`holo patrol`: each project's attention groups, runs in flight, stranded
tickets, quiet runs and repeat failures, read from real stores.

Run: python3 -m unittest discover -s tests -p 'test_patrol.py' -v
"""
import contextlib
import io
import json
import os
from unittest.mock import patch

import holophyte.cli.patrol
import store
import store.tickets
from holophyte.config.project import Project
from holophyte.holo import cli as holo_cli
from holophyte.serve.views import attention
from tests.host_fixture import HostFixture
from tests.phase_fixture import park_run
from tests.test_status import file_stuck_wait

NOW = 1_800_000_000_000
MIN = 60_000
TABLES = ("runs", "tickets", "runEvents", "interventions", "steerNotes")


class PatrolFixture(HostFixture):
    def setUp(self):
        super().setUp()
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("HOLO_PROJECT", None)
        clock = patch.object(holophyte.cli.patrol, "time",
                             return_value=NOW / 1000)
        clock.start()
        self.addCleanup(clock.stop)
        self.conns = {}

    def project(self, name):
        """A registered project `name` with an empty store, its supervisor
        beating now; its store connection."""
        path = self.repo(name, name=name)
        self.cli("project", "add", str(path))
        conn = store.open(str(Project.locate(path).store_path))
        self.addCleanup(conn.close)
        self.conns[name] = (conn, store.ensure_project(conn, f"team-{name}", path),
                            path)
        store.record_supervisor_heartbeat(conn, 4242, NOW - MIN, now=NOW)
        return conn

    def ticket(self, name, key):
        conn, project_id, _ = self.conns[name]
        return store.tickets.mirror_ticket(
            conn, project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it works"],
            verification_commands=["echo ok"])

    def claim(self, name, ticket, at):
        conn, project_id, _ = self.conns[name]
        run = store.claim(conn, project_id, ticket, now=at)
        store.set_phase(conn, run, "working", now=at)
        return run

    def ended(self, name, key, outcomes, status="in_flight"):
        """`key` with one run per `(outcome, kind, reason)`, oldest first."""
        conn = self.conns[name][0]
        ticket = self.ticket(name, key)
        if status == "in_flight":
            store.tickets.transition(conn, ticket, "in_flight")
        runs = []
        for n, (outcome, kind, reason) in enumerate(outcomes):
            at = NOW - (60 - 10 * n) * MIN
            runs.append(self.claim(name, ticket, at))
            store.release(conn, runs[-1], outcome, reason, failure_kind=kind,
                          now=at + 5 * MIN)
        return runs

    def live(self, name, key, heartbeat, event=None):
        conn = self.conns[name][0]
        ticket = self.ticket(name, key)
        store.tickets.transition(conn, ticket, "in_flight")
        run = self.claim(name, ticket, heartbeat)
        if event is not None:
            store.record_event(conn, run, "note", "still going", now=event)
        return run

    def stranded_project(self, name):
        """A failed, B killed, C in flight with no run, D live."""
        self.project(name)
        self.runs = {"A": self.ended(name, "A", [("failed", "verify", "red")])[0],
                     "B": self.ended(name, "B", [("killed", None, "swept")])[0]}
        store.tickets.transition(self.conns[name][0], self.ticket(name, "C"),
                                 "in_flight")
        self.runs["D"] = self.live(name, "D", NOW - MIN)

    def configure(self, name, text):
        path = Project.locate(self.conns[name][2]).config_path
        path.write_text(path.read_text() + "[patrol]\n" + text + "\n")

    def holo(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = holo_cli.main(["patrol", *argv])
        return code, out.getvalue()

    def patrol(self, name):
        code, text = self.holo("--json", "-p", name)
        self.assertEqual(code, 0, text)
        (row,) = json.loads(text)["projects"]
        return row

    def footprint(self, name):
        conn = self.conns[name][0]
        counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in TABLES]
        return counts, conn.execute("SELECT MAX(id) FROM runEvents").fetchone()


class StrandedTests(PatrolFixture):
    def test_every_in_flight_ticket_whose_newest_run_ended_or_never_ran(self):
        self.stranded_project("q")
        row = self.patrol("q")
        self.assertEqual(
            [(entry["ticket"], entry["run"], entry["outcome"])
             for entry in row["stranded"]],
            [("A", self.runs["A"], "failed"), ("B", self.runs["B"], "killed"),
             ("C", None, None)])
        _, text = self.holo("-p", "q")
        stranded = [line.split()[1] for line in text.splitlines()
                    if line.strip().startswith("stranded ")]
        self.assertEqual(stranded, ["A", "B", "C"])

    def test_an_ignored_ticket_is_in_no_section_and_listed_as_ignored(self):
        self.stranded_project("q")
        self.configure("q", 'ignore = ["A"]')
        row = self.patrol("q")
        self.assertNotIn('"A"', json.dumps(
            {key: value for key, value in row.items() if key != "ignored"}))
        self.assertEqual(row["ignored"], ["A"])

    def test_a_project_whose_only_entry_is_ignored_is_one_clean_line(self):
        self.project("solo")
        self.ended("solo", "A", [("failed", "verify", "red")])
        self.configure("solo", 'ignore = ["A"]')
        self.assertTrue(self.patrol("solo")["clean"])
        self.assertEqual(self.holo("-p", "solo"),
                         (0, "[solo] nothing to do · ignored A\n"))


class QuietTests(PatrolFixture):
    def test_a_run_is_quiet_when_heartbeat_and_newest_event_are_both_old(self):
        self.project("q")
        runs = [self.live("q", "R1", NOW - 25 * MIN),
                self.live("q", "R2", NOW - 25 * MIN, event=NOW - 5 * MIN),
                self.live("q", "R3", NOW - MIN)]
        row = self.patrol("q")
        self.assertEqual(row["quiet"], [
            {"ticket": "R1", "run": runs[0], "phase": "working",
             "heartbeat_age_ms": 25 * MIN, "event_age_ms": 25 * MIN}])
        self.assertEqual(
            [(run["ticket"], run["run"], run["phase"], run["heartbeat_age_ms"])
             for run in row["in_flight"]],
            [("R1", runs[0], "working", 25 * MIN),
             ("R2", runs[1], "working", 25 * MIN),
             ("R3", runs[2], "working", MIN)])
        self.configure("q", "quiet_min = 30")
        self.assertEqual(self.patrol("q")["quiet"], [])


class RepeatTests(PatrolFixture):
    def test_two_newest_failures_alike_but_for_digits_repeat(self):
        self.project("q")
        e = self.ended("q", "E", [("failed", "verify", "3 tests failed in 41s"),
                                  ("failed", "verify", "12 tests failed in 9s")],
                       status="ready")
        self.ended("q", "F", [("failed", "verify", "red"),
                              ("failed", "budget", "red")], status="ready")
        self.ended("q", "G", [("killed", None, "red"),
                              ("failed", "verify", "red")], status="ready")
        row = self.patrol("q")
        self.assertEqual(
            [(entry["ticket"], entry["runs"], entry["failure_kind"])
             for entry in row["repeat_failures"]],
            [("E", e[::-1], "verify")])
        self.configure("q", "repeat_runs = 3")
        self.assertEqual(self.patrol("q")["repeat_failures"], [])


class AttentionTests(PatrolFixture):
    def park(self, key, kind, at):
        conn = self.conns["q"][0]
        ticket = self.ticket("q", key)
        store.tickets.transition(conn, ticket, "in_flight")
        run = self.claim("q", ticket, at)
        park_run(conn, run, "awaiting_merge_approval", park_kind=kind, now=at)
        store.tickets.transition(conn, ticket, "blocked_on_operator")
        store.set_question(conn, ticket, f"{kind} waits on you\nmore")

    def test_groups_hold_the_attention_items_in_first_appearance_order(self):
        conn = self.project("q")
        self.park("T1", "thread", NOW - 9 * MIN)
        self.park("T2", "contradiction", NOW - 8 * MIN)
        file_stuck_wait(conn, self.conns["q"][1], "NAT")
        row = self.patrol("q")
        _, body = attention(Project.locate(self.conns["q"][2]), NOW)
        groups = [group["group"] for group in row["attention"]]
        self.assertEqual(groups, ["blocked:thread", "blocked:contradiction",
                                  "stuck_wait:needs_spec"])
        self.assertEqual([item for group in row["attention"]
                          for item in group["items"]], body["items"])


class ConfigTests(PatrolFixture):
    def test_a_wrong_patrol_value_exits_naming_the_key_and_writes_nothing(self):
        self.stranded_project("q")
        path = str(self.conns["q"][2])
        config = Project.locate(path).config_path
        text = config.read_text()
        for value, named in (("quiet_min = 0", "quiet_min"),
                             ("repeat_runs = 1", "repeat_runs"),
                             ('ignore = "A"', "ignore"),
                             ("window = 5", "window")):
            with self.subTest(value=value):
                config.write_text(text + "[patrol]\n" + value + "\n")
                before = list(self.conns["q"][0].iterdump())
                with self.assertRaises(SystemExit) as raised:
                    self.cli("--patrol", path)
                self.assertIn(f"[patrol] {named}", str(raised.exception))
                if named == "window":
                    self.assertIn("ignore, quiet_min, repeat_runs",
                                  str(raised.exception))
                self.assertEqual(list(self.conns["q"][0].iterdump()), before)


class HostFormTests(PatrolFixture):
    def test_host_form_lists_each_project_and_a_lost_store_is_its_error(self):
        self.project("p")
        self.live("p", "P1", NOW - MIN)
        self.stranded_project("q")
        code, text = self.holo()
        self.assertEqual(code, 0, text)
        lines = [line for line in text.splitlines()
                 if not line.startswith("[host]")]
        self.assertEqual(lines[0], "[p] nothing to do · 1 in flight")
        self.assertEqual(lines[1], "[q]")
        code, text = self.holo("--json")
        snap = json.loads(text)
        self.assertEqual((snap["form"], [row["name"] for row in snap["projects"]]),
                         ("host", ["p", "q"]))
        p_row = snap["projects"][0]
        self.assertTrue(p_row["clean"])
        self.conns["q"][0].close()
        Project.locate(self.conns["q"][2]).store_path.unlink()
        code, text = self.holo("--json")
        snap = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual(snap["projects"][0], p_row)
        self.assertTrue(snap["projects"][1]["error"])


class ReadOnlyTests(PatrolFixture):
    def test_patrol_changes_no_row_of_any_store(self):
        self.stranded_project("a")
        self.project("b")
        self.live("b", "R1", NOW - 25 * MIN)
        self.project("c")
        self.ended("c", "E", [("failed", "verify", "red 1"),
                              ("failed", "verify", "red 2")], status="ready")
        before = {name: self.footprint(name) for name in self.conns}
        for argv in ((), ("--json",)):
            self.holo(*argv)
            for name in self.conns:
                self.holo(*argv, "-p", name)
        self.assertEqual({name: self.footprint(name) for name in self.conns},
                         before)

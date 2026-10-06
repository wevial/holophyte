"""`holo status` without `--json`: a page that leads with what needs a
person, in the client's zone, with locks only when one is held or stale.

Run: python3 -m unittest discover -s tests -p 'test_holo_status_page.py' -v
"""
import io
import json
import os
import pty
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import holophyte.cli.status
import holophyte.holo.status_page
import store
import store.tickets
import tests.test_holo_results as results_tests
from holophyte.cli.status import host_snapshot
from holophyte.config.project import Project
from holophyte.holo import cli as holo_cli
from holophyte.holo.status_page import page
from holophyte.host.registry import Host
from holophyte.host.supervisor_lock import supervisor_lock_path
from tests.host_fixture import HostFixture
from tests.test_holo_grammar import ROOT, factory, holo

NOW = int(datetime(2026, 10, 6, 17, 42, tzinfo=timezone.utc).timestamp() * 1000)
MINUTE = 60_000
QUESTION = "PR #2281 ready to merge, waiting on you"
REASON = "review asked for changes; sent back"
SHA = "92ef2b0c4d5e6f708192a3b4c5d6e7f8091a2b3c"
ESCAPE = "\x1b"
PR_URL = "https://example.invalid/org/repo/pull/2281"


def on_tty(argv, env):
    """`argv` run with its stdout a pseudo-terminal; its exit and output."""
    master, slave = pty.openpty()
    with tempfile.TemporaryFile() as errors:
        child = subprocess.Popen(argv, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                 stdout=slave, stderr=errors)
        os.close(slave)
        chunks = []
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        child.wait()
    os.close(master)
    return child.returncode, b"".join(chunks).decode()


def holo_env(home, **extra):
    return {**os.environ, "HOLOPHYTE_HOME": str(home), "PYTHONPATH": str(ROOT),
            **extra}


class StatusPageTests(HostFixture):
    """Two registered projects: alpha with a ticket parked on its pull request
    and a stranded run, beta with nothing to do."""

    def setUp(self):
        super().setUp()
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        for name in ("HOLO_PROJECT", "NO_COLOR"):
            os.environ.pop(name, None)
        self.alpha = self.repo("alpha", name="alpha")
        self.beta = self.repo("beta", name="beta")
        for path in (self.alpha, self.beta):
            self.cli("project", "add", str(path))
        self.conn = store.open(str(Project.locate(self.alpha).store_path))
        self.addCleanup(self.conn.close)
        self.project_id = store.ensure_project(self.conn, "team-alpha", self.alpha)
        self.park("ALPHA-14", NOW - 90 * MINUTE)
        self.strand("ALPHA-9", NOW - 14 * MINUTE)

    def ticket(self, key):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)
        store.tickets.transition(self.conn, ticket, "in_flight")
        return ticket

    def park(self, key, at):
        ticket = self.ticket(key)
        run = store.claim(self.conn, self.project_id, ticket, now=at)
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, run, phase, now=at)
        store.park(self.conn, run, "awaiting_merge_approval", pr_url=PR_URL)
        store.tickets.transition(self.conn, ticket, "blocked_on_operator")
        store.set_question(self.conn, ticket, QUESTION)

    def strand(self, key, ended):
        run = store.claim(self.conn, self.project_id, self.ticket(key),
                          now=ended - 30 * MINUTE)
        store.release(self.conn, run, "failed", reason=REASON, now=ended)

    def go_live(self, key, heartbeat):
        run = store.claim(self.conn, self.project_id, self.ticket(key),
                          now=heartbeat)
        store.set_phase(self.conn, run, "working", now=heartbeat)

    def write_sweep(self, ended):
        (self.home / "sweep.json").write_text(json.dumps(
            {"started": ended - 5000, "ended": ended, "exit": 0,
             "revision": SHA, "projects": {"alpha": "ok", "beta": "ok"}}))

    def snapshot(self):
        return host_snapshot(Host.locate(), now=NOW)

    def page(self, snap=None):
        return page(snap or self.snapshot(), NOW, ZoneInfo("UTC"))


class PageOrderTests(StatusPageTests):
    def test_needs_you_comes_first_with_the_parked_and_stranded_tickets(self):
        self.go_live("ALPHA-20", NOW - 20_000)
        self.write_sweep(NOW - 40_000)

        lines = self.page()

        headings = ["Needs you (2)", "Running (1)", "Quiet"]
        at = [lines.index(heading) for heading in headings]
        footer = next(index for index, line in enumerate(lines)
                      if line.startswith("Sweep "))
        self.assertEqual(at + [footer], sorted(at + [footer]))
        needs = lines[at[0] + 1:at[1]]
        self.assertTrue(any("ALPHA-14" in line and QUESTION in line
                            for line in needs), needs)
        self.assertTrue(any("ALPHA-9" in line and REASON in line
                            for line in needs), needs)
        running = lines[at[1] + 1:at[2]]
        self.assertIn("ALPHA-20", running[0])
        self.assertTrue(running[0].endswith("working · heartbeat 20 s ago"),
                        running[0])
        self.assertEqual(lines[at[2] + 1], "  ✓  beta   nothing ready")

    def test_the_client_zone_shows_clock_time_and_the_stranded_wait(self):
        (self.home / "client.toml").write_text('timezone = "America/Los_Angeles"\n')
        self.write_sweep(NOW - 40_000)
        out = io.StringIO()
        with patch.object(holophyte.holo.status_page, "time", lambda: NOW / 1000), \
                patch.object(holophyte.cli.status, "time", lambda: NOW / 1000), \
                redirect_stdout(out):
            code = holo_cli.main(["status"])

        self.assertEqual(code, 0, out.getvalue())
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].endswith(" · Tue Oct 6, 10:42 PDT"), lines[0])
        [stranded] = [line for line in lines if "ALPHA-9" in line]
        self.assertTrue(stranded.endswith(f"{REASON}  14 min"), stranded)


class LockAndBuildTests(StatusPageTests):
    def test_free_locks_print_no_lock_line(self):
        snap = self.snapshot()
        alpha = snap["projects"][0]["store"]
        self.assertEqual((alpha["supervisor_lock"], alpha["merge_lock"]),
                         (None, None))

        text = "\n".join(self.page(snap))

        self.assertNotIn("lock", text)

    def test_a_dead_supervisor_pid_is_a_problem_with_a_hint_not_a_quiet_line(self):
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        supervisor_lock_path(Project.locate(self.beta)).write_text(
            f"host-a {dead.pid} {NOW}\n")

        lines = self.page()

        problems = lines[lines.index("Problems (1)") + 1:]
        self.assertTrue(problems[0].startswith(
            f"  ✗  beta   supervisor lock names dead pid {dead.pid}  try: "),
            problems[0])
        self.assertNotIn("Quiet", lines)

    def test_the_footer_shows_the_build_as_its_first_seven_hex_digits(self):
        self.write_sweep(NOW - 40_000)
        snap = self.snapshot()
        snap["build"]["head"] = SHA

        self.assertEqual(self.page(snap)[-1], "Sweep ok 40 s ago · build 92ef2b0")


class SweepProblemTests(StatusPageTests):
    def test_a_sweep_error_naming_a_project_replaces_its_quiet_line(self):
        (self.home / "sweep.json").write_text(json.dumps(
            {"started": NOW - 45_000, "ended": NOW - 40_000, "exit": 1,
             "projects": {"alpha": "ok", "beta": "error: store locked"}}))

        snap = self.snapshot()
        snap["build"]["head"] = SHA

        lines = self.page(snap)

        self.assertNotIn("Quiet", lines)
        self.assertIn("  ✗  beta   sweep: error: store locked  try: journalctl"
                      " --user -u holophyte-sweep.service -n 200", lines)
        self.assertEqual(lines[-1], "Sweep failed 40 s ago · build 92ef2b0")

    def test_a_sweep_that_started_long_ago_and_never_ended_is_killed(self):
        (self.home / "sweep.json").write_text(json.dumps(
            {"started": NOW - 94 * 60 * MINUTE, "ended": None, "exit": None}))

        lines = self.page()

        problems = lines[lines.index("Problems (1)") + 1]
        self.assertTrue(problems.startswith(
            "  ✗  sweep  the sweep started 94 h ago never ended  try: "), problems)
        self.assertTrue(lines[-1].startswith("Sweep killed, started 94 h ago · "),
                        lines[-1])


class ColourTests(StatusPageTests):
    def setUp(self):
        super().setUp()
        self.go_live("ALPHA-20", int(datetime.now(timezone.utc).timestamp() * 1000))

    def test_piped_the_symbols_stay_and_no_escape_is_printed(self):
        completed = holo("status", home=self.home)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(ESCAPE, completed.stdout)
        for mark in ("  !  alpha", "  >  alpha", "  ✓  beta"):
            self.assertIn(mark, completed.stdout)

    def test_on_a_terminal_the_symbols_are_coloured(self):
        argv = [sys.executable, "-m", "holophyte.holo", "status"]
        code, printed = on_tty(argv, holo_env(self.home))

        self.assertEqual(code, 0, printed)
        for coloured in ("\x1b[38;5;208m!\x1b[0m", "\x1b[34m>\x1b[0m",
                         "\x1b[32m✓\x1b[0m"):
            self.assertIn(coloured, printed)

    def test_no_color_on_a_terminal_turns_the_colour_off(self):
        argv = [sys.executable, "-m", "holophyte.holo", "status"]
        code, printed = on_tty(argv, holo_env(self.home, NO_COLOR="1"))

        self.assertEqual(code, 0, printed)
        self.assertNotIn(ESCAPE, printed)
        self.assertIn("✓  beta", printed)


class JsonAndConfigTests(StatusPageTests):
    def test_json_is_the_factory_status_json(self):
        ours = holo("status", "--json", home=self.home)
        theirs = factory("--status", "--json", home=self.home)

        self.assertEqual((ours.returncode, theirs.returncode), (0, 0), ours.stderr)
        self.assertEqual(json.loads(ours.stdout), json.loads(theirs.stdout))

    def test_an_unknown_zone_exits_2_naming_the_key_and_the_value(self):
        (self.home / "client.toml").write_text('timezone = "Mars/Olympus"\n')

        completed = holo("status", home=self.home)

        self.assertEqual(completed.returncode, 2, completed.stdout)
        self.assertIn("timezone", completed.stderr)
        self.assertIn("'Mars/Olympus'", completed.stderr)


class WriteResultColourTests(results_tests.ResultTests):
    def setUp(self):
        super().setUp()
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("NO_COLOR", None)

    def test_on_a_terminal_the_tick_is_green_and_piped_it_is_plain(self):
        argv = [sys.executable, "-m", "holophyte.holo", "pause", "HOLO-1",
                "lunch", "-p", str(self.path)]
        code, printed = on_tty(argv, holo_env(self.home))
        self.assertEqual(code, 0, printed)
        self.assertTrue(printed.startswith("\x1b[32m✓\x1b[0m HOLO-1"), printed)

        piped = self.holo("pause", "HOLO-1", "lunch")
        self.assertEqual(piped.returncode, 0, piped.stderr)
        self.assertTrue(piped.stdout.startswith("✓ "), piped.stdout)
        self.assertNotIn(ESCAPE, piped.stdout)

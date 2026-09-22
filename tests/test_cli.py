"""`factory.py TARGET --repoint KO-n SHA --note TEXT`: a parked candidate
moved to a rebuilt branch tip, through the command line (KO-297).

The mode is the store's `repoint()` behind argparse, so what is tested here
is the wiring: `--note` is required the way `--requeue` requires it and the
refusal never reaches the store, the two shas are printed on success, and a
store refusal is a non-zero exit naming the ticket with nothing written.

Run: python3 -m unittest discover -s tests -p 'test_cli.py' -v
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import holophyte.target
import store
import store.tickets

MINUTE = 60 * 1000
T0 = 1_700_000_000_000
OLD_SHA = "0bbd7e6100000000000000000000000000000000"
NEW_SHA = "7af190c000000000000000000000000000000000"


class RepointFlagTests(unittest.TestCase):
    """A target with a store holding one ticket and its parked run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.dict(os.environ,
                             {"HOLOPHYTE_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.target = holophyte.target.Target.locate(self.repo)
        self.target.store_path.parent.mkdir(parents=True, exist_ok=True)
        conn = store.open(self.target.store_path, migrate="owner")
        self.addCleanup(conn.close)
        self.conn = conn
        self.project = store.tickets.ensure_project(conn, "team-1", self.repo)
        self.ticket = store.tickets.mirror_ticket(
            conn, self.project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)
        store.tickets.transition(conn, self.ticket, "in_flight")
        self.run = store.claim(conn, self.project, self.ticket, now=T0)
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(conn, self.run, phase, now=T0 + MINUTE)

    def park(self):
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")
        store.park(self.conn, self.run, "awaiting_merge_approval",
                   candidate_sha=OLD_SHA, now=T0 + 2 * MINUTE)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            holophyte.cli.cli([str(self.repo), *args])
        return out.getvalue(), err.getvalue()

    def candidate_sha(self):
        return self.conn.execute(
            "SELECT candidateSha FROM runs WHERE id = ?",
            (self.run,)).fetchone()[0]

    def test_project_commands_list_and_admission(self):
        def command(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                holophyte.cli.cli(["project", *args, "--store",
                                   str(self.target.store_path)])
            return out.getvalue()
        store.ensure_project(self.conn, "team-2", self.root / "aaa")
        third = store.ensure_project(self.conn, "team-3", self.root / "zzz")
        command("hold", "aaa", "--note", "waiting")
        command("disable", "zzz", "--note", "retired")
        lines = command("list").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("aaa", lines[0])
        self.assertIn("held", lines[0])
        self.assertIn("waiting", lines[0])
        self.assertIn("enabled", lines[1])
        self.assertIn(str(self.run), lines[1])
        self.assertIn("disabled", lines[2])
        self.assertIn("retired", lines[2])
        command("enable", "zzz")
        self.assertEqual(self.conn.execute(
            "SELECT admission FROM projects WHERE id = ?", (third,)
        ).fetchone(), ("enabled",))

    def test_project_add_registers_once_without_runs(self):
        repo = self.root / "fresh"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        target = holophyte.target.Target.locate(repo)
        with self.assertRaisesRegex(SystemExit, "configuration naming a team"):
            holophyte.cli.cli(["project", "add", str(repo)])
        self.assertFalse(target.store_path.exists())
        with self.assertRaisesRegex(SystemExit, "not a repository root"):
            holophyte.cli.cli(["project", "add", str(self.root / "missing")])
        target.holo_dir.mkdir(parents=True)
        target.config_path.write_text('[board]\nteam = "fresh-team"\n'
                                      'project_id = "fresh-project"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            holophyte.cli.cli(["project", "add", str(repo)])
        conn = open_store(target)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute(
            "SELECT repoPath, admission FROM projects").fetchall(),
            [(str(repo), "enabled")])
        self.assertEqual(conn.execute("SELECT count(*) FROM runs").fetchone(), (0,))
        self.assertEqual(conn.execute(
            "SELECT action FROM interventions WHERE projectId IS NOT NULL"
        ).fetchall(), [("register_project",)])
        with self.assertRaisesRegex(SystemExit, "project 1 already registered"):
            holophyte.cli.cli(["project", "add", str(repo)])

    def test_note_is_required(self):
        self.park()
        with patch.object(store, "repoint") as repoint:
            with self.assertRaises(SystemExit) as raised:
                self.cli("--repoint", "KO-1", NEW_SHA)
        # argparse's usage exit, the one `--requeue` without a note takes,
        # before any store is opened.
        self.assertEqual(raised.exception.code, 2)
        repoint.assert_not_called()
        self.assertEqual(self.candidate_sha(), OLD_SHA)

    def test_repoint_moves_the_sha_and_prints_both(self):
        self.park()
        out, _ = self.cli("--repoint", "KO-1", NEW_SHA,
                          "--note", "rebased onto the filtered main")
        self.assertIn(OLD_SHA, out)
        self.assertIn(NEW_SHA, out)
        self.assertEqual(self.candidate_sha(), NEW_SHA)
        self.assertEqual(
            self.conn.execute('SELECT runId, "action", guidance'
                              ' FROM interventions'
                              " WHERE action != 'migrate'").fetchall(),
            [(self.run, "repoint", "rebased onto the filtered main")])

    def test_a_refusal_exits_non_zero_naming_the_ticket_and_writes_nothing(self):
        # Not parked: the run is live at the gate.
        with self.assertRaises(SystemExit) as raised:
            self.cli("--repoint", "KO-1", NEW_SHA, "--note", "rebased")
        self.assertIn("KO-1", str(raised.exception))
        self.assertIsNone(self.candidate_sha())
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM interventions'
                              " WHERE action != 'migrate'")
            .fetchone(), (0,))


class HoldFlagTests(unittest.TestCase):
    setUp = RepointFlagTests.setUp
    cli = RepointFlagTests.cli

    def test_hold_release_record_before_write_and_refuse_repeat(self):
        import time

        self.conn.executescript("""
            CREATE TABLE admissionAudit (state TEXT, action TEXT, note TEXT);
            CREATE TRIGGER admission_record BEFORE UPDATE OF admission ON projects
            BEGIN
                INSERT INTO admissionAudit SELECT OLD.admission, action, note
                FROM interventions WHERE projectId = OLD.id ORDER BY id DESC LIMIT 1;
            END;
        """)
        before = int(time.time() * 1000)
        out, _ = self.cli("--hold", "--note", "reboot pending")
        self.assertIn("held: reboot pending", out)
        with self.assertRaisesRegex(SystemExit, "already held: reboot pending"):
            self.cli("--hold", "--note", "different reason")
        self.cli("--release-hold", "--note", "reboot complete")
        after = int(time.time() * 1000)
        rows = self.conn.execute(
            "SELECT action, note, at FROM interventions "
            "WHERE action IN ('hold', 'release_hold') ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [(a, n) for a, n, _ in rows],
            [("hold", "reboot pending"), ("release_hold", "reboot complete")],
        )
        self.assertTrue(all(before <= at <= after for _, _, at in rows))
        self.assertEqual(
            self.conn.execute("SELECT * FROM admissionAudit").fetchall(),
            [
                ("enabled", "hold", "reboot pending"),
                ("held", "release_hold", "reboot complete"),
            ],
        )
        self.assertEqual(
            self.conn.execute("SELECT admission, holdNote FROM projects").fetchone(),
            ("enabled", None),
        )

    def test_admission_requires_nonempty_note(self):
        for flag in ("--hold", "--release-hold"):
            for note in ([], ["--note", " "]):
                with (
                    self.subTest(flag=flag, note=note),
                    self.assertRaises(SystemExit) as raised,
                ):
                    self.cli(flag, *note)
                self.assertEqual(raised.exception.code, 2)

if __name__ == "__main__":
    unittest.main()

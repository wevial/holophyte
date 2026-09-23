"""`factory.py TARGET --repoint KO-n SHA --note TEXT`: a parked candidate
moved to a rebuilt branch tip, through the command line (KO-297).

Run: python3 -m unittest discover -s tests -p 'test_cli.py' -v
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import holophyte.board
import holophyte.cli
import holophyte.project
import holophyte.supervisor
import store
import store.tickets
from holophyte.runs import open_store

MINUTE = 60 * 1000
T0 = 1_700_000_000_000
OLD_SHA = "0bbd7e6100000000000000000000000000000000"
NEW_SHA = "7af190c000000000000000000000000000000000"
ROOT = Path(__file__).resolve().parent.parent


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
        self.target = holophyte.project.Project.locate(self.repo)
        self.target.store_path.parent.mkdir(parents=True, exist_ok=True)
        conn = store.open(self.target.store_path, migrate="owner")
        self.addCleanup(conn.close)
        self.conn = conn
        self.project_id = store.tickets.ensure_project(conn, "team-1", self.repo)
        self.ticket = store.tickets.mirror_ticket(
            conn, self.project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * MINUTE)
        store.tickets.transition(conn, self.ticket, "in_flight")
        self.run = store.claim(conn, self.project_id, self.ticket, now=T0)
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

    def board_cli(self, board, *args):
        """`cli()` on a target with a `[board]` table, `board` its provider."""
        self.target.holo_dir.mkdir(parents=True, exist_ok=True)
        self.target.config_path.write_text('[board]\nteam = "team-1"\n'
                                           'project_id = "project-1"\n')
        with patch.object(holophyte.cli, "LinearProvider", return_value=board):
            return self.cli(*args)

    def candidate_sha(self):
        return self.conn.execute(
            "SELECT candidateSha FROM runs WHERE id = ?",
            (self.run,)).fetchone()[0]

    def test_pause_requires_note_and_refuses_ended_outcome(self):
        with self.assertRaises(SystemExit):
            self.cli("--pause", "KO-1")
        out, _ = self.cli("--pause", "KO-1", "--note", "reboot writer")
        self.assertIn("pause requested", out)
        store.release(self.conn, self.run, "failed")
        count = self.conn.execute("SELECT COUNT(*) FROM interventions").fetchone()
        with self.assertRaisesRegex(SystemExit, "outcome failed"):
            self.cli("--pause", "KO-1", "--note", "too late")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM interventions").fetchone(), count)

    def test_abort_ends_a_run_whose_worker_is_gone_and_not_a_live_one(self):
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.repo, check=True,
                                  capture_output=True, text=True).stdout
        git("init", "-q", "-b", "main")
        git("-c", "user.email=t@example.invalid", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "base")
        wt = holophyte.project.worktree_path(self.target, "task/ko-1")
        git("worktree", "add", "-q", "-b", "task/ko-1", str(wt))
        (wt / "edit.txt").write_text("unsaved\n")
        store.set_branch(self.conn, self.run, "task/ko-1")
        board = Mock()

        def abort():
            return self.board_cli(board, "--abort", "KO-1",
                                  "--note", "host going down")[0]
        # The claim recorded this live process as the worker; it just beat.
        store.heartbeat(self.conn, self.run)
        self.assertIn("abort requested", abort())
        # A stale beat does not prove a live worker dead, nor does a dead pid
        # on a host this one cannot ask: neither commits under the writer.
        self.conn.execute("UPDATE runs SET lastHeartbeat = 0")
        self.conn.commit()
        self.assertIn("abort requested", abort())
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.conn.execute("UPDATE runs SET workerPid = ?, host = 'elsewhere'",
                          (dead.pid,))
        self.conn.commit()
        self.assertIn("abort requested", abort())
        self.assertIsNone(self.conn.execute("SELECT endedAt FROM runs").fetchone()[0])
        self.assertEqual(git("log", "-1", "--format=%s", "task/ko-1").strip(), "base")
        board.set_state.assert_not_called()
        # A worker on this host that beat a moment ago and then died.
        self.conn.execute("UPDATE runs SET host = ?", (socket.gethostname(),))
        self.conn.commit()
        store.heartbeat(self.conn, self.run)
        self.assertIn("no live worker", abort())
        board.set_state.assert_called_once_with("issue-1", "Todo")
        board.unlabel_issue.assert_called_once_with(
            "issue-1", holophyte.board.lease_label(self.target))
        self.assertEqual(self.conn.execute(
            "SELECT r.outcome, r.outcomeReason, t.status FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId").fetchone(),
            ("abandoned", "host going down", "blocked_on_operator"))
        self.assertEqual(git("log", "-1", "--format=%s", "task/ko-1").strip(),
                         "WIP: preserve work at operator abort")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM interventions WHERE action = 'abort'"
        ).fetchone(), (1,))

    def test_abort_refuses_an_ended_run_naming_its_outcome(self):
        with self.assertRaises(SystemExit):
            self.cli("--abort", "KO-1")
        store.release(self.conn, self.run, "failed")
        before = self.conn.execute("SELECT (SELECT COUNT(*) FROM interventions),"
                                   " (SELECT COUNT(*) FROM runEvents)").fetchone()
        with self.assertRaisesRegex(SystemExit, "outcome failed"):
            self.board_cli(Mock(), "--abort", "KO-1", "--note", "too late")
        self.assertEqual(self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM interventions),"
            " (SELECT COUNT(*) FROM runEvents)").fetchone(), before)

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
        target = holophyte.project.Project.locate(repo)
        with self.assertRaisesRegex(SystemExit, "configuration naming a team"):
            holophyte.cli.cli(["project", "add", str(repo)])
        self.assertFalse(target.store_path.exists())
        with self.assertRaisesRegex(SystemExit, "not a repository root"):
            holophyte.cli.cli(["project", "add", str(self.root / "missing")])
        target.holo_dir.mkdir(parents=True)
        target.config_path.write_text('[board]\nteam = "fresh-team"\n'
                                      'project_id = "fresh-project"\n')
        # Registration writes to the store but never creates or migrates it:
        # the supervisor owns the schema (KO-585).
        with self.assertRaisesRegex(SystemExit, "start the supervisor"):
            holophyte.cli.cli(["project", "add", str(repo)])
        self.assertFalse(target.store_path.exists())
        # The real supervisor start, one real pass, then a stop: it creates
        # the store but leaves the project row to registration.
        def stop(_interval):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        with patch("holophyte.supervisor.factory_revision", return_value="same"):
            self.assertEqual(holophyte.supervisor.supervise(
                target, wait=stop, out=io.StringIO()), 0)
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


class HelpWordTests(unittest.TestCase):
    """KO-617: the loop's help calls the repository it works in a project."""

    def test_help_names_the_project_and_never_a_target(self):
        def help_of(*args):
            with tempfile.TemporaryDirectory() as home:
                done = subprocess.run(
                    [sys.executable, str(ROOT / "factory.py"), *args, "--help"],
                    cwd=ROOT, capture_output=True, text=True,
                    env={**os.environ, "HOLOPHYTE_HOME": home})
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout
        loop = help_of()
        add = help_of("project", "add")
        # The usage block's last word is the positional argument.
        usage = loop.split("\n\n", 1)[0]
        self.assertTrue(usage.startswith("usage: factory.py"), usage)
        self.assertEqual(usage.split()[-1], "project")
        for text in (loop, add):
            self.assertIsNone(re.search(r"(?i)\btarget\b", text), text)


if __name__ == "__main__":
    unittest.main()

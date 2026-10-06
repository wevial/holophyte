"""`holo run N` without `--json`: the run's detail and files as one page,
in the client's zone, ending with the commands its state calls for.

Run: python3 -m unittest discover -s tests -p 'test_holo_run_page.py' -v
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import store
import store.tickets
from holophyte.config.project import Project
from holophyte.holo.cli import main
from holophyte.holo.run_page import page
from holophyte.serve.serve_runs import run_detail, run_files
from tests.test_holo_grammar import ROOT

PACIFIC = ZoneInfo("America/Los_Angeles")
MINUTE = 60_000
TITLE = "fix(board): close a canceled ticket when its live run stops"
PR_URL = "https://example.invalid/org/repo/pull/432"
ESCAPE = "\x1b"
GIT = ("git", "-c", "user.name=test", "-c", "user.email=test@example.com",
       "-c", "commit.gpgsign=false")


def pacific(hour, minute, second=0):
    at = datetime(2026, 10, 6, hour, minute, second, tzinfo=PACIFIC)
    return int(at.timestamp() * 1000)


NOW = pacific(15, 41)


def git(cwd, *args):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


def lines_of(count, changed=()):
    return "".join(f"{'new ' if n in changed else ''}line {n}\n"
                   for n in range(count))


class RunPageCase(unittest.TestCase):
    """A repository whose branch `task/holo-132` changes two files, and a
    store holding that branch's run of HOLO-132, parked on pull request 432
    after a `changes_requested` round and a `pass` round."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        environ = {key: value for key, value in os.environ.items()
                   if key not in ("HOLO_PROJECT", "NO_COLOR")}
        environ["HOLOPHYTE_HOME"] = str(self.home)
        environ["TZ"] = "UTC"
        self.addCleanup(time.tzset)
        self.enterContext(patch.dict(os.environ, environ, clear=True))
        time.tzset()
        self.target = self.root / "repo"
        self.commit_two_files()
        self.project = Project.locate(self.target, adopt=False)
        self.project.holo_dir.mkdir(parents=True)
        (self.home / "client.toml").write_text(
            'timezone = "America/Los_Angeles"\n')
        self.conn = store.open(str(self.project.store_path))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(
            self.conn, "team-1", self.target)
        self.parked = self.park_reviewed_run()

    def commit_two_files(self):
        board, stop = (self.target / "store" / "board.py",
                       self.target / "holophyte" / "loop" / "stop.py")
        for path in (board, stop):
            path.parent.mkdir(parents=True)
        git(self.root, "init", "-q", "-b", "main", str(self.target))
        board.write_text(lines_of(150))
        stop.write_text(lines_of(10))
        git(self.target, "add", ".")
        git(self.target, "commit", "-q", "-m", "base")
        git(self.target, "checkout", "-q", "-b", "task/holo-132")
        board.write_text(lines_of(150, changed={140}))
        stop.write_text(lines_of(7) + lines_of(14, changed=range(14)))
        git(self.target, "commit", "-q", "-am", "task")
        git(self.target, "checkout", "-q", "main")

    def ticket(self, key, title=None):
        ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=title or f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=30 * MINUTE)
        store.tickets.transition(self.conn, ticket, "in_flight")
        return ticket

    def park_reviewed_run(self):
        ticket = self.ticket("HOLO-132", TITLE)
        run = store.claim(self.conn, self.project_id, ticket,
                          now=pacific(14, 58))
        store.set_branch(self.conn, run, "task/holo-132")
        for phase, at in (("working", pacific(15, 1)),
                          ("verifying", pacific(15, 14)),
                          ("reviewing", pacific(15, 20))):
            store.set_phase(self.conn, run, phase, now=at)
        store.record_review_round(
            self.conn, run, 1, "changes_requested", "opus",
            findings=[{"severity": "p2", "path": "store/board.py", "line": 140,
                       "message": "close the canceled ticket too"}],
            started_at=pacific(15, 20), ended_at=pacific(15, 22))
        for phase, at in (("addressing", pacific(15, 23)),
                          ("verifying", pacific(15, 30)),
                          ("reviewing", pacific(15, 35))):
            store.set_phase(self.conn, run, phase, now=at)
        store.record_review_round(
            self.conn, run, 2, "pass", "opus",
            started_at=pacific(15, 35), ended_at=pacific(15, 39))
        store.set_phase(self.conn, run, "merge_gate", now=pacific(15, 39, 30))
        store.park(self.conn, run, "awaiting_merge_approval", pr_url=PR_URL,
                   note="needs you: approve, send back or abort",
                   now=pacific(15, 40))
        store.tickets.transition(self.conn, ticket, "blocked_on_operator")
        return run

    def page_of(self, run):
        """`holo run RUN -p TARGET` in process at NOW; its lines."""
        out, err = io.StringIO(), io.StringIO()
        with patch("holophyte.serve.serve_runs.time", return_value=NOW / 1000), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["run", str(run), "-p", str(self.target)])
        self.assertEqual(code, 0, err.getvalue())
        return out.getvalue().splitlines()

    def next_line(self, run):
        return next(line for line in self.page_of(run) if line.startswith("Next"))


class ParkedRunPageTests(RunPageCase):
    def test_the_header_names_the_ticket_then_the_run_state_time_and_pr(self):
        lines = self.page_of(self.parked)

        self.assertEqual(lines[0], f"HOLO-132  {TITLE}")
        self.assertEqual(lines[1], f"run {self.parked} · parked, awaiting merge"
                                   " approval · 43 of 30 min · PR #432")

    def test_the_timeline_holds_events_and_rounds_in_order_in_pacific_time(self):
        lines = self.page_of(self.parked)

        self.assertEqual(lines[3:14], [
            "  ✓  working                  15:01 PDT   claimed -> working",
            "  ✓  verifying                15:14 PDT   working -> verifying",
            "  ✓  reviewing                15:20 PDT   verifying -> reviewing",
            "  ✗  review r1                15:22 PDT   changes_requested · opus"
            " · p2 store/board.py:140",
            "  ✓  addressing               15:23 PDT   reviewing -> addressing",
            "  ✓  verifying                15:30 PDT   addressing -> verifying",
            "  ✓  reviewing                15:35 PDT   verifying -> reviewing",
            "  ✓  review r2                15:39 PDT   pass · opus",
            "  ✓  merge_gate               15:39 PDT   reviewing -> merge_gate",
            "  !  awaiting_merge_approval  15:40 PDT   needs you: approve,"
            " send back or abort",
            "",
        ])

    def test_the_files_line_has_both_files_with_their_git_counts(self):
        _, files = run_files(self.project, str(self.parked))

        [line] = [line for line in self.page_of(self.parked)
                  if line.startswith("Files")]

        self.assertEqual([(f["path"], f["added"], f["deleted"])
                          for f in files["files"]],
                         [("holophyte/loop/stop.py", 14, 3),
                          ("store/board.py", 1, 1)])
        self.assertEqual(line, "Files  holophyte/loop/stop.py +14 −3"
                               " · store/board.py +1 −1")

    def test_next_suggests_approve_and_the_ledger_and_no_requeue(self):
        line = self.next_line(self.parked)

        self.assertIn("holo approve HOLO-132", line)
        self.assertTrue(line.endswith(f"holo run {self.parked} --ledger"), line)
        self.assertNotIn("requeue", line)

    def test_json_is_the_run_detail_body(self):
        out = io.StringIO()
        with patch("holophyte.serve.serve_runs.time", return_value=NOW / 1000), \
                contextlib.redirect_stdout(out):
            code = main(["run", str(self.parked), "--json", "-p",
                         str(self.target)])

        self.assertEqual(code, 0)
        _, body = run_detail(self.project, str(self.parked), now=NOW)
        self.assertEqual(out.getvalue(), json.dumps(body) + "\n")

    def test_piped_the_page_carries_no_escape_sequence(self):
        completed = subprocess.run(
            [sys.executable, "-m", "holophyte.holo", "run", str(self.parked),
             "-p", str(self.target)], cwd=ROOT, capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)})

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("HOLO-132", completed.stdout)
        self.assertNotIn(ESCAPE, completed.stdout)
        _, body = run_detail(self.project, str(self.parked))
        _, files = run_files(self.project, str(self.parked))
        self.assertIn(ESCAPE, "\n".join(page(body, files, PACIFIC, colour=True)))


class NextByStateTests(RunPageCase):
    def test_a_failed_run_suggests_requeue(self):
        run = store.claim(self.conn, self.project_id, self.ticket("HOLO-150"),
                          now=NOW - 20 * MINUTE)
        store.release(self.conn, run, "failed", reason="verify failed",
                      now=NOW - MINUTE)

        self.assertEqual(self.next_line(run),
                         'Next   holo requeue HOLO-150 "note"   ·   '
                         f"holo run {run} --ledger")

    def test_a_run_parked_on_its_pull_request_suggests_send_back_and_babysit(self):
        line = self.next_line(self.parked)

        self.assertIn(f'holo send-back {self.parked} "note"', line)
        self.assertIn("holo babysit HOLO-132", line)

    def test_a_run_parked_with_no_pull_request_suggests_no_send_back(self):
        ticket = self.ticket("HOLO-151")
        run = store.claim(self.conn, self.project_id, ticket,
                          now=NOW - 20 * MINUTE)
        for phase in ("working", "verifying"):
            store.set_phase(self.conn, run, phase, now=NOW - 10 * MINUTE)
        store.park(self.conn, run, "awaiting_merge_approval", now=NOW - MINUTE)

        self.assertEqual(self.next_line(run),
                         "Next   holo approve HOLO-151   ·   "
                         f"holo run {run} --ledger")

    def test_a_working_run_shows_its_heartbeat_and_only_the_ledger(self):
        run = store.claim(self.conn, self.project_id, self.ticket("HOLO-152"),
                          now=NOW - 5 * MINUTE)
        store.set_phase(self.conn, run, "working", now=NOW - 20_000)

        lines = self.page_of(run)

        self.assertEqual(lines[1], f"run {run} · working · 5 of 30 min"
                                   " · heartbeat 20 s ago")
        self.assertEqual(lines[-1], f"Next   holo run {run} --ledger")


if __name__ == "__main__":
    unittest.main()

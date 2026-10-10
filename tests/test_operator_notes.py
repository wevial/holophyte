"""Regressions for private note commit citations and report boundaries."""
import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_fixture import LoopFixture  # noqa: E402
from serve_fixture import ServeTestCase  # noqa: E402

import holophyte.cli.report  # noqa: E402
import store  # noqa: E402
from holophyte.babysit.maintainer_notes import (  # noqa: E402
    amended_ticket,
    cite_commits,
    pending_state,
)
from holophyte.babysit.thread_text import fix_brief  # noqa: E402
from holophyte.cli.operator import steer_ticket  # noqa: E402
from holophyte.config.project import Project  # noqa: E402
from holophyte.holo import cli as holo_cli  # noqa: E402
from holophyte.pr.github import PrState, Thread  # noqa: E402
from holophyte.pr.pr_status import parse_pr_url  # noqa: E402
from store.operator_notes import consume, notes, send_back  # noqa: E402


class NoteCitationTests(LoopFixture):
    def test_event_ten_does_not_cite_event_one(self):
        (self.target / "README.md").write_text("requested change\n")
        self.git("add", "README.md")
        self.git("commit", "-m", "Fix operator_note event 10")
        fixed = self.git("rev-parse", "HEAD").strip()
        addressed = [(n, Thread(f"operator_note:{n}", "", None, "maintainer",
                                "change requested", "", author_kind="maintainer"), "")
                     for n in (1, 10)]

        def sh(argv, cwd):
            return self.git(*argv[1:], cwd=cwd).strip()

        result = cite_commits(self.target, self.base, fixed, addressed, sh)
        self.assertNotEqual(result, fixed)
        self.assertEqual(self.git("log", "-1", "--format=%B").strip(),
                         "Fix operator_note event 10\n\noperator_note event 1")
        self.assertEqual(cite_commits(self.target, self.base, result, addressed, sh),
                         result)


class NoteReportTests(ServeTestCase):
    def test_multiline_note_and_author_stay_on_the_consuming_round_line(self):
        self.seed()
        note = "remove heading\nkeep body\r\nkeep footer\u2028last line"
        author = "maintainer\rseat\u2029operator"
        with store.open(str(self.db)) as conn:
            for phase in ("verifying", "reviewing", "merge_gate"):
                store.set_phase(conn, self.run, phase)
            store.park(conn, self.run, "awaiting_merge_approval",
                       pr_url="https://example.test/org/repo/pull/1")
            store.tickets.transition(conn, 1, "blocked_on_operator")
            event_id = send_back(conn, self.run, note, author)
            consume(conn, self.run, [event_id], 1)
            report = holophyte.cli.report.report_lines(conn)
            instruction, = notes(conn, self.run)
        self.assertEqual(instruction["note"], note)
        self.assertEqual(instruction["author"], author)
        self.assertIn(
            f"Run {self.run} round 1: operator_note event {event_id} by "
            r"maintainer\rseat\u2029operator: remove heading\nkeep body\r\n"
            r"keep footer\u2028last line", report)
        self.assertEqual("\n".join(report).splitlines(), report)


class LiveNoteReportTests(ServeTestCase):
    def test_a_consumed_note_steered_into_a_babysitting_run_is_reported(self):
        self.seed()
        url = "https://github.com/example/repo/pull/1"
        note = "also log the port"
        with store.open(str(self.db)) as conn:
            for phase in ("verifying", "reviewing", "merge_gate"):
                store.set_phase(conn, self.run, phase)
            store.set_pull_request(conn, self.run, url)
            conn.commit()
            steer_ticket(Project.locate(self.target), "KO-7", note,
                         author="maintainer", out=io.StringIO())
            (event_id,) = conn.execute(
                "SELECT eventId FROM steerNotes").fetchone()
            consume(conn, self.run, [event_id], 2)
            conn.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = holo_cli.main(["report", "--notes", "-p", str(self.target)])
        self.assertFalse(code, out.getvalue())
        lines = out.getvalue().splitlines()
        noted = lines[lines.index("Notes (1)") + 1]
        self.assertTrue(noted.endswith(
            f"KO-7 run {self.run} round 2  maintainer: {note}"), noted)


class HintNoteTests(ServeTestCase):
    def test_a_hint_reaches_the_fix_brief_but_not_the_reviewers_ticket(self):
        self.seed()
        url = "https://github.com/example/repo/pull/1"
        hint = "the port is in config.toml"
        with store.open(str(self.db)) as conn:
            for phase in ("verifying", "reviewing", "merge_gate"):
                store.set_phase(conn, self.run, phase)
            store.park(conn, self.run, "awaiting_merge_approval", pr_url=url)
            store.tickets.transition(conn, 1, "blocked_on_operator")
            conn.commit()
            steer_ticket(Project.locate(self.target), "KO-7", hint, hint=True,
                         author="maintainer", out=io.StringIO())
            reviewed = amended_ticket(conn, self.run, "the ticket", url)
            state = pending_state(conn, self.run, PrState((), "success", None),
                                  url)
        (thread,) = state.threads
        brief = fix_brief(parse_pr_url(url), [(1, thread, "")], reviewed)
        self.assertEqual(reviewed, "the ticket")
        self.assertIn(hint, brief)

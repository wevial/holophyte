"""Regressions for private note commit citations and report boundaries."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loop_fixture import LoopFixture  # noqa: E402
from serve_fixture import ServeTestCase  # noqa: E402

import holophyte.report  # noqa: E402
import store  # noqa: E402
from holophyte.maintainer_notes import cite_commits  # noqa: E402
from holophyte.pr import Thread  # noqa: E402
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
        with store.open(str(self.db), migrate="owner") as conn:
            for phase in ("verifying", "reviewing", "merge_gate"):
                store.set_phase(conn, self.run, phase)
            store.park(conn, self.run, "awaiting_merge_approval",
                       pr_url="https://example.test/org/repo/pull/1")
            store.tickets.transition(conn, 1, "blocked_on_operator")
            event_id = send_back(conn, self.run, note, author)
            consume(conn, self.run, [event_id], 1)
            report = holophyte.report.report_lines(conn)
            instruction, = notes(conn, self.run)
        self.assertEqual(instruction["note"], note)
        self.assertEqual(instruction["author"], author)
        self.assertIn(
            f"Run {self.run} round 1: operator_note event {event_id} by "
            r"maintainer\rseat\u2029operator: remove heading\nkeep body\r\n"
            r"keep footer\u2028last line", report)
        self.assertEqual("\n".join(report).splitlines(), report)

"""A console note's text never leaves for GitHub in a pushed commit message,
pull request text or a thread reply.

Run: python3 -m unittest discover -s tests -p 'test_note_privacy.py' -v
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import store
import store.tickets
from holophyte.babysit.thread_text import fix_brief
from holophyte.cli.operator import BABYSIT_DEFAULT_NOTE
from holophyte.note_privacy import private_texts
from holophyte.pr.github import Thread
from holophyte.pr.pr_status import parse_pr_url
from store import console_asks, steer_notes
from store.operator_notes import send_back
from tests.fake_agent import APPROVE, Commit, Idle, Reply, _git
from tests.loop_fixture import MergeModeFixture
from tests.phase_fixture import park_run

NOTE = ("drop the legacy export flag and keep the importer reading"
        " the old column names")
WITHHELD = "[maintainer note withheld]"
NEVER = ("never quote, restate or paraphrase it in a commit message, a"
         " FOLLOW_UP line, pull request text or a thread reply")


class Recorded(Commit):
    """A fix turn that commits, keeps its own sha and answers `reply`."""

    def __init__(self, message, reply=None):
        super().__init__(message)
        self.reply, self.sha = reply, None

    def play(self, cwd, turn):
        answer = super().play(cwd, turn)
        self.sha = _git(cwd, "rev-parse", "HEAD")
        return self.reply if self.reply is not None else answer


class SendBackPushTests(MergeModeFixture):
    def sent_back(self, threads=(), note=NOTE, steer=None):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state()])
        self.loop(Commit("candidate"), APPROVE, Idle(""), provider=self.provider())
        with store.open(str(self.project.store_path)) as conn:
            run_id = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            if steer is None:
                note_id = send_back(conn, run_id, note, "maintainer")
            else:
                ticket = conn.execute("SELECT ticketId FROM runs WHERE id = ?",
                                      (run_id,)).fetchone()[0]
                note_id = steer_notes.steer(conn, ticket, steer,
                                            "maintainer").event_id
        self.push_log.unlink(missing_ok=True)
        self.pr_body.unlink(missing_ok=True)
        for path in self.api_dir.iterdir():
            path.unlink()
        self.serve(self.pr_state(list(threads)), self.pr_state())
        return note_id

    def sent(self):
        """Every request text the fake `gh` recorded: argv, API bodies and
        the pull request body."""
        texts = [*self.recorded(),
                 *(p.read_text() for p in self.api_dir.iterdir())]
        if self.pr_body.exists():
            texts.append(self.pr_body.read_text())
        return "\n".join(texts)

    def test_a_fix_commit_quoting_a_send_back_is_pushed_with_the_note_withheld(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state()])
        self.loop(Commit("candidate"), APPROVE, Idle(""), provider=self.provider())
        with store.open(str(self.project.store_path)) as conn:
            run_id = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            note_id = send_back(conn, run_id, NOTE, "maintainer")
        self.push_log.unlink(missing_ok=True)
        self.serve(self.pr_state())
        title = "Apply the maintainer's instruction"
        fix = Recorded(f"{title}\n\nPer operator_note event {note_id}: {NOTE}")
        self.loop(fix, Idle(""), provider=self.provider())

        _, tip = self.pushed()[-1]
        message = self.git("log", "-1", "--format=%B", tip).strip()
        self.assertEqual(
            message, f"{title}\n\nPer operator_note event {note_id}: {WITHHELD}")
        self.assertNotEqual(tip, fix.sha)
        self.assertEqual(self.git("rev-parse", f"{tip}^{{tree}}"),
                         self.git("rev-parse", f"{fix.sha}^{{tree}}"))
        self.assertNotIn(NOTE, self.sent())

    def test_a_fix_commit_citing_only_the_id_is_pushed_unchanged(self):
        note_id = self.sent_back()
        fix = Recorded(f"Apply operator_note event {note_id}")
        self.loop(fix, Idle(""), provider=self.provider())

        _, tip = self.pushed()[-1]
        self.assertEqual(tip, fix.sha)

    def test_a_thread_reply_and_the_description_withhold_a_quoted_note(self):
        self.sent_back([self.NIT])
        fix = Recorded("Rename the counter", reply=f"THREAD 1: Did as asked: {NOTE}.")
        self.loop(Reply("THREAD 1: ADDRESS -- style"), fix,
                  Idle(f"Renames the counter.\n\nAs the maintainer said: {NOTE}."),
                  provider=self.provider())

        replies = [value for kind, value in self.api_calls() if kind == "reply"]
        self.assertTrue(replies)
        self.assertTrue(all(WITHHELD in str(v) and NOTE not in str(v)
                            for v in replies))
        body = self.pr_body.read_text()
        self.assertIn(f"As the maintainer said: {WITHHELD}.", body)
        self.assertNotIn(NOTE, self.sent())

    def test_an_amendment_keeps_its_criterion_and_withholds_its_prose(self):
        prose = "also mention the export in the changelog for the next release"
        self.sent_back(steer=f"ADD: the export names its file\n{prose}")
        self.loop(Commit("Name the export's file"),
                  Idle("The steer said:\n\nADD: the export names its file\n"
                       f"{prose}"),
                  provider=self.provider())

        self.assertIn(f"ADD: the export names its file\n{WITHHELD}",
                      self.pr_body.read_text())
        self.assertNotIn(prose, self.sent())


class FixBriefTests(unittest.TestCase):
    def test_the_brief_cites_the_note_by_id_and_forbids_restating_it(self):
        note = Thread("operator_note:52894", "", None, "maintainer", NOTE, "",
                      author_kind="maintainer")
        brief = fix_brief(parse_pr_url("https://github.com/example/repo/pull/7"),
                          [(1, note, "")], "ticket")
        self.assertIn("Cite operator_note event 52894", brief)
        self.assertIn("by its id alone", brief)
        self.assertIn(NEVER, brief)


class RequeuePushTests(MergeModeFixture):
    def test_a_commit_restating_a_requeue_note_is_pushed_with_it_withheld(self):
        from tests.test_cli_requeue import RequeuedClaimTests
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state()])
        run = RequeuedClaimTests.fail_a_run(self, note=NOTE)
        with store.open(str(self.project.store_path)) as conn:
            (event,) = conn.execute(
                "SELECT id FROM runEvents WHERE runId = ? AND kind = 'intervention'"
                " AND summary LIKE 'human requeue: %'", (run,)).fetchone()
        words = " ".join(NOTE.split()[-10:])
        message = f"Rework the importer\n\nThis change will {words} as before."
        fake, _ = self.loop(Commit(message), APPROVE, Idle(""),
                            provider=self.provider())

        opening = fake.turns[0].goal.split("Implement this task in this repo:")[0]
        self.assertIn(f"Operator's requeue note (intervention event {event}):",
                      opening)
        self.assertIn("by its id alone", opening)
        self.assertIn(NEVER, opening)
        _, tip = self.pushed()[-1]
        self.assertEqual(self.git("log", "-1", "--format=%B", tip).strip(),
                         f"Rework the importer\n\nThis change will {WITHHELD}"
                         " as before.")
        self.assertNotIn(words, "\n".join(
            [*self.recorded(), *(p.read_text() for p in self.api_dir.iterdir()),
             self.pr_body.read_text() if self.pr_body.exists() else ""]))


class PrivateTextsTests(unittest.TestCase):
    HUMAN = "rerun once the fixture server stops timing out on the second call"
    FACTORY = "the verify flaked on a port clash, so one more attempt is due"
    ABORT = "stop this one, the approach rewrites the importer we are keeping"
    SEND_BACK = "split the parser change from the renaming so review stays small"
    QUESTION = "why does the importer read the old column names at all?"
    OTHER = "a different ticket's note that must not show up for the first one"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = store.open(str(Path(tmp.name) / "store.db"))
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        self.project_id = store.tickets.ensure_project(self.conn, "team-1",
                                                    Path(tmp.name))

    def ticket(self, key):
        return store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id=f"issue-{key}",
            linear_identifier=key, title=f"ticket {key}",
            acceptance_criteria=[f"Given {key}, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60_000)

    def claim(self, ticket):
        store.tickets.transition(self.conn, ticket, "in_flight")
        return store.claim(self.conn, self.project_id, ticket)

    def failed_and_requeued(self, ticket, note, source="human"):
        run = self.claim(ticket)
        store.release(self.conn, run, "failed", "review rejected")
        store.requeue(self.conn, ticket, note, source=source)
        return run

    def parked(self, ticket):
        run = self.claim(ticket)
        park_run(self.conn, run, "awaiting_merge_approval",
                 pr_url="https://github.com/example/repo/pull/7")
        store.tickets.transition(self.conn, ticket, "blocked_on_operator")
        return run

    def test_every_console_note_on_the_ticket_and_no_other(self):
        ticket, other = self.ticket("KO-1"), self.ticket("KO-2")
        self.failed_and_requeued(ticket, self.HUMAN)
        self.failed_and_requeued(ticket, self.FACTORY, source="factory")
        self.parked(ticket)
        store.babysit(self.conn, ticket, BABYSIT_DEFAULT_NOTE)
        send_back(self.conn, self.parked(ticket), self.SEND_BACK, "maintainer")
        console_asks.ask(self.conn, self.parked(ticket), self.QUESTION,
                         "maintainer")
        newest = self.claim(ticket)
        store.abort(self.conn, newest, self.ABORT)
        self.failed_and_requeued(other, self.OTHER)

        texts = private_texts(self.conn, newest)

        for note in (self.HUMAN, self.FACTORY, self.ABORT, self.SEND_BACK,
                     self.QUESTION):
            self.assertTrue(any(note in text for text in texts), note)
        self.assertFalse(any(self.OTHER in text for text in texts))
        self.assertNotIn(BABYSIT_DEFAULT_NOTE, texts)


if __name__ == "__main__":
    unittest.main()

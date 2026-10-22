"""A console note's text never leaves for GitHub in a pushed commit message,
pull request text or a thread reply.

Run: python3 -m unittest discover -s tests -p 'test_note_privacy.py' -v
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.tickets
from holophyte.babysit.thread_text import fix_brief
from holophyte.cli.operator import BABYSIT_DEFAULT_NOTE
from holophyte.note_privacy import private_texts, withhold
from holophyte.pr.github import Thread
from holophyte.pr.pr_status import parse_pr_url
from store import console_asks, steer_notes
from store.operator_notes import send_back
from tests.fake_agent import APPROVE, Commit, Consolidate, Idle, Reply, _git
from tests.loop_fixture import BRANCH, MergeModeFixture
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


class OriginFixture(MergeModeFixture):
    """The fake route, with each push it answers also made by real `git`
    into a bare repository standing in for origin."""

    def route(self, **kwargs):
        bare = Path(self.enterContext(tempfile.TemporaryDirectory())) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        self.bare, real_git = bare, shutil.which("git")
        self.fake_route(push_sh=f'  for last in "$@"; do :; done\n'
                                f'  "{real_git}" push -q "{bare}" "$last" || exit 1',
                        **kwargs)

    def origin(self, *args):
        return subprocess.run(["git", "--git-dir", str(self.bare), *args],
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    def origin_tip(self):
        return self.origin("rev-parse", f"refs/heads/{BRANCH}")

    def sent(self):
        """Every request text the fake `gh` recorded: argv, API bodies and
        the pull request body."""
        texts = [*self.recorded(),
                 *(p.read_text() for p in self.api_dir.iterdir())]
        if self.pr_body.exists():
            texts.append(self.pr_body.read_text())
        return "\n".join(texts)


class SendBackPushTests(OriginFixture):
    def sent_back(self, threads=(), note=NOTE, steer=None):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.route(states=[self.pr_state()])
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

    def test_a_fix_commit_quoting_a_send_back_is_pushed_with_the_note_withheld(self):
        note_id = self.sent_back()
        title = "Apply the maintainer's instruction"
        fix = Recorded(f"{title}\n\nPer operator_note event {note_id}: {NOTE}")
        self.loop(fix, Idle(""), provider=self.provider())

        tip = self.origin_tip()
        message = self.origin("log", "-1", "--format=%B", tip)
        self.assertEqual(
            message, f"{title}\n\nPer operator_note event {note_id}: {WITHHELD}")
        self.assertNotEqual(tip, fix.sha)
        self.assertEqual(self.origin("rev-parse", f"{tip}^{{tree}}"),
                         self.git("rev-parse", f"{fix.sha}^{{tree}}").strip())
        self.assertNotIn(NOTE, self.sent())

    def test_a_fix_commit_citing_only_the_id_is_pushed_unchanged(self):
        note_id = self.sent_back()
        fix = Recorded(f"Apply operator_note event {note_id}")
        self.loop(fix, Idle(""), provider=self.provider())

        self.assertEqual(self.origin_tip(), fix.sha)

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


    def test_an_evidence_failure_log_quoting_a_note_is_withheld_from_the_body(self):
        from holophyte.pr import pr_media
        from holophyte.pr.evidence_refresh import publish_evidence
        note_id = self.sent_back()
        self.loop(Commit(f"Apply operator_note event {note_id}"), Idle(""),
                  provider=self.provider())
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'ui_paths = ["ui/**"]\nui_capture = "true"\n')
        section = f"## Evidence\n\nCapture failed:\n\n    echo {NOTE}\n"
        with store.open(str(self.project.store_path)) as conn, \
                patch.object(pr_media, "refresh", return_value=section), \
                patch.object(pr_media, "capture_failure", return_value=None):
            run_id = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            publish_evidence(self.project, conn, run_id, "KO-131", self.target,
                             parse_pr_url(self.URL), self.BODY, "a" * 40, 1)

        self.assertIn(f"echo {WITHHELD}", self.pr_body.read_text())
        self.assertNotIn(NOTE, self.sent())


class WithholdTests(unittest.TestCase):
    def test_a_note_of_repeated_words_is_withheld_whole(self):
        note = "keep the old name keep the old name keep the old name"
        self.assertEqual(withhold(f"Done: {note}.", [note]), f"Done: {WITHHELD}.")


class FixBriefTests(unittest.TestCase):
    def test_the_brief_cites_the_note_by_id_and_forbids_restating_it(self):
        note = Thread("operator_note:52894", "", None, "maintainer", NOTE, "",
                      author_kind="maintainer")
        brief = fix_brief(parse_pr_url("https://github.com/example/repo/pull/7"),
                          [(1, note, "")], "ticket")
        self.assertIn("Cite operator_note event 52894", brief)
        self.assertIn("by its id alone", brief)
        self.assertIn(NEVER, brief)


class RequeuePushTests(OriginFixture):
    def test_a_commit_restating_a_requeue_note_is_pushed_with_it_withheld(self):
        from tests.test_cli_requeue import RequeuedClaimTests
        self.configure('[merge]\nmode = "pr"\n')
        self.route(states=[self.pr_state()])
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
        self.assertEqual(self.origin("log", "-1", "--format=%B", self.origin_tip()),
                         f"Rework the importer\n\nThis change will {WITHHELD}"
                         " as before.")
        self.assertNotIn(words, self.sent())


    def test_a_held_concern_quoting_a_requeue_note_is_withheld_from_the_body(self):
        from tests.test_adversary import Change, attack, finding
        from tests.test_cli_requeue import RequeuedClaimTests
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       "[review]\nadversary = true\n")
        self.route()
        RequeuedClaimTests.fail_a_run(self, note=NOTE)
        self.loop(
            Change("poetry.lock"),
            Reply("- src/app.py:3 [p1] load() returns None\n\n"
                  "CRITERION 1: met \u2014 tests/test_thing.py::test_it_works\n"
                  "VERDICT: REQUEST_CHANGES"),
            attack(*(finding(f"src/c{n}.py", n, message, "concern") for n, message
                     in enumerate(("one", "two", "three",
                                   f"the note asks to {NOTE}"), 1))),
            Consolidate(), Change("poetry.lock", "relocked\n"),
            APPROVE, attack(), Idle(""), provider=self.provider())

        body = self.pr_body.read_text()
        self.assertIn(f"src/c4.py:4 [p1] the note asks to {WITHHELD}", body)
        self.assertNotIn(NOTE, self.sent())


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

    def test_a_human_babysit_note_worded_like_mark_ready_stays_private(self):
        ticket = self.ticket("KO-1")
        note = "Everything was marked ready for review"
        self.parked(ticket)
        store.babysit(self.conn, ticket, note)

        self.assertEqual(private_texts(self.conn, self.claim(ticket)), (note,))

    def test_a_send_back_worded_like_the_factorys_babysit_note_stays_private(self):
        ticket = self.ticket("KO-1")
        note = "https://github.com/example/repo/pull/7 was marked ready for review"
        send_back(self.conn, self.parked(ticket), note, "maintainer")

        self.assertEqual(private_texts(self.conn, self.claim(ticket)), (note,))


if __name__ == "__main__":
    unittest.main()

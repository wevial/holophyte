"""A person's message to a running scout: the thread takes it in any live
phase, the lead's synthesis turn takes a waiting steer, the report turn
reads it, and an ask or a late steer waits for the scout's end, against stub
lead and helper CLIs over a real git repository.

Run: python3 -m unittest discover -s tests -p 'test_scout_live_steer.py' -v
"""
import contextlib
import io
import json
import os
import shlex
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import store
from holophyte.config.project import Project
from holophyte.scout.thread_action import message_action
from store.scout_thread import (
    ThreadRefused,
    messages,
    post_message,
    take_message,
    waiting_messages,
)
from store.scouts import create_scout, end_scout, move_scout, scout
from tests.host_fixture import git
from tests.scout_stubs import BRIEF, fenced, titled
from tests.test_scout_lead import CLAUDE_LEAD
from tests.test_scout_process import ScoutFixture
from tests.test_store_scout_thread import open_store

ROOT = Path(__file__).resolve().parent.parent
STEER = "check the digest email too"
ASK = "which digest does this send?"
FIRST, SECOND = "Where is the digest sent?", "Who builds the digest?"
REPORT = {**BRIEF, "summary": "the digest is sent twice", "findings": [],
          "actions": []}
POST = ("import store\nfrom store.scout_thread import post_message\n"
        "conn = store.open({store!r})\n"
        "post_message(conn, {scout}, 'maintainer', {mode!r}, {text!r})\n")


def plan(question):
    return fenced({"tasks": [titled({"question": question,
                                      "paths": ["notes.txt"], "mode": "read"})]})


def post_command(store_path, scout_id, mode, text):
    script = POST.format(store=str(store_path), scout=scout_id, mode=mode,
                         text=text)
    return (f"PYTHONPATH={shlex.quote(str(ROOT))}"
            f" {shlex.quote(sys.executable)} -c {shlex.quote(script)}")


class LivePostTests(unittest.TestCase):
    def setUp(self):
        _, self.conn = open_store(self)
        self.project_id = store.ensure_project(self.conn, "team",
                                               "/repos/example")

    def scout_in(self, phase):
        scout_id = create_scout(self.conn, self.project_id, "why slow", "bug",
                                30)
        if phase == "queued":
            return scout_id
        move_scout(self.conn, scout_id, "planning", base_sha="abc")
        if phase in ("swarming", "synthesizing"):
            move_scout(self.conn, scout_id, "swarming")
        if phase == "synthesizing":
            move_scout(self.conn, scout_id, "synthesizing")
        if phase in ("done", "failed", "stopped", "answering"):
            end_scout(self.conn, scout_id, "done" if phase == "answering"
                      else phase, f"ended {phase}")
        if phase == "answering":
            take_message(self.conn, post_message(
                self.conn, scout_id, "maintainer", "ask", "what next?"))
        self.assertEqual(scout(self.conn, scout_id).phase, phase)
        return scout_id

    def steer(self, scout_id, mode="steer"):
        return post_message(self.conn, scout_id, "maintainer", mode, STEER)

    def test_a_steer_is_stored_waiting_on_a_live_or_done_scout(self):
        for phase in ("queued", "planning", "swarming", "synthesizing",
                      "answering", "done"):
            with self.subTest(phase=phase):
                scout_id = self.scout_in(phase)
                posted = self.steer(scout_id)
                (message,) = [message for message in
                              messages(self.conn, scout_id)
                              if message.id == posted]
                self.assertEqual((message.mode, message.text,
                                  message.consumed_by),
                                 ("steer", STEER, None))

    def test_a_steer_to_a_failed_or_stopped_scout_is_refused(self):
        for phase in ("failed", "stopped"):
            with self.subTest(phase=phase):
                scout_id = self.scout_in(phase)
                with self.assertRaises(ThreadRefused) as caught:
                    self.steer(scout_id)
                self.assertIn(f"it is {phase}", str(caught.exception))
                self.assertEqual(messages(self.conn, scout_id), [])

    def test_a_second_waiting_message_on_a_running_scout_is_refused(self):
        swarming = self.scout_in("swarming")
        waiting = self.steer(swarming)
        with self.assertRaises(ThreadRefused) as caught:
            self.steer(swarming, "ask")
        self.assertIn(f"message {waiting}", str(caught.exception))
        self.assertEqual([message.id for message in
                          messages(self.conn, swarming)], [waiting])


class LiveSteerRunTests(ScoutFixture):
    def setUp(self):
        super().setUp()
        self.path = self.project("alpha", CLAUDE_LEAD)
        self.db = self.conn(self.path)
        (project,) = self.db.execute("SELECT id FROM projects").fetchone()
        self.scout_id = create_scout(self.db, project, "why is the digest"
                                     " sent twice?", "research", 30, waves=2)
        move_scout(self.db, self.scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main"))
        self.store_path = Project.locate(self.path, adopt=False).store_path

    def run_scout(self, **answers):
        self.stubs.answer(plan=plan(FIRST), synthesis=plan(SECOND),
                          report=fenced(REPORT), **answers)
        code, printed = self.cli("--scout", str(self.scout_id),
                                 str(self.path))
        self.assertEqual(code, 0, printed)
        self.assertEqual(scout(self.db, self.scout_id).phase, "done")

    def posts(self, mode, text):
        return [post_command(self.store_path, self.scout_id, mode, text)]

    def goals(self, turn):
        return [call["argv"][-1] for call in self.stubs.turns(turn)]

    def taken(self):
        return self.db.execute(
            "SELECT id, payload, summary, level FROM runEvents"
            " WHERE scoutId = ? AND kind = 'scout_steer_taken'",
            (self.scout_id,)).fetchall()

    def waiting(self):
        return [(message.mode, message.text)
                for message in waiting_messages(self.db)
                if message.scout_id == self.scout_id]

    def test_a_steer_posted_while_planning_steers_the_synthesis_and_report(self):
        self.run_scout(plan_sh=self.posts("steer", STEER))
        (synthesis,) = self.goals("synthesis")
        self.assertIn(f"Steer: {STEER}", synthesis)
        (steer,) = messages(self.db, self.scout_id)
        ((event, payload, summary, level),) = self.taken()
        self.assertEqual(steer.consumed_by, event)
        self.assertEqual(json.loads(payload), {"message": steer.id, "wave": 2})
        self.assertEqual((summary, level), ("steer taken for wave 2",
                                            "detail"))
        (report,) = self.goals("report")
        self.assertIn(STEER, report)
        self.assertEqual(self.waiting(), [])

    def test_a_steer_posted_in_the_last_wave_waits_for_the_scouts_end(self):
        self.run_scout(helpers={SECOND: {"helper_sh": self.posts("steer",
                                                                 STEER)}})
        self.assertEqual(self.taken(), [])
        lead_goals = [call["argv"][-1] for call in self.stubs.calls()
                      if call["turn"] in ("plan", "synthesis", "report",
                                          "repair")]
        self.assertEqual(len(lead_goals), 3)
        self.assertFalse([goal for goal in lead_goals if STEER in goal])
        self.assertEqual(self.waiting(), [("steer", STEER)])

    def test_an_ask_posted_while_planning_waits_for_the_scouts_end(self):
        self.run_scout(plan_sh=self.posts("ask", ASK))
        (synthesis,) = self.goals("synthesis")
        self.assertNotIn(ASK, synthesis)
        self.assertEqual(self.waiting(), [("ask", ASK)])


class LiveMessageActionTests(ScoutFixture):
    def setUp(self):
        super().setUp()
        self.path = self.project("alpha")
        self.db = self.conn(self.path)
        (self.project_id,) = self.db.execute(
            "SELECT id FROM projects").fetchone()
        self.swarming = create_scout(self.db, self.project_id, "a live scout",
                                     "recon", 30)
        move_scout(self.db, self.swarming, "planning", base_sha="b" * 40)
        move_scout(self.db, self.swarming, "swarming")

    def detail(self, message):
        return (f"scout {self.swarming} is swarming; message {message} waits"
                " for its next wave or its end")

    def test_a_running_scout_queues_the_steer_and_a_failed_one_refuses(self):
        code, reply = message_action(Project.locate(self.path), {
            "scout": self.swarming, "mode": "steer", "text": STEER})
        (message,) = messages(self.db, self.swarming)
        self.assertEqual(code, 200, reply)
        self.assertEqual((reply["ok"], reply["queued"], reply["message"],
                          reply["detail"]),
                         (True, True, message.id, self.detail(message.id)))
        self.assertIsNone(message.consumed_by)

        failed = create_scout(self.db, self.project_id, "an ended scout",
                              "recon", 30)
        move_scout(self.db, failed, "planning", base_sha="c" * 40)
        end_scout(self.db, failed, "failed", "the lead route is down")
        code, reply = message_action(Project.locate(self.path), {
            "scout": failed, "mode": "steer", "text": STEER})
        self.assertEqual((code, reply["ok"], reply["reason"]),
                         (200, False, "not_done"))
        self.assertIn("failed", reply["detail"])
        self.assertEqual(messages(self.db, failed), [])

    def test_holo_scout_steer_on_a_running_scout_prints_that_it_waits(self):
        from holophyte.holo.cli import main
        self.enterContext(patch.dict(os.environ, {"HOLO_TRANSPORT": "local"}))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["scout", "steer", str(self.swarming), STEER,
                             "-p", str(self.path)])
            except SystemExit as stop:
                code = stop.code
        self.assertEqual(code, 0, err.getvalue())
        (message,) = messages(self.db, self.swarming)
        self.assertIn(self.detail(message.id), out.getvalue())

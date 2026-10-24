"""A steered scout's follow-up pass: the host sweep takes a waiting steer,
`factory.py --scout ID PROJECT` resumes the lead's session to plan one wave
under the pass's own budget, and the pass's report is appended as the next
pass, against stub lead and helper CLIs over real git repositories.

Run: python3 -m unittest discover -s tests -p 'test_scout_follow_up.py' -v
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.scout.scout_process as scout_process
import holophyte.scout.scout_sweep as scout_sweep
import holophyte.scout.stop as stop
from holophyte.config.checks import check_config
from holophyte.config.project import Project
from holophyte.host.registry import Host
from holophyte.scout.answer import Outcome, fail_answer, settle, stop_answer
from holophyte.scout.follow_up import (
    PASS_OPENING,
    PassOutcome,
    current_pass,
    fail_pass,
    run_pass,
    settle_pass,
    stop_pass,
)
from holophyte.scout.lead import REPORT_OPENING, SYNTHESIS_OPENING
from holophyte.scout.scout_config import scout_config
from holophyte.scout.scout_process import answer_mode, make_lead, pass_mode
from holophyte.scout.swarm import Stopping
from store.scout_thread import messages, post_message, take_message
from store.scouts import (
    ScoutRefused,
    beat_scout,
    claim_scout,
    create_scout,
    end_scout,
    file_scout_action,
    holding_scout,
    move_scout,
    record_scout_actions,
    record_scout_report,
    record_scout_tasks,
    scout,
    set_scout_session,
    start_scout_task,
)
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_answer import reaped
from tests.test_scout_config import ScoutFixture as ConfigFixture
from tests.test_scout_lead import CLAUDE_LEAD, running
from tests.test_scout_process import MINUTE, now_ms
from tests.test_scout_stop import WAIT_S, StopCase, wait_for

REASON = "the lead wrote its report"
FINDINGS = [{"title": "the retry loop never sleeps", "status": "shown",
             "evidence": [{"url": "https://example.test/retry"}]},
            {"title": "the timeout is read twice", "status": "likely",
             "evidence": [{"url": "https://example.test/timeout"}]}]
ACTIONS = [{"title": "add a backoff", "why": "it spins", "kind": "fix",
            "body": "Add a backoff."},
           {"title": "test the timeout", "why": "read twice", "kind": "test",
            "body": "Test the timeout."}]
REPORT = {"summary": "the retry loop spins", "findings": FINDINGS,
          "actions": ACTIONS, "cut": []}
STEER = "Go check the digest email as well."
QUESTIONS = ["Where is the digest sent?", "Who builds the digest?",
             "What does the digest test assert?", "Where is the digest set?",
             "Which commit added the digest?"]
SECOND = {"summary": "the digest retries too",
          "findings": [{"title": "the digest shares the retry loop",
                        "status": "shown",
                        "evidence": [{"url": "https://example.test/digest"}]}],
          "actions": [{"title": "share the backoff", "why": "same loop",
                       "kind": "fix", "body": "Share the backoff."}]}

ROOT = Path(__file__).resolve().parent.parent
REPLACE = """import store
from holophyte.scout.follow_up import current_pass, fail_pass
from store.scout_thread import post_message, take_message

conn = store.open("{store}")
fail_pass(conn, {scout}, current_pass(conn, {scout}), "heartbeat stale")
take_message(conn, post_message(conn, {scout}, "maintainer", "steer",
                                "And the cron?"))
"""
STALE_ANSWER = """import store
from holophyte.scout.answer import fail_answer
from store.scout_thread import post_message, take_message

conn = store.open("{store}")
fail_answer(conn, {scout}, {ask}, "heartbeat stale")
take_message(conn, post_message(conn, {scout}, "maintainer", "steer",
                                "Go check the digest email as well."))
"""


def plan(*questions):
    return fenced({"tasks": [{"question": question, "paths": ["notes.txt"],
                              "mode": "read"} for question in questions]})


class PassCase(StopCase):
    """`alpha` with a Claude lead and a `done` scout holding a pass 1 report
    of two findings and two action items, the second filed as KEY-1."""

    config = CLAUDE_LEAD

    def setUp(self):
        super().setUp()
        self.target = Project.locate(self.path, adopt=False)
        self.target.config_path.write_text(
            self.target.config_path.read_text() + self.config)
        self.stubs.answer(pass_plan=plan(QUESTIONS[0]), report=fenced(SECOND))

    def done_scout(self, session="S", **counts):
        (project,) = self.store_conn.execute(
            "SELECT id FROM projects").fetchone()
        scout_id = create_scout(self.store_conn, project, "why does the"
                                " retry spin?", "research", 30, **counts)
        move_scout(self.store_conn, scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main"))
        set_scout_session(self.store_conn, scout_id, session)
        record_scout_report(self.store_conn, scout_id, 1, REPORT)
        record_scout_actions(self.store_conn, scout_id, 1, ACTIONS)
        end_scout(self.store_conn, scout_id, "done", REASON)
        file_scout_action(self.store_conn, scout_id, 2, "KEY-1")
        return scout_id

    def steer(self, scout_id, text=STEER):
        return post_message(self.store_conn, scout_id, "maintainer", "steer",
                            text)

    def steered(self, **counts):
        scout_id = self.done_scout(**counts)
        take_message(self.store_conn, self.steer(scout_id))
        return scout_id

    def reports(self, scout_id):
        return {number: json.loads(report) for number, report in
                self.store_conn.execute(
                    "SELECT pass, report FROM scoutReports WHERE scoutId = ?",
                    (scout_id,))}

    def items(self, scout_id):
        return self.store_conn.execute(
            "SELECT pass, position, item, ticketKey FROM scoutActions"
            " WHERE scoutId = ? ORDER BY position", (scout_id,)).fetchall()

    def tasks(self, scout_id, number=2):
        return self.store_conn.execute(
            "SELECT wave, question, phase FROM scoutTasks WHERE scoutId = ?"
            " AND pass = ? ORDER BY id", (scout_id, number)).fetchall()

    def payloads(self, scout_id, kind):
        return [payload for event, payload in self.events(scout_id)
                if event == kind]

    def first_pass_kept(self, scout_id):
        self.assertEqual(self.reports(scout_id)[1], REPORT)
        self.assertEqual(self.items(scout_id)[:2], [
            (1, 1, json.dumps(ACTIONS[0]), None),
            (1, 2, json.dumps(ACTIONS[1]), "KEY-1")])


class SweptPassTests(PassCase):
    def test_a_swept_steer_runs_one_resumed_wave_and_appends_pass_two(self):
        session = str(uuid.uuid4())
        scout_id = self.done_scout(session, waves=2)
        before = scout(self.store_conn, scout_id)
        self.stubs.answer(pass_plan=plan(QUESTIONS[0]), report=fenced(SECOND),
                          synthesis=plan(QUESTIONS[1]))
        steer = self.steer(scout_id)

        _, printed = self.cli("--supervise", "--once")
        self.assertIn(f"steered by message {steer}", printed)
        self.assertTrue(wait_for(lambda: "scout_pass_done" in [
            kind for kind, _ in self.events(scout_id)], 60),
            self.events(scout_id))
        self.assertTrue(wait_for(
            lambda: scout(self.store_conn, scout_id).phase == "done"))
        (pid,) = map(int, re.findall(r"steered by message \d+ as pid (\d+)",
                                     printed))
        self.assertTrue(wait_for(lambda: reaped(pid), 30))

        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.ended_at, row.reason), (before.ended_at, REASON))
        self.assertEqual(self.reports(scout_id)[2], dict(SECOND, cut=[]))
        self.first_pass_kept(scout_id)
        self.assertEqual(self.items(scout_id)[2:], [
            (2, 3, json.dumps(SECOND["actions"][0]), None)])
        events = self.store_conn.execute(
            "SELECT id, kind, payload FROM runEvents WHERE scoutId = ?"
            " ORDER BY seq", (scout_id,)).fetchall()
        kinds = [kind for _, kind, _ in events]
        started = kinds.index("scout_pass_started")
        finished = kinds.index("scout_pass_done")
        self.assertLess(started, finished)
        self.assertEqual(json.loads(events[started][2])["pass"], 2)
        self.assertEqual(json.loads(events[finished][2])["pass"], 2)
        (taken,) = messages(self.store_conn, scout_id)
        self.assertEqual(taken.consumed_by, events[started][0])

        (planned,) = self.stubs.turns("pass_plan")
        self.assertEqual(planned["argv"][planned["argv"].index("--resume") + 1],
                         session)
        self.assertNotIn("--session-id", planned["argv"])
        lines = planned["argv"][-1].splitlines()
        self.assertEqual(lines[0], PASS_OPENING)
        self.assertTrue([line for line in lines if STEER in line])
        self.assertTrue([line for line in lines if line.startswith("F1 ")
                         and FINDINGS[0]["title"] in line])
        (reported,) = self.stubs.turns("report")
        self.assertEqual(
            reported["argv"][reported["argv"].index("--resume") + 1], session)
        goals = [call["argv"][-1] for call in self.stubs.calls()]
        self.assertFalse([goal for goal in goals
                          if goal.startswith(SYNTHESIS_OPENING)])
        self.assertEqual(self.tasks(scout_id), [(1, QUESTIONS[0], "done")])


class PassBudgetTests(PassCase):
    def test_a_pass_starts_no_more_helpers_than_steer_helpers(self):
        config = self.target.config_path
        config.write_text(config.read_text().replace(
            "[scout]\n", "[scout]\nsteer_helpers = 3\n", 1))
        shared = self.root / "shared"
        shared.mkdir()
        self.stubs.answer(pass_plan=plan(*QUESTIONS), report=fenced(SECOND),
                          helper_shared=str(shared), helper_sleep=1)
        scout_id = self.steered(swarm=2)
        code, printed = self.cli("--scout", str(scout_id), str(self.path))
        self.assertEqual(code, 0, printed)

        self.assertEqual(len(self.stubs.turns("helper")), 3)
        self.assertLessEqual(max(helper["present"]
                                 for helper in self.stubs.helpers()), 2)
        self.assertEqual([phase for _, _, phase in self.tasks(scout_id)],
                         ["done"] * 3 + ["queued"] * 2)
        self.assertEqual(self.reports(scout_id)[2]["cut"], QUESTIONS[3:])

    def test_a_helper_past_the_pass_budget_is_stopped_and_the_report_runs(self):
        pid_file = self.child("helper-child")
        self.stubs.answer(pass_plan=plan(QUESTIONS[0]), report=fenced(SECOND),
                          helper_sleep=60, helper_pid_file=str(pid_file))
        scout_id = self.steered()
        row = scout(self.store_conn, scout_id)
        worktree = make_lead(self.target, scout_id, row.base_sha)
        started = time.monotonic()
        outcome = run_pass(self.store_conn, self.target, row,
                           current_pass(self.store_conn, scout_id), worktree,
                           3, Stopping())
        self.assertEqual(settle_pass(self.store_conn, scout_id, outcome), 0)
        self.assertLess(time.monotonic() - started, 30)

        self.assertEqual(self.tasks(scout_id), [(1, QUESTIONS[0], "stopped")])
        self.assertTrue(wait_for(lambda: not running(self.pid_of(pid_file))))
        self.assertEqual(len(self.stubs.turns("report")), 1)
        self.assertEqual(self.reports(scout_id)[2]["cut"], [QUESTIONS[0]])
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.budget_min), ("done", 30))


class FailedPassTests(PassCase):
    def test_a_report_still_malformed_after_repair_fails_only_the_pass(self):
        unsummed = {key: value for key, value in SECOND.items()
                    if key != "summary"}
        self.stubs.answer(pass_plan=plan(QUESTIONS[0]),
                          report=fenced(unsummed), repair=fenced(unsummed))
        scout_id = self.steered()
        code, printed = self.cli("--scout", str(scout_id), str(self.path))
        self.assertEqual(code, 1, printed)

        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason), ("done", REASON))
        (failed,) = self.payloads(scout_id, "scout_pass_failed")
        self.assertIn("summary", failed["reason"])
        self.assertEqual(list(self.reports(scout_id)), [1])
        self.assertEqual(len(self.items(scout_id)), 2)
        self.first_pass_kept(scout_id)


class SteerConfigTests(ConfigFixture):
    def test_the_steer_keys_default_and_refuse_bad_values_by_name(self):
        config = scout_config(self.project())
        self.assertEqual((config.steer_budget_min, config.steer_helpers),
                         (10, 4))
        for table, key in (
                ("steer_budget_min = 0\n", "steer_budget_min"),
                ('steer_helpers = "four"\n', "steer_helpers"),
                ("helper_turns = 5\nsteer_helpers = 6\n", "steer_helpers"),
                ("swarm = 1\nhelper_turns = 1\n", "steer_helpers")):
            with self.subTest(table=table):
                with self.assertRaises(SystemExit) as refused:
                    check_config(self.project(f"[scout]\n{table}"))
                self.assertIn(f"[scout] {key}", str(refused.exception))


class SteerSlotTests(PassCase):
    def test_a_steer_waits_for_a_free_slot_then_starts_its_pass(self):
        with open(Host.locate().path, "a") as registry:
            registry.write("\n[scout]\nmax_parallel = 1\n")
        spawned = []

        def recorder(argv, **_options):
            spawned.append((argv, scout(self.store_conn, int(argv[-2])).phase))
            return SimpleNamespace(pid=4242)

        live = self.planning()
        beat_scout(self.store_conn, live)
        scout_id = self.done_scout()
        self.steer(scout_id)
        with patch.object(scout_sweep, "SPAWN", recorder):
            self.sweep_pass()
            self.assertEqual(spawned, [])
            (waiting,) = messages(self.store_conn, scout_id)
            self.assertIsNone(waiting.consumed_by)

            end_scout(self.store_conn, live, "done", "answered")
            self.sweep_pass()
        ((argv, phase),) = spawned
        self.assertEqual(argv[-3:], ["--scout", str(scout_id), str(self.path)])
        self.assertEqual(phase, "planning")


class StalePassTests(PassCase):
    def test_a_stale_pass_returns_to_done_and_stops_its_running_task(self):
        config = self.target.config_path
        config.write_text(config.read_text()
                          + "[supervisor]\nheartbeat_stale_min = 1\n")
        scout_id = self.steered()
        move_scout(self.store_conn, scout_id, "swarming")
        (task,) = record_scout_tasks(self.store_conn, scout_id, 2, 1, [
            {"question": QUESTIONS[0], "mode": "read"}])
        start_scout_task(self.store_conn, task)
        beat_scout(self.store_conn, scout_id, now=now_ms() - 2 * MINUTE)
        with patch.object(scout_sweep, "SPAWN", None):
            self.sweep_pass()

        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason), ("done", REASON))
        (failed,) = self.payloads(scout_id, "scout_pass_failed")
        self.assertTrue(failed["reason"].startswith("heartbeat stale"),
                        failed["reason"])
        self.assertEqual(self.tasks(scout_id), [(1, QUESTIONS[0], "stopped")])
        self.assertEqual(self.reports(scout_id), {1: REPORT})


class RetriedPassTests(PassCase):
    def test_an_earlier_attempt_of_a_reused_pass_cannot_end_the_retry(self):
        scout_id = self.done_scout()
        take_message(self.store_conn, self.steer(scout_id))
        earlier = current_pass(self.store_conn, scout_id)
        fail_pass(self.store_conn, scout_id, earlier, "heartbeat stale")
        take_message(self.store_conn, self.steer(scout_id, "And the cron?"))
        retry = current_pass(self.store_conn, scout_id)
        self.assertEqual(retry["pass"], earlier["pass"])

        stale = PassOutcome(earlier["pass"], earlier["message"],
                            dict(SECOND, cut=[]))
        for ending in (lambda: settle_pass(self.store_conn, scout_id, stale),
                       lambda: fail_pass(self.store_conn, scout_id, earlier,
                                         "cut"),
                       lambda: stop_pass(self.store_conn, scout_id, earlier,
                                         "stopped", None, None)):
            with self.assertRaises(ScoutRefused):
                ending()
        self.assertEqual(scout(self.store_conn, scout_id).phase, "planning")
        self.assertEqual(self.reports(scout_id), {1: REPORT})
        self.assertEqual(len(self.items(scout_id)), 2)


class EndedPassTests(PassCase):
    def failed_then(self, scout_id, mode, text):
        take_message(self.store_conn, self.steer(scout_id))
        earlier = current_pass(self.store_conn, scout_id)
        fail_pass(self.store_conn, scout_id, earlier, "heartbeat stale")
        take_message(self.store_conn, post_message(
            self.store_conn, scout_id, "maintainer", mode, text))
        return earlier

    def test_a_failed_pass_cannot_settle_over_a_later_answer(self):
        scout_id = self.done_scout()
        earlier = self.failed_then(scout_id, "ask", "Which finding?")
        stale = PassOutcome(earlier["pass"], earlier["message"],
                            dict(SECOND, cut=[]))
        with self.assertRaises(ScoutRefused):
            settle_pass(self.store_conn, scout_id, stale)
        self.assertEqual(scout(self.store_conn, scout_id).phase, "answering")
        self.assertEqual(self.reports(scout_id), {1: REPORT})
        self.assertEqual(len(self.items(scout_id)), 2)

    def test_a_failed_answer_cannot_end_a_later_pass(self):
        scout_id = self.done_scout()
        ask = post_message(self.store_conn, scout_id, "maintainer", "ask",
                           "Which finding?")
        take_message(self.store_conn, ask)
        beat_scout(self.store_conn, scout_id, os.getpid())
        fail_answer(self.store_conn, scout_id, ask, "heartbeat stale")
        take_message(self.store_conn, self.steer(scout_id))

        for ending in (lambda: settle(self.store_conn, scout_id,
                                      Outcome(ask, "F1 explains it.")),
                       lambda: fail_answer(self.store_conn, scout_id, ask,
                                           "cut"),
                       lambda: stop_answer(self.store_conn, scout_id, ask,
                                           "stopped", None, None)):
            with self.assertRaises(ScoutRefused):
                ending()
        self.assertEqual(scout(self.store_conn, scout_id).phase, "planning")
        self.assertEqual(settle_pass(self.store_conn, scout_id, PassOutcome(
            2, current_pass(self.store_conn, scout_id)["message"],
            dict(SECOND, cut=[]))), 0)
        self.assertEqual(self.reports(scout_id)[2], dict(SECOND, cut=[]))

    def test_an_answer_process_stops_beating_a_pass_that_replaced_it(self):
        scout_id = self.done_scout()
        ask = post_message(self.store_conn, scout_id, "maintainer", "ask",
                           "Which finding?")
        take_message(self.store_conn, ask)
        row, beats = scout(self.store_conn, scout_id), []

        def answer_went_stale_and_a_steer_was_taken(*_args):
            subprocess.run([sys.executable, "-c", STALE_ANSWER.format(
                store=self.target.store_path, scout=scout_id, ask=ask)],
                cwd=ROOT, check=True)
            with self.assertRaises(ScoutRefused):
                beat_scout(self.store_conn, scout_id)
            beats.append(scout(self.store_conn, scout_id).heartbeat_at)
            return Outcome(ask, "F1 explains it.")

        with patch.object(scout_process, "answered",
                          answer_went_stale_and_a_steer_was_taken):
            self.assertEqual(answer_mode(self.store_conn, self.target, row,
                                         ask, Stopping()), 1)
        now = scout(self.store_conn, scout_id)
        self.assertEqual((now.phase, now.pid, now.heartbeat_at),
                         ("planning", None, beats[0]))

    def test_a_process_holding_a_replaced_steer_claims_nothing(self):
        scout_id = self.done_scout()
        take_message(self.store_conn, self.steer(scout_id))
        row = scout(self.store_conn, scout_id)
        earlier = current_pass(self.store_conn, scout_id)
        fail_pass(self.store_conn, scout_id, earlier, "heartbeat stale")
        take_message(self.store_conn, self.steer(scout_id, "And the cron?"))

        self.assertEqual(pass_mode(self.store_conn, self.target, row, earlier,
                                   Stopping()), 1)
        now = scout(self.store_conn, scout_id)
        self.assertEqual((now.phase, now.pid), ("planning", None))
        self.assertEqual(self.stubs.calls(), [])

    def test_a_vanished_process_stop_leaves_a_later_pass_running(self):
        scout_id = self.steered()
        beat_scout(self.store_conn, scout_id, os.getpid())
        targeted = current_pass(self.store_conn, scout_id)

        def pass_ended_and_next_steer_taken(_pid, _signal):
            fail_pass(self.store_conn, scout_id, targeted, "the turn ended")
            take_message(self.store_conn, self.steer(scout_id, "And cron?"))
            raise ProcessLookupError

        with patch.object(stop, "runs_scout", return_value=True), \
                patch.object(stop.os, "kill", pass_ended_and_next_steer_taken):
            status, reply = stop.stop(self.target, {
                "scout": scout_id, "note": "steered the wrong way"})
        self.assertEqual((status, reply["ok"]), (200, False), reply)
        self.assertEqual(scout(self.store_conn, scout_id).phase, "planning")
        self.assertNotEqual(current_pass(self.store_conn, scout_id)["message"],
                            targeted["message"])
        self.assertEqual(self.payloads(scout_id, "scout_pass_stopped"), [])


class ReplacedPassTests(PassCase):
    def test_a_stale_process_writes_nothing_once_its_steer_is_replaced(self):
        scout_id = self.done_scout()
        take_message(self.store_conn, self.steer(scout_id))
        row = scout(self.store_conn, scout_id)
        earlier = current_pass(self.store_conn, scout_id)
        replace = self.root / "replace.py"
        replace.write_text(REPLACE.format(store=self.target.store_path,
                                          scout=scout_id))
        self.stubs.answer(pass_plan=plan(QUESTIONS[0]), report=fenced(SECOND),
                          pass_plan_sh=[f"PYTHONPATH={ROOT} {sys.executable}"
                                        f" {replace}"])

        self.assertEqual(pass_mode(self.store_conn, self.target, row, earlier,
                                   Stopping()), 1)
        now = scout(self.store_conn, scout_id)
        self.assertEqual((now.phase, now.pid), ("planning", None))
        self.assertEqual(self.tasks(scout_id), [])
        self.assertEqual(self.stubs.turns("helper"), [])
        self.assertEqual(self.stubs.turns("report"), [])

        self.stubs.answer(pass_plan=plan(QUESTIONS[0]), report=fenced(SECOND))
        self.assertEqual(pass_mode(self.store_conn, self.target, now,
                                   current_pass(self.store_conn, scout_id),
                                   Stopping()), 0)
        self.assertEqual(self.reports(scout_id)[2], dict(SECOND, cut=[]))
        self.assertEqual(scout(self.store_conn, scout_id).phase, "done")


    def test_a_stale_process_beat_is_refused_once_its_steer_is_replaced(self):
        scout_id = self.done_scout()
        take_message(self.store_conn, self.steer(scout_id))
        claim_scout(self.store_conn, scout_id, os.getpid())
        fail_pass(self.store_conn, scout_id,
                  current_pass(self.store_conn, scout_id), "heartbeat stale")
        take_message(self.store_conn, self.steer(scout_id, "And the cron?"))
        beaten = scout(self.store_conn, scout_id).heartbeat_at
        with holding_scout(self.store_conn, scout_id, os.getpid()):
            with self.assertRaises(ScoutRefused):
                beat_scout(self.store_conn, scout_id, now=beaten + MINUTE)
        self.assertEqual(scout(self.store_conn, scout_id).heartbeat_at, beaten)


class StoppedPassTests(PassCase):
    def test_a_stop_kills_the_pass_helper_and_returns_the_scout_to_done(self):
        pid_file = self.child("helper-child")
        self.stubs.answer(pass_plan=plan(QUESTIONS[0]), report=fenced(SECOND),
                          helper_sleep=600, helper_pid_file=str(pid_file))
        self.start_host()
        scout_id = self.steered()
        proc = self.spawn_scout(scout_id)
        child = self.pid_of(pid_file)
        status, reply = self.stop(scout_id, "steered the wrong way")
        self.assertEqual((status, reply["ok"], reply["pid"]),
                         (200, True, proc.pid), reply)
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)

        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason), ("done", REASON))
        self.assertTrue(wait_for(lambda: not running(child)))
        self.assertEqual(self.tasks(scout_id), [(1, QUESTIONS[0], "stopped")])
        (stopped,) = self.payloads(scout_id, "scout_pass_stopped")
        self.assertEqual((stopped["author"], stopped["note"]),
                         ("maintainer", "steered the wrong way"))
        goals = [call["argv"][-1] for call in self.stubs.calls()]
        self.assertFalse([goal for goal in goals
                          if goal.startswith(REPORT_OPENING)])
        self.assertEqual(list(self.reports(scout_id)), [1])

    def test_a_stop_with_no_process_returns_the_pass_to_done(self):
        self.start_host()
        scout_id = self.steered()
        status, reply = self.stop(scout_id)
        self.assertEqual((status, reply["ok"], reply["pid"]),
                         (200, True, None), reply)
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason), ("done", REASON))
        (summary,) = [summary for (summary,) in self.store_conn.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ? AND kind = ?",
            (scout_id, "scout_pass_stopped"))]
        self.assertIn("no scout process was running", summary)

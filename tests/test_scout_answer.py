"""A finished scout answering an ask: the host sweep takes it under the
scout slots, `factory.py --scout ID PROJECT` re-creates the lead worktree
and resumes the lead's session for one answer turn, against stub lead CLIs
over real git repositories.

Run: python3 -m unittest discover -s tests -p 'test_scout_answer.py' -v
"""
import json
import os
import re
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import holophyte.scout.scout_sweep as scout_sweep
import holophyte.scout.stop as stop
from holophyte.config.project import Project
from holophyte.host.registry import Host
from holophyte.scout.answer import (
    ANSWER_OPENING,
    Outcome,
    answer,
    current_ask,
    fail_answer,
    labels,
    settle,
)
from holophyte.scout.lead import PLAN_OPENING, REPORT_OPENING
from holophyte.scout.run_helpers import HELPER_OPENING
from holophyte.scout.scout_process import answer_mode, answer_stopped, make_lead
from holophyte.scout.swarm import Stopping
from store.scout_thread import messages, post_message, take_message
from store.scouts import (
    ScoutRefused,
    beat_scout,
    create_scout,
    end_scout,
    move_scout,
    record_scout_actions,
    record_scout_report,
    scout,
    set_scout_session,
)
from tests.host_fixture import git
from tests.scout_stubs import fenced
from tests.test_scout_lead import CLAUDE_LEAD, CODEX_LEAD, SECRET, running
from tests.test_scout_process import MINUTE, now_ms
from tests.test_scout_stop import WAIT_S, StopCase, wait_for

REASON = "the lead wrote its report"
FINDINGS = [{"title": "the retry loop never sleeps", "status": "shown",
             "evidence": [{"file": "notes.txt", "line": 1}]},
            {"title": "the timeout is read twice", "status": "likely",
             "evidence": [{"url": "https://example.test/timeout"}]}]
ACTIONS = [{"title": "add a backoff to the retry loop", "why": "it spins",
            "kind": "fix", "body": "Add a backoff."}]
REPORT = {"summary": "the retry loop spins", "findings": FINDINGS,
          "actions": ACTIONS, "cut": []}
QUESTION = "Which finding explains the spin?"
TEXT = "F2 shows the timeout read twice; A1 fixes the loop."


def reaped(pid):
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return True


class AnswerCase(StopCase):
    """`alpha` with a Claude lead and a `done` scout holding a pass 1
    report of two findings and one action item."""

    config = CLAUDE_LEAD

    def setUp(self):
        super().setUp()
        target = Project.locate(self.path, adopt=False)
        target.config_path.write_text(target.config_path.read_text()
                                      + self.config)
        self.target = target
        self.stubs.answer(answer=fenced({"answer": TEXT, "refs": ["F2"]}))

    def done_scout(self, session="S", created=None, report=REPORT):
        (project,) = self.store_conn.execute(
            "SELECT id FROM projects").fetchone()
        scout_id = create_scout(self.store_conn, project, "why does the"
                                " retry spin?", "research", 30, now=created)
        move_scout(self.store_conn, scout_id, "planning",
                   base_sha=git(self.path, "rev-parse", "main~1"))
        if session is not None:
            set_scout_session(self.store_conn, scout_id, session)
        record_scout_report(self.store_conn, scout_id, 1, report)
        record_scout_actions(self.store_conn, scout_id, 1, ACTIONS)
        end_scout(self.store_conn, scout_id, "done", REASON)
        return scout_id

    def ask(self, scout_id, text=QUESTION):
        return post_message(self.store_conn, scout_id, "maintainer", "ask",
                            text)

    def reports(self, scout_id):
        return [json.loads(report) for (report,) in self.store_conn.execute(
            "SELECT report FROM scoutReports WHERE scoutId = ?", (scout_id,))]

    def answers(self, scout_id):
        return [message for message in messages(self.store_conn, scout_id)
                if message.mode == "answer"]

    def payloads(self, scout_id, kind):
        return [payload for event, payload in self.events(scout_id)
                if event == kind]

    def answer_step(self, scout_id, cap=300):
        """The process's answer mode in this process, with `cap` seconds
        for the turn."""
        row = scout(self.store_conn, scout_id)
        worktree = make_lead(self.target, scout_id, row.base_sha)
        outcome = answer(self.store_conn, self.target, row,
                         current_ask(self.store_conn, scout_id), worktree,
                         Stopping(), cap)
        return settle(self.store_conn, scout_id, outcome)

    def answering(self, text=QUESTION, session="S"):
        scout_id = self.done_scout(session)
        take_message(self.store_conn, self.ask(scout_id, text))
        return scout_id

    def answer_goal(self):
        (call,) = self.stubs.turns("answer")
        return call["argv"][-1]


class SweptAnswerTests(AnswerCase):
    def test_a_sweep_answers_an_ask_from_the_resumed_lead_session(self):
        session = str(uuid.uuid4())
        scout_id = self.done_scout(session)
        before = scout(self.store_conn, scout_id)
        lead = make_lead(self.target, scout_id, before.base_sha)
        (lead / "stray.txt").write_text("left by an earlier run")
        ask = self.ask(scout_id)

        _, printed = self.cli("--supervise", "--once")
        self.assertIn(f"answering message {ask}", printed)
        self.assertTrue(wait_for(lambda: "scout_answered" in [
            kind for kind, _ in self.events(scout_id)], 60),
            self.events(scout_id))
        self.assertTrue(wait_for(
            lambda: scout(self.store_conn, scout_id).phase == "done"))
        (pid,) = map(int, re.findall(r"answering message \d+ as pid (\d+)",
                                     printed))
        self.assertTrue(wait_for(lambda: reaped(pid), 30))

        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.ended_at, row.reason, row.lead_session),
                         (before.ended_at, REASON, session))
        self.assertEqual(self.reports(scout_id), [REPORT])
        asked, answered = messages(self.store_conn, scout_id)
        self.assertEqual((asked.id, asked.mode), (ask, "ask"))
        self.assertEqual((answered.author, answered.mode, answered.answers_id,
                          answered.text), ("scout", "answer", ask, TEXT))
        events = self.store_conn.execute(
            "SELECT id, kind FROM runEvents WHERE scoutId = ? ORDER BY seq",
            (scout_id,)).fetchall()
        kinds = [kind for _, kind in events]
        started = kinds.index("scout_answer_started")
        self.assertLess(started, kinds.index("agent_turn"))
        self.assertLess(kinds.index("agent_turn"), kinds.index("scout_answered"))
        self.assertEqual(asked.consumed_by, events[started][0])

        (turn,) = self.stubs.turns("answer")
        self.assertEqual(turn["argv"][turn["argv"].index("--resume") + 1],
                         session)
        self.assertEqual(turn["cwd"], str(lead))
        self.assertFalse((lead / "stray.txt").exists())
        listed = git(self.path, "worktree", "list", "--porcelain").split("\n\n")
        (entry,) = [entry for entry in listed
                    if entry.startswith(f"worktree {lead}\n")]
        self.assertIn(f"HEAD {before.base_sha}", entry)
        goals = [call["argv"][-1] for call in self.stubs.calls()]
        self.assertEqual([goal for goal in goals if goal.startswith(
            (PLAN_OPENING, HELPER_OPENING, REPORT_OPENING))], [])


class AnswerStepTests(AnswerCase):
    def test_the_goal_labels_the_report_and_the_answer_cites_known_labels(self):
        config = self.target.config_path
        config.write_text(config.read_text()
                          + f'[linear]\napi_key = "{SECRET}"\n')
        self.stubs.answer(answer=fenced({"answer": TEXT,
                                         "refs": ["F2", "A1", "F9"]}))
        scout_id = self.answering(f"{QUESTION} The key is {SECRET}.")
        self.assertEqual(self.answer_step(scout_id), 0)

        lines = self.answer_goal().splitlines()
        self.assertTrue(lines[0].startswith(ANSWER_OPENING))
        self.assertTrue([line for line in lines if QUESTION in line])
        for label, title in (("F1", FINDINGS[0]["title"]),
                             ("F2", FINDINGS[1]["title"]),
                             ("A1", ACTIONS[0]["title"])):
            self.assertTrue([line for line in lines
                             if line.startswith(f"{label} ") and title in line],
                            label)
        self.assertFalse([arg for call in self.stubs.calls()
                          for arg in call["argv"] if SECRET in arg])
        (answered,) = self.answers(scout_id)
        self.assertEqual(answered.blocks,
                         [{"type": "finding", "pass": 1, "index": 2},
                          {"type": "action", "position": 1}])
        (event,) = self.payloads(scout_id, "scout_answered")
        self.assertEqual(event["unknown_refs"], ["F9"])
        self.assertEqual(scout(self.store_conn, scout_id).phase, "done")

    def test_the_goal_carries_each_stored_report_in_full(self):
        report = dict(REPORT, cut=["the CI logs were not read"],
                      reproduction={"level": "not_reproduced",
                                    "hypotheses": [{"claim": "a timer race"}]})
        scout_id = self.done_scout(report=report)
        take_message(self.store_conn, self.ask(scout_id))
        self.assertEqual(self.answer_step(scout_id), 0)
        goal = self.answer_goal()
        for carried in ("the CI logs were not read", "a timer race",
                        ACTIONS[0]["body"], FINDINGS[0]["evidence"][0]["file"]):
            self.assertIn(carried, goal)

    def test_an_obsolete_attempt_cannot_settle_a_newer_ask(self):
        scout_id = self.done_scout()
        first = self.ask(scout_id)
        take_message(self.store_conn, first)
        fail_answer(self.store_conn, scout_id, first, "heartbeat stale")
        second = self.ask(scout_id)
        take_message(self.store_conn, second)

        for obsolete in (Outcome(first, TEXT), Outcome(first, failure="cut")):
            with self.assertRaises(ScoutRefused):
                settle(self.store_conn, scout_id, obsolete)
        self.assertEqual(scout(self.store_conn, scout_id).phase, "answering")
        self.assertEqual(self.answers(scout_id), [])

        self.assertEqual(settle(self.store_conn, scout_id,
                                Outcome(second, TEXT)), 0)
        (answered,) = self.answers(scout_id)
        self.assertEqual(answered.answers_id, second)
        self.assertEqual(scout(self.store_conn, scout_id).phase, "done")

    def test_a_process_whose_ask_went_stale_runs_no_turn_for_a_newer_ask(self):
        scout_id = self.done_scout()
        first = self.ask(scout_id)
        take_message(self.store_conn, first)
        row = scout(self.store_conn, scout_id)
        fail_answer(self.store_conn, scout_id, first, "heartbeat stale")
        second = self.ask(scout_id)
        take_message(self.store_conn, second)

        self.assertEqual(answer_mode(self.store_conn, self.target, row, first,
                                     Stopping()), 1)
        self.assertEqual(self.stubs.turns("answer"), [])
        self.assertEqual(self.answers(scout_id), [])
        now = scout(self.store_conn, scout_id)
        self.assertEqual((now.phase, now.pid), ("answering", None))

    def test_an_answer_without_json_or_cut_at_its_cap_leaves_the_report(self):
        pid_file = self.child("answer-child")
        for case, answers, cap, named in (
                ("no json", {"answer": "The loop spins."}, 300,
                 "no JSON object"),
                ("cap", {"answer": fenced({"answer": TEXT, "refs": []}),
                         "answer_sleep": 60,
                         "answer_pid_file": str(pid_file)}, 3, "time limit")):
            with self.subTest(case=case):
                self.stubs.answer(**answers)
                scout_id = self.answering()
                started = time.monotonic()
                self.assertEqual(self.answer_step(scout_id, cap), 1)
                self.assertLess(time.monotonic() - started, 30)
                self.assertEqual(scout(self.store_conn, scout_id).phase, "done")
                self.assertEqual(self.answers(scout_id), [])
                (failed,) = self.payloads(scout_id, "scout_answer_failed")
                self.assertIn(named, failed["reason"])
                self.assertEqual(self.reports(scout_id), [REPORT])
        self.assertTrue(wait_for(lambda: not running(self.pid_of(pid_file))))

    def test_a_lead_route_failing_its_probe_runs_no_answer_turn(self):
        self.stubs.answer(probe="not today")
        scout_id = self.answering()
        self.assertEqual(self.answer_step(scout_id), 1)
        self.assertEqual(scout(self.store_conn, scout_id).phase, "done")
        (failed,) = self.payloads(scout_id, "scout_answer_failed")
        self.assertRegex(failed["reason"], r"^lead route opus is down: \S")
        self.assertEqual(self.stubs.turns("answer"), [])
        self.assertEqual(self.answers(scout_id), [])


class CodexAnswerTests(AnswerCase):
    config = CODEX_LEAD

    def test_a_codex_lead_resumes_its_session_or_starts_fresh_with_the_ask(self):
        session = str(uuid.uuid4())
        scout_id = self.answering(session=session)
        self.assertEqual(self.answer_step(scout_id), 0)
        (call,) = self.stubs.turns("answer")
        argv = [Path(call["argv"][0]).name, *call["argv"][1:]]
        self.assertEqual(argv[:2], ["codex", "exec"])
        sandbox = argv.index("--sandbox")
        self.assertEqual(argv[sandbox + 1], "read-only")
        self.assertLess(sandbox, argv.index("resume"))
        self.assertLess(argv.index("resume"), argv.index(session))

        (self.stubs.directory / "calls.jsonl").unlink()
        scout_id = self.answering(session=None)
        self.assertEqual(self.answer_step(scout_id), 0)
        (call,) = self.stubs.turns("answer")
        self.assertEqual([Path(call["argv"][0]).name, call["argv"][1]],
                         ["codex", "exec"])
        self.assertNotIn("resume", call["argv"])
        self.assertIn("Ask: why does the retry spin?", call["argv"][-1])


class AnswerSlotTests(AnswerCase):
    def setUp(self):
        super().setUp()
        with open(Host.locate().path, "a") as registry:
            registry.write("\n[scout]\nmax_parallel = 1\n")
        self.spawned = []

        def recorder(argv, **_options):
            scout_id = int(argv[-2])
            self.spawned.append((argv, scout(self.store_conn, scout_id).phase))
            return SimpleNamespace(pid=4242)

        patcher = patch.object(scout_sweep, "SPAWN", recorder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def live_scout(self):
        scout_id = self.planning()
        beat_scout(self.store_conn, scout_id)
        return scout_id

    def test_an_ask_waits_for_a_free_slot_then_starts_its_scout_answering(self):
        live = self.live_scout()
        scout_id = self.done_scout()
        ask = self.ask(scout_id)
        self.sweep_pass()
        self.assertEqual(self.spawned, [])
        (asked,) = messages(self.store_conn, scout_id)
        self.assertIsNone(asked.consumed_by)

        end_scout(self.store_conn, live, "done", "answered")
        self.sweep_pass()
        ((argv, phase),) = self.spawned
        self.assertEqual(argv[-3:], ["--scout", str(scout_id), str(self.path)])
        self.assertEqual(phase, "answering")
        (asked,) = messages(self.store_conn, scout_id)
        self.assertEqual(asked.id, ask)
        self.assertIsNotNone(asked.consumed_by)

    def test_a_queued_scout_created_before_the_ask_takes_the_slot_first(self):
        live = self.live_scout()
        (project,) = self.store_conn.execute(
            "SELECT id FROM projects").fetchone()
        queued = create_scout(self.store_conn, project, "where else?",
                              "research", 30, now=1000)
        scout_id = self.done_scout()
        self.ask(scout_id)
        end_scout(self.store_conn, live, "done", "answered")
        self.sweep_pass()
        ((argv, phase),) = self.spawned
        self.assertEqual((argv[-2], phase), (str(queued), "planning"))
        (asked,) = messages(self.store_conn, scout_id)
        self.assertIsNone(asked.consumed_by)

    def test_an_ask_waits_untaken_while_the_project_config_is_refused(self):
        config = self.target.config_path
        config.write_text(config.read_text().replace(
            "[scout]\n", "[scout]\nwaves = 3\n", 1))
        scout_id = self.done_scout()
        self.ask(scout_id)
        printed = self.sweep_pass()
        self.assertEqual(self.spawned, [])
        self.assertIn("[scout] waves must be 1 or 2", printed)
        self.assertEqual(scout(self.store_conn, scout_id).phase, "done")
        (asked,) = messages(self.store_conn, scout_id)
        self.assertIsNone(asked.consumed_by)

    def test_an_ask_older_than_a_queued_scout_takes_the_slot_first(self):
        live = self.live_scout()
        scout_id = self.done_scout()
        self.ask(scout_id)
        (project,) = self.store_conn.execute(
            "SELECT id FROM projects").fetchone()
        queued = create_scout(self.store_conn, project, "where else?",
                              "research", 30, now=now_ms() + MINUTE)
        end_scout(self.store_conn, live, "done", "answered")
        self.sweep_pass()
        ((argv, phase),) = self.spawned
        self.assertEqual((argv[-2], phase), (str(scout_id), "answering"))
        self.assertEqual(scout(self.store_conn, queued).phase, "queued")


class StaleAnswerTests(AnswerCase):
    def test_a_stale_answer_returns_to_done_where_a_stale_run_fails(self):
        config = self.target.config_path
        config.write_text(config.read_text()
                          + "[supervisor]\nheartbeat_stale_min = 1\n")
        answering = self.answering()
        swarming = self.planning()
        move_scout(self.store_conn, swarming, "swarming")
        for scout_id in (answering, swarming):
            beat_scout(self.store_conn, scout_id, now=now_ms() - 2 * MINUTE)
        with patch.object(scout_sweep, "SPAWN", None):
            self.sweep_pass()
        row = scout(self.store_conn, answering)
        self.assertEqual((row.phase, row.reason), ("done", REASON))
        self.assertEqual(self.reports(answering), [REPORT])
        (failed,) = self.payloads(answering, "scout_answer_failed")
        self.assertTrue(failed["reason"].startswith("heartbeat stale"),
                        failed["reason"])
        self.assertEqual(scout(self.store_conn, swarming).phase, "failed")


class StoppedAnswerTests(AnswerCase):
    def test_a_stop_kills_the_answer_turn_and_returns_the_scout_to_done(self):
        pid_file = self.child("answer-child")
        self.stubs.answer(answer=fenced({"answer": TEXT, "refs": []}),
                          answer_sleep=600, answer_pid_file=str(pid_file))
        self.start_host()
        scout_id = self.answering()
        proc = self.spawn_scout(scout_id)
        child = self.pid_of(pid_file)
        status, reply = self.stop(scout_id, "asked the wrong scout")
        self.assertEqual((status, reply["ok"], reply["pid"]),
                         (200, True, proc.pid), reply)
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason), ("done", REASON))
        self.assertTrue(wait_for(lambda: not running(child)))
        (stopped,) = self.payloads(scout_id, "scout_answer_stopped")
        self.assertEqual((stopped["author"], stopped["note"]),
                         ("maintainer", "asked the wrong scout"))
        self.assertEqual(self.answers(scout_id), [])

    def test_a_stop_with_no_process_returns_the_scout_to_done(self):
        self.start_host()
        scout_id = self.answering()
        status, reply = self.stop(scout_id)
        self.assertEqual((status, reply["ok"], reply["pid"]),
                         (200, True, None), reply)
        row = scout(self.store_conn, scout_id)
        self.assertEqual((row.phase, row.reason), ("done", REASON))
        (summary,) = [summary for (summary,) in self.store_conn.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ? AND kind = ?",
            (scout_id, "scout_answer_stopped"))]
        self.assertIn("no scout process was running", summary)

    def test_a_stop_whose_process_ended_leaves_a_newer_answer_running(self):
        scout_id = self.answering()
        beat_scout(self.store_conn, scout_id, os.getpid())
        first = current_ask(self.store_conn, scout_id)

        def old_process_gone_and_next_ask_taken(_pid, _signal):
            fail_answer(self.store_conn, scout_id, first, "the turn ended")
            take_message(self.store_conn, self.ask(scout_id, "And the loop?"))
            raise ProcessLookupError

        with patch.object(stop, "runs_scout", return_value=True), \
                patch.object(stop.os, "kill",
                             old_process_gone_and_next_ask_taken):
            status, reply = stop.stop(self.target, {
                "scout": scout_id, "note": "asked the wrong scout"})
        self.assertEqual((status, reply["ok"]), (200, False), reply)
        self.assertEqual(scout(self.store_conn, scout_id).phase, "answering")
        self.assertNotEqual(current_ask(self.store_conn, scout_id), first)
        self.assertEqual(self.payloads(scout_id, "scout_answer_stopped"), [])

    def test_a_signal_stop_of_a_later_ask_takes_no_earlier_stops_note(self):
        self.start_host()
        scout_id = self.answering()
        self.stop(scout_id, "asked the wrong scout")
        take_message(self.store_conn, self.ask(scout_id, "And the loop?"))
        later = current_ask(self.store_conn, scout_id)
        self.assertEqual(answer_stopped(self.store_conn, scout_id, later), 0)
        (_, stopped) = self.payloads(scout_id, "scout_answer_stopped")
        self.assertEqual((stopped["reason"], stopped["author"],
                          stopped["note"]), (stop.BY_SIGNAL, None, None))
        self.assertEqual(scout(self.store_conn, scout_id).phase, "done")


class LabelTests(unittest.TestCase):
    def test_findings_count_on_across_passes_and_index_within_their_pass(self):
        second = {"findings": [{"title": "a third finding"}]}
        labelled = labels([(1, REPORT), (2, second)], [(1, ACTIONS[0])])
        self.assertEqual(list(labelled), ["F1", "F2", "F3", "A1"])
        self.assertEqual(labelled["F3"],
                         {"type": "finding", "pass": 2, "index": 1})
        self.assertEqual(labelled["F2"],
                         {"type": "finding", "pass": 1, "index": 2})

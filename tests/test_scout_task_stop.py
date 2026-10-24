"""Stopping one helper task of a running scout: `stop()` with a `task`
against real `factory.py --scout ID PROJECT` processes running stub lead
and helper CLIs, `holo scout stop --task` over the local transport, and the
`holo mcp` `scout_stop` tool's schema.

Run: python3 -m unittest discover -s tests -p 'test_scout_task_stop.py' -v
"""
import os
import subprocess
import sys
import time

from holophyte.config.project import Project
from holophyte.scout.lead import REPORT_OPENING
from holophyte.scout.stop import stop
from store.scouts import (
    claim_scout,
    end_scout,
    end_scout_task,
    move_scout,
    record_scout_tasks,
    scout,
    start_scout_task,
)
from tests.scout_stubs import fenced
from tests.test_holo_mcp import McpCase
from tests.test_scout_lead import running
from tests.test_scout_stop import REPORT, ROOT, WAIT_S, StopCase, wait_for

STOP_WITHIN_S = 15
SLOW, FAST = "slow question", "fast question"


class TaskStopCase(StopCase):
    def target(self):
        return Project.locate(self.path, adopt=False)

    def ask(self, scout_id, task_id, **extra):
        return stop(self.target(), {"scout": scout_id, "task": task_id,
                                    **extra})

    def tasks(self, scout_id):
        """Each task's question mapped to its id, phase and excerpt."""
        return {question: (task_id, phase, excerpt)
                for task_id, question, phase, excerpt in
                self.store_conn.execute(
                    "SELECT id, question, phase, excerpt FROM scoutTasks"
                    " WHERE scoutId = ? ORDER BY id", (scout_id,))}

    def phase_of(self, scout_id, question):
        return self.tasks(scout_id).get(question, (None, None, None))[1]

    def swarming(self, questions, helpers, kind="research", mode="read",
                 swarm=2):
        self.stubs.answer(
            plan=fenced({"tasks": [{"question": question, "paths": [],
                                    "mode": mode} for question in questions]}),
            report=fenced(REPORT), helpers=helpers)
        scout_id = self.planning(kind=kind, swarm=swarm,
                                 helper_turns=len(questions), waves=1)
        return scout_id, self.spawn_scout(scout_id)

    def slow_and_fast(self):
        pid_file = self.child("slow-helper")
        scout_id, proc = self.swarming([SLOW, FAST], {
            SLOW: {"helper": "slow so far", "helper_sleep": 60,
                   "helper_pid_file": str(pid_file)},
            FAST: {"helper": "fast answer"}})
        child = self.pid_of(pid_file)
        self.assertTrue(wait_for(
            lambda: self.phase_of(scout_id, SLOW) == "running"),
            self.tasks(scout_id))
        return scout_id, proc, child, self.tasks(scout_id)[SLOW][0]

    def task_events(self, scout_id):
        return [payload for kind, payload in self.events(scout_id)
                if kind == "scout_task_stop_requested"]


class RunningTaskStopTests(TaskStopCase):
    def test_a_stopped_read_helper_ends_stopped_and_the_scout_reports_it(self):
        scout_id, proc, child, slow = self.slow_and_fast()
        asked = time.monotonic()
        status, answer = self.ask(scout_id, slow)
        self.assertEqual((status, answer["ok"]), (200, True), answer)
        self.assertTrue(wait_for(
            lambda: self.phase_of(scout_id, SLOW) == "stopped",
            STOP_WITHIN_S), self.tasks(scout_id))
        self.assertLess(time.monotonic() - asked, STOP_WITHIN_S)
        self.assertIn("slow so far", self.tasks(scout_id)[SLOW][2])
        self.assertTrue(wait_for(lambda: not running(child), STOP_WITHIN_S))
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)
        self.assertEqual(self.tasks(scout_id)[FAST][1], "done")
        self.assertEqual(scout(self.store_conn, scout_id).phase, "done")
        (report,) = [call["argv"][-1] for call in self.stubs.turns("report")]
        self.assertTrue(report.startswith(REPORT_OPENING))
        self.assertIn("[stopped] slow question", report)

    def test_a_task_stop_without_a_note_answers_and_records_a_null_note(self):
        scout_id, proc, _, slow = self.slow_and_fast()
        status, answer = self.ask(scout_id, slow)
        self.assertEqual(status, 200, answer)
        self.assertEqual(
            (answer["ok"], answer["scout"], answer["task"], answer["pid"],
             answer["detail"]),
            (True, scout_id, slow, None,
             f"scout {scout_id} stops helper task {slow}"))
        payload = self.task_events(scout_id)[-1]
        self.assertEqual((payload["task"], payload["note"], payload["via"]),
                         (slow, None, "POST /actions/scout-stop"))
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)

    def test_a_queued_task_asked_to_stop_is_never_launched(self):
        questions = ["first question", "second question", "third question"]
        scout_id, proc = self.swarming(
            questions, {questions[0]: {"helper": "first", "helper_sleep": 8}},
            swarm=1)
        self.assertTrue(wait_for(
            lambda: self.phase_of(scout_id, questions[0]) == "running"),
            self.tasks(scout_id))
        third = self.tasks(scout_id)[questions[2]][0]
        status, answer = self.ask(scout_id, third, note="not needed")
        self.assertEqual((status, answer["ok"]), (200, True), answer)
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)
        tasks = self.tasks(scout_id)
        self.assertEqual(tasks[questions[2]][1:], ("stopped", ""))
        self.assertEqual(tasks[questions[1]][1], "done")
        self.assertNotIn(questions[2], [call["question"] for call
                                        in self.stubs.turns("helper")])

    def test_a_stopped_run_helper_turn_is_killed_and_ends_stopped(self):
        pid_file = self.child("run-helper")
        scout_id, proc = self.swarming([SLOW], {SLOW: {
            "helper_sleep": 60, "helper_pid_file": str(pid_file)}},
            kind="bug", mode="run", swarm=1)
        child = self.pid_of(pid_file)
        self.assertTrue(wait_for(
            lambda: self.phase_of(scout_id, SLOW) == "running"),
            self.tasks(scout_id))
        status, answer = self.ask(scout_id, self.tasks(scout_id)[SLOW][0])
        self.assertEqual((status, answer["ok"]), (200, True), answer)
        self.assertTrue(wait_for(
            lambda: self.phase_of(scout_id, SLOW) == "stopped",
            STOP_WITHIN_S), self.tasks(scout_id))
        self.assertTrue(wait_for(lambda: not running(child), STOP_WITHIN_S))
        proc.wait(timeout=WAIT_S)


class RefusedTaskStopTests(TaskStopCase):
    def with_task(self, scout_id):
        (task_id,) = record_scout_tasks(self.store_conn, scout_id, 1, 1, [
            {"question": SLOW, "mode": "read"}])
        return task_id

    def test_refused_task_stops_name_why_and_write_nothing(self):
        done = self.planning()
        done_task = self.with_task(done)
        end_scout(self.store_conn, done, "done", "answered")
        live = self.planning()
        claim_scout(self.store_conn, live, os.getpid())
        move_scout(self.store_conn, live, "swarming")
        ended_task = self.with_task(live)
        start_scout_task(self.store_conn, ended_task)
        end_scout_task(self.store_conn, ended_task, "done", excerpt="found")
        elsewhere = self.planning(host="other-writer")
        claim_scout(self.store_conn, elsewhere, os.getpid(),
                    host="other-writer")
        move_scout(self.store_conn, elsewhere, "swarming")
        elsewhere_task = self.with_task(elsewhere)
        before = self.written()

        for scout_id, task_id, named in ((done, done_task, "done"),
                                         (live, ended_task, "done"),
                                         (elsewhere, elsewhere_task,
                                          "other-writer")):
            with self.subTest(scout=scout_id, task=task_id):
                status, answer = self.ask(scout_id, task_id)
                self.assertEqual((status, answer["ok"]), (200, False), answer)
                self.assertIn(named, answer["detail"])
        status, answer = self.ask(live, done_task)
        self.assertEqual((status, answer["error"]), (404, "no such task"))
        self.assertEqual(self.written(), before)

    def test_a_swarming_scout_with_no_running_process_is_refused(self):
        sleeper = subprocess.Popen(["sleep", "600"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        scout_id = self.planning()
        claim_scout(self.store_conn, scout_id, sleeper.pid)
        move_scout(self.store_conn, scout_id, "swarming")
        task_id = self.with_task(scout_id)
        before = self.written()
        status, answer = self.ask(scout_id, task_id)
        self.assertEqual((status, answer["ok"], answer["detail"]),
                         (200, False, f"scout {scout_id} has no running"
                                      f" process to stop task {task_id}"))
        self.assertEqual(scout(self.store_conn, scout_id).phase, "swarming")
        self.assertEqual(self.written(), before)


class HoloTaskStopTests(TaskStopCase):
    def holo(self, *argv):
        env = dict(os.environ, HOLO_TRANSPORT="local")
        return subprocess.run([sys.executable, "-m", "holophyte.holo", *argv],
                              cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=120)

    def test_holo_stops_one_task_and_still_wants_a_note_without_one(self):
        scout_id, proc, _, slow = self.slow_and_fast()
        ran = self.holo("scout", "stop", str(scout_id), "--task", str(slow),
                        "-p", str(self.path))
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertIn(f"scout {scout_id} stops helper task {slow}", ran.stdout)
        self.assertEqual(proc.wait(timeout=WAIT_S), 0)

        ran = self.holo("scout", "stop", str(scout_id), "-p", str(self.path))
        self.assertEqual(ran.returncode, 2, ran.stderr)
        self.assertIn("holo scout stop's note must say why",
                      ran.stdout + ran.stderr)


class McpTaskStopTests(McpCase):
    def test_the_scout_stop_tool_takes_an_integer_task(self):
        async def use(client, _):
            return (await client.list_tools()).tools
        (tool,) = [tool for tool in self.session(use)
                   if tool.name == "scout_stop"]
        self.assertEqual(tool.input_schema["properties"]["task"]["type"],
                         "integer")

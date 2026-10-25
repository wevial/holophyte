"""A scout's helper swarm: its waves, caps, time budget, worktrees and
records, run by `factory.py --scout ID PROJECT` against stub lead and
helper CLIs over a real git repository.

Run: python3 -m unittest discover -s tests -p 'test_scout_swarm.py' -v
"""
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import holophyte.scout.lead as lead
import holophyte.scout.scout_process as scout_process
import holophyte.scout.swarm as swarm
import store
from store.scouts import create_scout, move_scout, scout
from tests.host_fixture import git
from tests.scout_stubs import fenced, titled
from tests.test_scout_lead import running
from tests.test_scout_process import ScoutFixture

REPORT = {"summary": "the retry loop never backs off", "findings": [],
          "actions": []}
QUESTIONS = ["Where is the retry loop?", "Who calls the retry loop?",
             "What does the retry test assert?", "Where is the backoff set?",
             "Which commit added the retry?"]


def plan(*questions):
    return fenced({"tasks": [titled({"question": question,
                                      "paths": ["notes.txt"], "mode": "read"})
                             for question in questions]})


class ScoutSwarmTests(ScoutFixture):
    def research_scout(self, **counts):
        path = self.project("alpha")
        conn = self.conn(path)
        (project,) = conn.execute("SELECT id FROM projects").fetchone()
        scout_id = create_scout(conn, project, "why does the retry spin?",
                                "research", 30, **counts)
        move_scout(conn, scout_id, "planning",
                   base_sha=git(path, "rev-parse", "main"))
        return path, conn, scout_id

    def run_scout(self, path, scout_id):
        code, printed = self.cli("--scout", str(scout_id), str(path))
        self.assertEqual(code, 0, printed)

    def tasks(self, conn, scout_id):
        return conn.execute(
            "SELECT wave, question, phase, route, tokens, durationMs"
            " FROM scoutTasks WHERE scoutId = ? ORDER BY id",
            (scout_id,)).fetchall()

    def report(self, conn, scout_id):
        (report,) = conn.execute("SELECT report FROM scoutReports"
                                 " WHERE scoutId = ?", (scout_id,)).fetchone()
        return json.loads(report)

    def test_a_wave_runs_no_more_helpers_at_once_than_the_swarm_size(self):
        path, conn, scout_id = self.research_scout(swarm=2, helper_turns=6,
                                                   waves=1)
        shared = self.root / "shared"
        shared.mkdir()
        self.stubs.answer(plan=plan(*QUESTIONS), report=fenced(REPORT),
                          helper_shared=str(shared), helper_sleep=1)
        self.run_scout(path, scout_id)
        self.assertEqual([phase for _, _, phase, *_ in self.tasks(conn, scout_id)],
                         ["done"] * 5)
        seen = [helper["present"] for helper in self.stubs.helpers()]
        self.assertEqual(len(seen), 5)
        self.assertEqual(max(seen), 2)

    def test_tasks_past_the_helper_total_stay_queued_and_are_cut(self):
        path, conn, scout_id = self.research_scout(swarm=2, helper_turns=2)
        self.stubs.answer(plan=plan(*QUESTIONS[:4]), report=fenced(REPORT))
        self.run_scout(path, scout_id)
        self.assertEqual(len(self.stubs.turns("helper")), 2)
        phases = [phase for _, _, phase, *_ in self.tasks(conn, scout_id)]
        self.assertEqual(phases, ["done", "done", "queued", "queued"])
        self.assertEqual(self.report(conn, scout_id)["cut"], QUESTIONS[2:4])

    def test_a_helper_past_the_budget_is_stopped_and_its_group_killed(self):
        path, conn, scout_id = self.research_scout()
        pid_file = self.root / "helper-child.pid"
        self.stubs.answer(
            plan=plan(*QUESTIONS[:3]), report=fenced(REPORT),
            helpers={QUESTIONS[1]: {"helper_sleep": 60,
                                    "helper_pid_file": str(pid_file)}})
        real = lead.run_lead
        with patch.object(scout_process, "run_lead",
                          lambda conn, project, row, worktree, _budget_s, *rest:
                          real(conn, project, row, worktree, 5, *rest)):
            self.run_scout(path, scout_id)
        phases = [phase for _, _, phase, *_ in self.tasks(conn, scout_id)]
        self.assertEqual(phases, ["done", "stopped", "done"])
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while running(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(running(pid))
        self.assertEqual(len(self.stubs.turns("report")), 1)
        self.assertEqual(self.report(conn, scout_id)["cut"], [QUESTIONS[1]])
        self.assertEqual(scout(conn, scout_id).phase, "done")

    def test_the_budget_counts_from_the_plan_turn_not_the_lead_probe(self):
        path, conn, scout_id = self.research_scout()
        self.stubs.answer(plan=plan(QUESTIONS[0]), report=fenced(REPORT))
        real_probe, real_lead = lead.probe_scout_route, lead.run_lead

        def slow_probe(*args):
            time.sleep(5)
            return real_probe(*args)

        with patch.object(lead, "probe_scout_route", slow_probe), \
                patch.object(scout_process, "run_lead",
                             lambda conn, project, row, worktree, _budget_s, *rest:
                             real_lead(conn, project, row, worktree, 4, *rest)):
            self.run_scout(path, scout_id)
        self.assertEqual([phase for _, _, phase, *_ in self.tasks(conn, scout_id)],
                         ["done"])

    def test_a_scout_with_no_budget_left_after_its_plan_skips_the_helper_probe(self):
        path, conn, scout_id = self.research_scout()
        self.stubs.answer(plan=plan(*QUESTIONS[:2]), report=fenced(REPORT))
        with patch.object(lead, "monotonic", lambda: time.monotonic() - 3600):
            self.run_scout(path, scout_id)
        self.assertEqual([Path(call["argv"][0]).name
                          for call in self.stubs.turns("probe")], ["claude"])
        self.assertEqual(self.report(conn, scout_id)["cut"], QUESTIONS[:2])

    def test_a_helper_whose_store_fails_to_open_ends_the_scout_without_hanging(self):
        path, conn, scout_id = self.research_scout(swarm=2)
        self.stubs.answer(plan=plan(*QUESTIONS[:2]), report=fenced(REPORT))
        opened = []

        def open_once_failing(store_path):
            opened.append(store_path)
            if len(opened) == 2:
                time.sleep(1)
                raise OSError("store is locked")
            return store.open(store_path)

        finished = threading.Thread(
            target=self.cli, args=("--scout", str(scout_id), str(path)),
            daemon=True)
        with patch.object(swarm, "store",
                          type("Store", (), {"open": staticmethod(
                              open_once_failing)})):
            finished.start()
            finished.join(30)
        self.assertFalse(finished.is_alive())
        self.assertEqual(scout(conn, scout_id).phase, "failed")

    def test_a_second_wave_follows_a_synthesis_turn_resuming_the_lead(self):
        path, conn, scout_id = self.research_scout(waves=2)
        self.stubs.answer(plan=plan(QUESTIONS[0]), synthesis=plan(QUESTIONS[1]),
                          report=fenced(REPORT))
        self.run_scout(path, scout_id)
        (planned,), (synthesis,) = (self.stubs.turns("plan"),
                                    self.stubs.turns("synthesis"))
        session = planned["argv"][planned["argv"].index("--session-id") + 1]
        self.assertEqual(
            synthesis["argv"][synthesis["argv"].index("--resume") + 1], session)
        self.assertTrue(synthesis["argv"][-1].startswith("Scout synthesis turn"))
        self.assertEqual([(wave, question, phase) for wave, question, phase, *_
                          in self.tasks(conn, scout_id)],
                         [(1, QUESTIONS[0], "done"), (2, QUESTIONS[1], "done")])
        order = [(call["turn"], call.get("question"))
                 for call in self.stubs.calls()]
        self.assertGreater(order.index(("report", None)),
                           order.index(("helper", QUESTIONS[1])))

    def test_an_empty_synthesis_ends_the_waves_and_the_report_gets_every_answer(self):
        path, conn, scout_id = self.research_scout(waves=2)
        self.stubs.answer(
            plan=plan(*QUESTIONS[:2]), synthesis=fenced({"tasks": []}),
            report=fenced(REPORT),
            helpers={question: {"helper": f"answer {number}: notes.txt:1"}
                     for number, question in enumerate(QUESTIONS[:2])})
        self.run_scout(path, scout_id)
        self.assertEqual({wave for wave, *_ in self.tasks(conn, scout_id)}, {1})
        (report,) = self.stubs.turns("report")
        for number, question in enumerate(QUESTIONS[:2]):
            self.assertIn(f"{question}\nanswer {number}: notes.txt:1",
                          report["argv"][-1])

    def test_each_helper_reads_its_own_detached_worktree_removed_at_the_end(self):
        path, conn, scout_id = self.research_scout()
        self.stubs.answer(plan=plan(*QUESTIONS[:3]), report=fenced(REPORT),
                          helper_git=[["rev-parse", "HEAD"],
                                      ["symbolic-ref", "-q", "HEAD"]])
        self.run_scout(path, scout_id)
        helpers = self.stubs.helpers()
        base = scout(conn, scout_id).base_sha
        self.assertEqual(len({helper["cwd"] for helper in helpers}), 3)
        for helper in helpers:
            (head_status, head), (symbolic_status, _) = helper["git"]
            self.assertEqual((head_status, head), (0, base))
            self.assertNotEqual(symbolic_status, 0)
            self.assertFalse(Path(helper["cwd"]).exists())
            self.assertNotIn(helper["cwd"], git(path, "worktree", "list"))

    def test_a_push_from_a_helper_is_refused(self):
        path, conn, scout_id = self.research_scout()
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", str(origin))
        git(path, "remote", "add", "origin", str(origin))
        git(path, "push", "-q", "origin", "main")
        self.stubs.answer(plan=plan(QUESTIONS[0]), report=fenced(REPORT),
                          helper_git=[["push", "origin",
                                       "HEAD:refs/heads/helper-leak"]])
        self.run_scout(path, scout_id)
        ((status, _),) = self.stubs.helpers()[0]["git"]
        self.assertNotEqual(status, 0)
        self.assertNotIn("helper-leak", git(path, "ls-remote", "origin"))

    def test_a_codex_helper_turn_records_its_tokens_route_and_turn(self):
        path, conn, scout_id = self.research_scout()
        self.stubs.answer(plan=plan(QUESTIONS[0]), report=fenced(REPORT),
                          helper_tokens="41,000")
        self.run_scout(path, scout_id)
        ((_, _, phase, route, tokens, duration),) = self.tasks(conn, scout_id)
        self.assertEqual((phase, route, tokens), ("done", "luna", 41000))
        self.assertIsNotNone(duration)
        self.assertEqual(Path(self.stubs.turns("helper")[0]["argv"][0]).name,
                         "codex")
        roles = [json.loads(payload)["role"] for (payload,) in conn.execute(
            "SELECT payload FROM runEvents WHERE scoutId = ?"
            " AND kind = 'agent_turn'", (scout_id,))]
        self.assertIn("scout-helper", roles)

    def test_a_helper_mentioning_tokens_used_with_no_count_ends_done(self):
        path, conn, scout_id = self.research_scout()
        self.stubs.answer(plan=plan(QUESTIONS[0]), report=fenced(REPORT),
                          helper="I counted the tokens used, in total.")
        self.run_scout(path, scout_id)
        ((_, _, phase, _, tokens, _),) = self.tasks(conn, scout_id)
        self.assertEqual((phase, tokens), ("done", None))

    def test_a_claude_helper_excerpt_holds_its_result_not_its_json(self):
        path, conn, scout_id = self.research_scout(helper_route="haiku")
        answer = "The retry loop is at notes.txt:1."
        self.stubs.answer(plan=plan(QUESTIONS[0]), report=fenced(REPORT),
                          claude_helper=json.dumps(
                              {"type": "result", "result": answer,
                               "usage": {"input_tokens": 7,
                                         "output_tokens": 5}}))
        self.run_scout(path, scout_id)
        ((excerpt, tokens),) = conn.execute(
            "SELECT excerpt, tokens FROM scoutTasks WHERE scoutId = ?",
            (scout_id,)).fetchall()
        self.assertEqual((excerpt.strip(), tokens), (answer, 12))

    def test_a_helper_route_that_fails_its_probe_runs_no_helper(self):
        path, conn, scout_id = self.research_scout()
        self.stubs.answer(plan=plan(*QUESTIONS[:2]), report=fenced(REPORT),
                          codex_probe="not today")
        self.run_scout(path, scout_id)
        self.assertEqual(self.stubs.turns("helper"), [])
        self.assertEqual([phase for _, _, phase, *_ in self.tasks(conn, scout_id)],
                         ["queued", "queued"])
        (summary,) = [summary for (summary,) in conn.execute(
            "SELECT summary FROM runEvents WHERE scoutId = ?"
            " AND kind = 'scout_helpers'", (scout_id,))]
        self.assertIn("helper route luna is down", summary)
        self.assertEqual(self.report(conn, scout_id),
                         dict(REPORT, cut=QUESTIONS[:2]))

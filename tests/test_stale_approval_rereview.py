"""A review round or covering review whose only open findings say a cited
approval went stale is reviewed again at the same head, not sent to a fix."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.config.project
import holophyte.loop.implement
import holophyte.loop.review_round
import holophyte.pr.github
import holophyte.pr.pullrequest
import store
import store.tickets
from holophyte.babysit import babysitter
from holophyte.loop.gates import RunFailure
from holophyte.loop.runs import open_store, set_phase
from holophyte.loop.stop import continuation, resume_paused

CRITERIA = ["Given a missing file, then load() returns an empty thing",
            "Given a broken file, then load() names the line"]
TICKET = "Fix load() in `holophyte/load.py`.\n"


class Parked(Exception):
    pass


class StaleApprovalRereviewTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name, "repo")
        self.root.mkdir()
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(Path(tmp.name, "home"))})
        home.start()
        self.addCleanup(home.stop)
        for args in (("init", "-q", "-b", "main"),
                     ("config", "user.name", "Test implementer"),
                     ("config", "user.email", "implementer@example.test"),
                     ("commit", "--allow-empty", "-qm", "base"),
                     ("checkout", "-qb", "task")):
            self.git(*args)
        self.base = self.git("rev-parse", "HEAD")
        self.approved = self.commit({
            "holophyte/load.py": "def load():\n    return {}\n",
            "tests/test_load.py": "def test_missing():\n    pass\n\n\n"
                                  "def test_broken():\n    pass\n"})
        self.head = self.commit({
            "holophyte/load.py": "def load():\n    return {} or {}\n",
            "tests/test_load.py": "def test_missing():\n    assert True\n\n\n"
                                  "def test_broken():\n    pass\n"})
        self.project = holophyte.config.project.Project.locate(self.root)
        self.project.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.project.config_path.write_text('[merge]\nmode = "pr"\napprove = "auto"\n')
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project_id = store.tickets.ensure_project(self.conn, "team-1", self.root)
        ticket = store.tickets.mirror_ticket(
            self.conn, project_id, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1",
            acceptance_criteria=CRITERIA, verification_commands=["true"],
            time_box_ms=60 * 60 * 1000)
        store.tickets.transition(self.conn, ticket, "in_flight")
        self.run_id = store.claim(self.conn, project_id, ticket)
        self.project_id, self.ticket_id = project_id, ticket
        set_phase(self.conn, self.run_id, "merge_gate")
        self.prompts, self.fix_turns = [], []

    def git(self, *args):
        return subprocess.check_output(
            ["git", *args], cwd=self.root, text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, files):
        for path, text in files.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", "commit")
        return self.git("rev-parse", "HEAD")

    def stale_reply(self, second="met — tests/test_load.py::test_broken"):
        return (f"CRITERION 1: met — approval at {self.approved[:12]};"
                " tests/test_load.py::test_missing\n"
                f"CRITERION 2: {second}\nVERDICT: APPROVE")

    def direct_reply(self):
        return ("CRITERION 1: met — tests/test_load.py::test_missing\n"
                "CRITERION 2: met — tests/test_load.py::test_broken\n"
                "VERDICT: APPROVE")

    def agents(self, replies):
        replies = list(replies)

        def reviewer(target, role, goal, *args, **kwargs):
            self.prompts.append(goal)
            reply = replies.pop(0)
            return reply() if callable(reply) else reply

        def implementer(target, conn, run_id, beat_s, wt, budget_min, goal,
                        **kwargs):
            self.fix_turns.append(goal)
            return "No change: the criterion needs a fresh review.", False

        return (patch.object(holophyte.loop.review_round, "agent", reviewer),
                patch.object(holophyte.loop.review_round, "_timed", implementer),
                patch.object(holophyte.loop.implement, "_transport_timed",
                             implementer))

    def review_rounds(self, *replies, cap=2, resume=None):
        reviewer, fixer, babysit_fixer = self.agents(replies)
        with reviewer, fixer, babysit_fixer:
            return holophyte.loop.review_round._review_rounds(
                self.project, self.conn, self.run_id, None, "KO-1", "task",
                self.root, 30, self.base, self.head, TICKET, "true", (),
                CRITERIA, 10, cap, resume=resume)

    def pausing(self, reply):
        def pause_then_reply():
            store.pause(self.conn, self.run_id, "review checkpoint")
            return reply
        return pause_then_reply

    def review_fix(self, *replies, fix_note="repair the pin"):
        reviewer, fixer, babysit_fixer = self.agents(replies)
        with (reviewer, fixer, babysit_fixer,
              patch.object(holophyte.pr.pullrequest, "refresh_pr_text"),
              patch.object(holophyte.pr.github, "rest",
                           return_value={"body": "Fixed the load."}),
              patch.object(holophyte.pr.pullrequest, "_park_on_pr",
                           side_effect=Parked)):
            return babysitter._review_fix(
                self.project, self.conn, self.run_id, None, "KO-1", "task",
                self.root, self.head, self.approved, 30,
                holophyte.pr.github.PullRequest(
                    "example.test", "o", "n", 1, "https://example.test/pull/1"),
                TICKET,
                "true", (), CRITERIA, fix_note=fix_note, budget_min=10)

    def rereview_events(self):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE runId = ?"
            " AND kind = 'stale_approval_rereview'", (self.run_id,))]

    def round_verdicts(self):
        return [verdict for (verdict,) in self.conn.execute(
            "SELECT verdict FROM reviewRounds WHERE runId = ?"
            " ORDER BY round", (self.run_id,))]

    def test_review_round_of_only_stale_approvals_runs_again_not_a_fix(self):
        result = self.review_rounds(self.stale_reply(), self.direct_reply())

        self.assertEqual(result, (self.head, 2, True))
        self.assertEqual(self.fix_turns, [])
        (event,) = self.rereview_events()
        self.assertEqual(event["sha"], self.head)
        self.assertNotIn("changed since approval at", self.prompts[0])
        self.assertIn(f"tests/test_load.py (changed since approval at "
                      f"{self.approved})", self.prompts[1])
        self.assertEqual(self.round_verdicts(), ["changes_requested", "pass"])

    def test_second_stale_only_review_round_fails_naming_it(self):
        with self.assertRaises(RunFailure) as failed:
            self.review_rounds(self.stale_reply(), self.stale_reply(), cap=3)

        self.assertEqual(self.fix_turns, [])
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(len(self.rereview_events()), 1)
        reason = str(failed.exception)
        self.assertIn(f"changed since approval at {self.approved}", reason)
        self.assertNotIn("(none recorded)", reason)

    def test_pause_after_a_stale_only_round_keeps_the_guard_on_resume(self):
        with self.assertRaises(store.RunEnded):
            self.review_rounds(self.pausing(self.stale_reply()), cap=3)
        resume_paused(self.project, self.conn, self.ticket_id, "go on")
        store.tickets.transition(self.conn, self.ticket_id, "in_flight")
        self.run_id = store.claim(self.conn, self.project_id, self.ticket_id)
        set_phase(self.conn, self.run_id, "merge_gate")

        with self.assertRaises(RunFailure) as failed:
            self.review_rounds(self.stale_reply(), self.direct_reply(), cap=3,
                               resume=continuation(self.conn, self.run_id))

        self.assertEqual(self.fix_turns, [])
        self.assertEqual(len(self.prompts), 2)
        self.assertIn(f"changed since approval at {self.approved}",
                      str(failed.exception))

    def test_review_round_with_a_stale_and_an_ordinary_finding_gets_a_fix(self):
        with self.assertRaises(RunFailure) as failed:
            self.review_rounds(self.stale_reply("unwitnessed — no test"))

        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.fix_turns), 1)
        self.assertEqual(self.rereview_events(), [])
        self.assertIn("fix round made no progress", str(failed.exception))

    def test_covering_review_of_only_stale_approvals_runs_again_not_a_fix(self):
        approved = self.review_fix(self.stale_reply(), self.direct_reply())

        self.assertEqual(approved, self.head)
        self.assertEqual(self.fix_turns, [])
        (event,) = self.rereview_events()
        self.assertEqual(event["sha"], self.head)
        self.assertEqual(len(self.prompts), 2)
        self.assertIn(f"tests/test_load.py (changed since approval at "
                      f"{self.approved})", self.prompts[1])
        self.assertNotIn("changed since approval at", self.prompts[0])
        self.assertEqual(self.round_verdicts(), ["changes_requested", "pass"])

    def test_second_stale_only_covering_review_fails_without_a_note(self):
        with self.assertRaises(RunFailure) as failed:
            self.review_fix(self.stale_reply(), self.stale_reply(),
                            fix_note=None)

        self.assertEqual(self.fix_turns, [])
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(len(self.rereview_events()), 1)
        reason = str(failed.exception)
        self.assertIn(f"changed since approval at {self.approved}", reason)
        self.assertNotIn("(none recorded)", reason)

    def test_stale_finding_beside_an_ordinary_one_gets_a_fix_turn(self):
        with self.assertRaises(RunFailure) as failed:
            self.review_fix(self.stale_reply("unwitnessed — no test"))

        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(len(self.fix_turns), 1)
        self.assertIn("changed since approval at", self.fix_turns[0])
        self.assertEqual(self.rereview_events(), [])
        self.assertIn("fix round made no progress", str(failed.exception))


if __name__ == "__main__":
    unittest.main()

"""After each witness pass a story closes under the merge lock when every
witness is green at main's tip with its approved file, or parks on one
typed decision per witness.

Run: python3 -m unittest discover -s tests -p 'test_story_close.py' -v
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import APPROVE, Commit, FakeAgent, no_agent_processes  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402
from test_store_claim_loop import STORE_MODE  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.gates  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import holophyte.story.story_close  # noqa: E402
import linear_provider  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.follow_ups  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board.projection import release_run  # noqa: E402
from holophyte.config.config_tables import sweep_config  # noqa: E402
from holophyte.host.supervisor import (  # noqa: E402
    fresh_memory,
    reconcile_parked_pull_requests,
)
from holophyte.loop.follow_ups import (  # noqa: E402
    Origin,
    draft_body,
    draft_title,
    fingerprint,
)
from holophyte.loop.gates import merge_lock  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from holophyte.story.witness import (  # noqa: E402
    run_witnesses,
    witness_pass,
    witness_step,
)
from provider import board_for  # noqa: E402
from store.stories import approve_story, file_story, story  # noqa: E402
from store.story_proposals import record_proposal  # noqa: E402
from tests.test_native_loop import NATIVE, no_linear  # noqa: E402
from tests.test_witness_runner import (  # noqa: E402
    FAILS_AN_ASSERTION,
    PASSES,
    PYTHON,
)

W1_FILE, W2_FILE = "tests/test_w1.py", "tests/test_w2.py"
PROPOSED = "Export the refund column"
PROPOSALS = "SELECT state FROM storyProposals ORDER BY id"
UNMET = ("file a follow-up child", "accept the changed witness file",
         "amend the witness (re-plan)", "abandon the story")


def witness(key, file):
    module = file.removesuffix(".py").replace("/", ".")
    return {"key": key, "criterion": f"{key} holds", "file": file,
            "command": f"{PYTHON} -m unittest {module}", "source": PASSES}


class StoryCloseFixture(LoopFixture):
    config = NATIVE

    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.configure(self.config)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)

    def tickets(self):
        return [self.ticket_id(store.board.file_ticket(
            self.conn, self.project_id, "NAT", VALID_BODY, column=column))
            for column in ("backlog", "ready")]

    def approve(self, parent, child, witnesses):
        file_story(self.conn, parent, witnesses,
                   [(child, "completes", [w["key"] for w in witnesses])])
        (revision,) = self.conn.execute(
            "SELECT revision FROM tickets WHERE id = ?", (parent,)).fetchone()
        approve_story(self.conn, parent, revision, "operator", "go")

    def ticket_id(self, identifier):
        return self.conn.execute(
            "SELECT id FROM tickets WHERE linearIdentifier = ?",
            (identifier,)).fetchone()[0]

    def commit(self, path, text, message):
        (self.target / path).write_text(text)
        self.git("add", path)
        self.git("commit", "-q", "-m", message)
        return self.tip()

    def tip(self):
        return self.git("rev-parse", "main").strip()

    def step(self):
        with patch.object(sys, "stdout", io.StringIO()):
            witness_step(self.project, self.conn, self.project_id)

    def operator_pass(self, parent):
        with patch.object(sys, "stdout", io.StringIO()):
            witness_pass(self.project, self.conn, parent, "operator")

    def propose(self, raiser, text=PROPOSED):
        """The proposal id of `text`, a follow-up of `raiser`'s merged run."""
        run_id = store.claim(self.conn, self.project_id, raiser)
        for phase in ("working", "verifying", "reviewing", "merge_gate",
                      "merging"):
            store.set_phase(self.conn, run_id, phase)
        release_run(self.conn, run_id, True, merge_sha=self.tip())
        store.tickets.walk_ticket(self.conn, raiser, "merged")
        row_id = store.follow_ups.record_follow_up(
            self.conn, run_id, self.tip(), "feature", True, text,
            fingerprint(text, None))
        (row,) = store.follow_ups.pending_follow_ups(self.conn, run_id)
        (key,) = self.read(
            f"SELECT linearIdentifier FROM tickets WHERE id = {raiser}")[0]
        return record_proposal(
            self.conn, story(self.conn, raiser).ticketId, row_id, raiser,
            draft_title(text), draft_body(row, Origin(key, None, self.tip()),
                                          depends_on=key))

    def decisions(self, parent):
        return [(d.kind, d.question.split(" ")[1], d.options, d.defaultOption)
                for d in story(self.conn, parent).decisions]


class StoryCloseTests(StoryCloseFixture):
    def test_the_loop_closes_the_story_on_its_childs_merge(self):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        fake = FakeAgent(Commit("lands w1", path=W1_FILE, body=PASSES),
                         APPROVE)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake), \
                patch("holophyte.review.freshness.critic_admits",
                      return_value=True):
            holophyte.cli.operator.main(self.project, self.board)

        tip = self.tip()
        self.assertEqual(self.read(
            "SELECT mergeSha FROM runs WHERE outcome = 'merged'"), [(tip,)])
        found = story(self.conn, parent)
        self.assertEqual((found.state, found.closedSha), ("closed", tip),
                         out.getvalue())
        self.assertEqual(self.read(
            f"SELECT status FROM tickets WHERE id = {parent}"), [("merged",)])
        notes = [text for (text,) in self.read(
            f"SELECT text FROM ticketNotes WHERE ticketId = {parent}")
            if "W1 green" in text]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn(tip, notes[0])

    def test_a_commit_landing_before_the_lock_defers_the_close(self):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        green_at = self.commit(W1_FILE, PASSES, "w1 lands")
        real_lock = holophyte.story.story_close.merge_lock
        landed = []

        @contextlib.contextmanager
        def lock_after_a_merge(*args, **kwargs):
            landed.append(self.commit("README.md", "more\n", "docs"))
            with real_lock(*args, **kwargs) as path:
                yield path

        with patch.object(holophyte.story.story_close, "merge_lock",
                          lock_after_a_merge):
            self.step()

        self.assertEqual(self.read(
            "SELECT mainSha, verdict FROM witnessResults"),
            [(green_at, "green")])
        self.assertEqual(story(self.conn, parent).state, "approved")

        self.step()

        found = story(self.conn, parent)
        self.assertEqual((found.state, found.closedSha), ("closed", landed[0]))

    def test_a_lock_held_through_the_pass_closes_on_a_later_step(self):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        tip = self.commit(W1_FILE, PASSES, "w1 lands")

        with merge_lock(self.project, None), \
                patch.object(holophyte.loop.gates, "MERGE_LOCK_WAIT_SEC", 0):
            self.step()
        self.assertEqual(story(self.conn, parent).state, "approved")

        self.step()

        found = story(self.conn, parent)
        self.assertEqual((found.state, found.closedSha), ("closed", tip))
        self.assertEqual(self.read(
            "SELECT mainSha, verdict FROM witnessResults"), [(tip, "green")])

    def test_a_baseline_green_at_the_tip_is_no_pass_and_closes_nothing(self):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        run_witnesses(self.project, self.conn, parent, self.tip(), "baseline",
                      copy_files=True)

        self.step()

        self.assertEqual(story(self.conn, parent).state, "approved")
        self.assertEqual(self.read(
            "SELECT verdict, verifier FROM witnessResults ORDER BY id"),
            [("green", "baseline"), ("absent", "loop")])

    def test_a_red_witness_with_every_child_merged_parks_unmet_once(self):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        store.tickets.walk_ticket(self.conn, child, "merged")
        self.commit(W1_FILE, FAILS_AN_ASSERTION, "w1 lands red")

        self.step()
        self.operator_pass(parent)

        self.assertEqual(story(self.conn, parent).state, "parked")
        self.assertEqual(self.decisions(parent),
                         [("unmet", "W1", UNMET, "file a follow-up child")])

    def test_a_green_witness_whose_file_changed_parks_unmet_once(self):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        store.tickets.walk_ticket(self.conn, child, "merged")
        self.commit(W1_FILE, PASSES + "# edited\n", "w1 lands edited")

        self.step()
        self.operator_pass(parent)

        self.assertEqual(self.read("SELECT verdict FROM witnessResults"),
                         [("green",), ("green",)])
        self.assertEqual(story(self.conn, parent).state, "parked")
        self.assertEqual(self.decisions(parent),
                         [("unmet", "W1", UNMET, "file a follow-up child")])

    def test_a_green_turned_red_parks_regressed_with_a_child_open(self):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE),
                                     witness("W2", W2_FILE)])
        self.commit(W1_FILE, PASSES, "w1 lands")
        self.step()
        red_at = self.commit(W1_FILE, FAILS_AN_ASSERTION, "w1 breaks")

        self.step()
        self.operator_pass(parent)

        self.assertEqual(self.read(
            f"SELECT verdict FROM witnessResults WHERE mainSha = '{red_at}'"
            " AND witnessKey = 'W1'"), [("red",)] * 4)
        self.assertEqual(story(self.conn, parent).state, "parked")
        self.assertEqual(
            [(kind, key, options[0], default)
             for kind, key, options, default in self.decisions(parent)],
            [("regressed", "W1", "file a fix child", "file a fix child")])

    def test_a_red_whose_rerun_the_budget_skipped_waits_for_the_next_pass(self):
        self.configure(NATIVE + "[story]\nwitness_sec = 2\n")
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE),
                                     dict(witness("W2", W2_FILE),
                                          command="sleep 5")])
        self.commit(W1_FILE, PASSES, "w1 lands")
        self.step()
        self.commit(W2_FILE, PASSES, "w2 lands")
        red_at = self.commit(W1_FILE, FAILS_AN_ASSERTION, "w1 breaks")
        runs_of_w1 = ("SELECT verdict FROM witnessResults"
                      f" WHERE mainSha = '{red_at}' AND witnessKey = 'W1'")

        self.step()

        self.assertEqual(self.read(runs_of_w1), [("red",)])
        self.assertEqual(
            (story(self.conn, parent).state, self.decisions(parent)),
            ("approved", []))

        self.step()

        self.assertEqual(self.read(runs_of_w1), [("red",)] * 2)
        self.assertEqual([(kind, key) for kind, key, *_ in
                          self.decisions(parent)], [("regressed", "W1")])


class OpenProposalCloseTests(StoryCloseFixture):
    def test_closing_names_and_supersedes_an_open_proposal(self):
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        self.propose(child)
        tip = self.commit(W1_FILE, PASSES, "w1 lands")

        self.step()

        self.assertEqual(story(self.conn, parent).state, "closed")
        (note,) = [text for (text,) in self.read(
            f"SELECT text FROM ticketNotes WHERE ticketId = {parent}")
            if tip in text]
        self.assertIn(f"Open proposals: p1 from NAT-2: {PROPOSED}", note)
        self.assertEqual(self.read(PROPOSALS), [("superseded",)])


class LinearStub:
    """The Linear module a store-mode `LinearBoard` calls: it answers each
    issue's state and records each state it is sent."""

    def __init__(self):
        self.names = {"KO-1": "Todo", "KO-2": "Todo"}
        self.sent = []

    def states(self, identifiers, label=None):
        return {identifier: {"state": "completed", "name": "Done",
                             "column": None}
                if self.names[identifier] == "Done" else
                {"state": "open", "name": self.names[identifier],
                 "column": "ready"} for identifier in identifiers}

    def set_state(self, issue_id, state, team):
        self.sent.append((issue_id, state))
        self.names[issue_id.replace("issue-", "KO-")] = state

    def comment(self, task_id, body):
        pass

    def closed_identifiers(self, identifiers):
        return {}


class StoreModeCloseTests(StoryCloseFixture):
    config = STORE_MODE

    def setUp(self):
        super().setUp()
        self.linear = LinearStub()
        patcher = patch.dict(sys.modules, {"linear_provider": self.linear})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.board = board_for(self.project)

    def tickets(self):
        return [store.tickets.mirror_ticket(
            self.conn, self.project_id, f"issue-{n}", f"KO-{n}", f"KO-{n}",
            board_state="Todo", board_column="ready") for n in (1, 2)]

    def sweep(self, now):
        with patch("holophyte.host.reconcile._reconcile_pull_requests"), \
                patch("holophyte.host.supervisor.linear_budget_low",
                      return_value=False), \
                patch("holophyte.host.supervisor.start_loop_for"):
            reconcile_parked_pull_requests(
                self.project, self.conn, now, self.board, io.StringIO(),
                knobs=self.knobs, memory=self.memory)

    def test_the_closed_parents_done_is_queued_and_sent_by_the_sweep(self):
        self.knobs, self.memory = sweep_config(self.project), fresh_memory()
        parent, child = self.tickets()
        self.approve(parent, child, [witness("W1", W1_FILE)])
        self.commit(W1_FILE, PASSES, "w1 lands")

        self.step()

        self.assertEqual(story(self.conn, parent).state, "closed")
        self.assertEqual(self.read(
            f"SELECT pushState FROM tickets WHERE id = {parent}"), [("Done",)])
        now = int(time.time() * 1000)
        self.sweep(now)
        self.sweep(now + self.knobs.board_ask_ms)
        self.assertEqual(self.linear.sent, [("issue-1", "Done")])

if __name__ == "__main__":
    import unittest
    unittest.main()

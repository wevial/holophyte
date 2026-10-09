"""A fix commit's FOLLOW_UP lines: captured pending, settled when the run merges.

Run: python3 -m unittest discover -s tests -p 'test_follow_ups.py' -v
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_agent import (  # noqa: E402
    APPROVE,
    REQUEST_CHANGES,
    Commit,
    FakeAgent,
    Idle,
    no_agent_processes,
)
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402

import holophyte.cli.operator  # noqa: E402
import holophyte.loop.adjudicate  # noqa: E402
import holophyte.loop.implement  # noqa: E402
import holophyte.loop.review_round  # noqa: E402
import store  # noqa: E402
import store.board  # noqa: E402
import store.tickets  # noqa: E402
import ticket_template  # noqa: E402
from holophyte.agents.fix_session import fix_turn  # noqa: E402
from holophyte.board.projection import release_run  # noqa: E402
from holophyte.loop.follow_ups import capture, settle  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from provider import LinearBoard, board_for  # noqa: E402

NATIVE = '[board]\nkind = "native"\nkey = "NAT"\n'
FEATURE = "FOLLOW_UP(feature): Cache the board read @ provider.py:185"
GUARDRAIL = "- FOLLOW_UP(guardrail): Lint ticket checks for slash patterns"
LEGACY = "FOLLOW_UP: legacy wording"
FIX = f"address the review\n\nADDRESS: fixed the blocker\n{FEATURE}\n{GUARDRAIL}\n"
URL = "https://github.com/example/repo/pull/12"


def events(conn, run_id, kind):
    return [json.loads(payload) for (payload,) in conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
        " ORDER BY seq", (run_id, kind))]


class CaptureTests(LoopFixture):
    """`fix_turn()` over a real repository and store, its turn committing."""

    def setUp(self):
        super().setUp()
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        project = store.tickets.ensure_project(self.conn, "team", str(self.target))
        ticket = store.tickets.mirror_ticket(
            self.conn, project, linear_issue_id="issue-1",
            linear_identifier="KO-1", title="ticket 1")
        self.run_id = store.claim(self.conn, project, ticket)

    def fix(self, *messages):
        sha = self.git("rev-parse", "HEAD").strip()

        def timed(*args, argv=None):
            for turn, message in enumerate(messages):
                Commit(message).play(self.target, f"{sha[:8]}-{turn}")
            return "done", False

        fix_turn(self.project, self.conn, self.run_id, 0, self.target, 10,
                 "TICKET", "VERDICT: REQUEST_CHANGES", sha, timed=timed,
                 check_cap=None)

    def rows(self):
        return self.conn.execute(
            "SELECT id, kind, kindGiven, text, path, line, settledAt"
            " FROM followUps WHERE runId = ? ORDER BY id",
            (self.run_id,)).fetchall()

    def test_each_follow_up_form_is_stored_pending_with_its_kind(self):
        self.fix(f"{FIX}{LEGACY}\n")

        rows = self.rows()
        self.assertEqual([row[1:] for row in rows], [
            ("feature", 1, "Cache the board read", "provider.py", 185, None),
            ("guardrail", 1, "Lint ticket checks for slash patterns", None,
             None, None),
            ("feature", 0, "legacy wording", None, None, None)])
        missing = events(self.conn, self.run_id, "follow_up_kind_missing")
        self.assertEqual([event["id"] for event in missing], [rows[2][0]])

    def test_other_verdicts_and_mid_sentence_mentions_store_nothing(self):
        line = "FOLLOW_UP(feature): Retry the label write @ provider.py:90"
        self.fix("fix one\n\nADDRESS: fixed\nDECLINE: out of scope\n"
                 "The reviewer's FOLLOW_UP about logging is declined.\n"
                 f"{line}\n", f"fix two\n\n{line}\n")
        self.fix(f"fix three\n\n- {line}\n")

        self.assertEqual([row[1:6] for row in self.rows()], [
            ("feature", 1, "Retry the label write", "provider.py", 90)])


class Recording(LinearBoard):
    """A Linear board that records each filing instead of calling Linear."""

    def __init__(self, key="KO-900", refusal=None):
        super().__init__("project-under-test", "team-under-test")
        self.key, self.refusal, self.filed = key, refusal, []

    def closed_identifiers(self, identifiers):
        return {}

    def file(self, title, body, estimate, state, priority=None, blockers=(),
             parent=None):
        self.filed.append((title, body, estimate, state))
        if self.refusal is not None:
            raise self.refusal
        return self.key


class LoopRuns(LoopFixture):
    def run_loop(self, *script, board=None):
        fake = FakeAgent(*script)
        out = io.StringIO()
        with no_agent_processes(), patch.object(sys, "stdout", out), \
                patch.object(holophyte.loop.implement, "agent", fake), \
                patch.object(holophyte.loop.review_round, "agent", fake), \
                patch.object(holophyte.loop.adjudicate, "agent", fake), \
                patch("holophyte.review.freshness.critic_admits",
                      return_value=True):
            self.rc = holophyte.cli.operator.main(self.project, board)
        return out.getvalue()

    def store_events(self, kind):
        with closing(open_store(self.project)) as conn:
            return events(conn, 1, kind)


class NativeProject(LoopRuns):
    def setUp(self):
        super().setUp()
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        environ = patch.dict(os.environ, env, clear=True)
        environ.start()
        self.addCleanup(environ.stop)
        self.configure(NATIVE)
        self.board = board_for(self.project)
        self.conn = open_store(self.project)
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, self.board.team, self.project.path)


class NativeMergeTests(NativeProject):
    def test_a_merged_run_files_its_feature_draft_and_keeps_its_guardrail(self):
        store.board.file_ticket(self.conn, self.project_id, "NAT", VALID_BODY)

        out = self.run_loop(Commit("the work"), REQUEST_CHANGES, Commit(FIX),
                            APPROVE, board=self.board)

        [(merge_sha, outcome)] = self.read("SELECT mergeSha, outcome FROM runs")
        self.assertEqual(outcome, "merged", out)
        [(draft_id, key, title, body, column)] = self.read(
            "SELECT id, linearIdentifier, title, body, boardColumn FROM tickets"
            " WHERE linearIdentifier != 'NAT-1'")
        self.assertEqual(column, "backlog")
        self.assertFalse(store.tickets.pickable(self.conn, draft_id))
        self.assertTrue(title.startswith("Draft follow-up: "), title)
        for fact in ("Cache the board read", "provider.py:185", merge_sha,
                     "NAT-1"):
            self.assertIn(fact, body)
        self.assertEqual(self.read(
            "SELECT kind, filedAs, settledAt IS NOT NULL FROM followUps"
            " ORDER BY id"), [("feature", key, 1), ("guardrail", None, 1)])
        self.assertEqual([e["key"] for e in self.store_events("follow_up_filed")],
                         [key])
        self.assertEqual(len(self.store_events("follow_up_ledger")), 1)


class NativeSettleTests(NativeProject):
    """Settled over the store with real git commits; the merge is recorded."""

    def merged_run(self, *messages, pr_url=None):
        key = store.board.file_ticket(self.conn, self.project_id, "NAT",
                                      VALID_BODY)
        ticket = store.read.ticket_by_identifier(self.conn, key)
        run_id = store.claim(self.conn, self.project_id, ticket.id)
        base = self.git("rev-parse", "HEAD").strip()
        for message in messages:
            self.git("commit", "-q", "--allow-empty", "-m", message)
        capture(self.conn, run_id, self.target, base)
        for phase in ("working", "verifying", "reviewing", "merge_gate",
                      "merging"):
            store.set_phase(self.conn, run_id, phase)
        if pr_url:
            store.set_pull_request(self.conn, run_id, pr_url)
        release_run(self.conn, run_id, True,
                    merge_sha=self.git("rev-parse", "HEAD").strip())
        settle(self.project, self.conn, run_id)
        return run_id

    def drafts(self):
        return self.read("SELECT linearIdentifier, body FROM tickets"
                         " WHERE title LIKE 'Draft follow-up: %' ORDER BY id")

    def test_a_draft_names_the_pull_request_and_is_refused_ready(self):
        self.merged_run(f"fix\n\n{FEATURE}", pr_url=URL)

        [(_, body)] = self.drafts()
        self.assertIn(URL, body)
        parsed = ticket_template.parse(body)
        self.assertEqual(parsed.title, "Draft follow-up: Cache the board read")
        self.assertTrue(parsed.summary.startswith(
            "DRAFT: the operator completes this before it leaves Backlog."))
        self.assertEqual(parsed.estimate_min, 30)
        with self.assertRaises(store.board.FilingRefused):
            store.board.file_ticket(self.conn, self.project_id, "NAT", body,
                                    column="ready")

    def test_an_open_draft_absorbs_a_repeat_until_it_is_canceled(self):
        self.merged_run(f"fix\n\n{FEATURE}")
        [(first, _)] = self.drafts()

        repeat = self.merged_run(
            "fix\n\nFOLLOW_UP(feature):   cache  the `Board` READ."
            " @ provider.py:240")

        self.assertEqual([key for key, _ in self.drafts()], [first])
        self.assertEqual(self.read(
            f"SELECT filedAs, duplicateOf FROM followUps WHERE runId = {repeat}"),
            [(None, first)])
        self.assertEqual([e["key"] for e in events(
            self.conn, repeat, "follow_up_duplicate")], [first])

        (revision,) = self.read(
            f"SELECT revision FROM tickets WHERE linearIdentifier = '{first}'")[0]
        store.board.cancel_ticket(self.conn, self.project_id, first, revision,
                                  "not wanted")
        self.merged_run(f"fix\n\n{FEATURE}")

        self.assertEqual(len(self.drafts()), 2)


class LinearMergeTests(LoopRuns):
    """The default Linear-mode project, its board a recording stand-in."""

    def merge(self, board, *script):
        with patch("provider.board_for", return_value=board):
            return self.main_output(*script)

    def test_a_feature_is_filed_once_into_backlog_with_estimate_30(self):
        board = Recording(key="KO-901")

        out = self.merge(board, Commit("the work"), REQUEST_CHANGES,
                         Commit(FIX), APPROVE)

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)],
                         out)
        self.assertEqual([(estimate, state) for _, _, estimate, state
                          in board.filed], [(30, "Backlog")])
        self.assertEqual(self.read(
            "SELECT filedAs FROM followUps WHERE kind = 'feature'"),
            [("KO-901",)])

    def test_a_refused_filing_is_recorded_and_the_run_stays_merged(self):
        board = Recording(refusal=RuntimeError("no Backlog state"))

        out = self.merge(board, Commit("the work"), REQUEST_CHANGES,
                         Commit(FIX), APPROVE)

        [(outcome, merge_sha)] = self.read("SELECT outcome, mergeSha FROM runs")
        self.assertEqual(outcome, "merged", out)
        self.assertEqual(merge_sha, self.git("rev-parse", "main").strip())
        [(error, settled)] = self.read(
            "SELECT error, settledAt IS NOT NULL FROM followUps"
            " WHERE kind = 'feature'")
        self.assertIn("no Backlog state", error)
        self.assertEqual(settled, 1)
        self.assertEqual(len(self.store_events("follow_up_unfiled")), 1)

    def test_a_run_failing_after_its_fix_turn_files_nothing(self):
        board = Recording()

        out = self.merge(board, Commit("the work"), REQUEST_CHANGES,
                         Commit(FIX), REQUEST_CHANGES, Idle())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)],
                         out)
        self.assertEqual(board.filed, [])
        self.assertEqual(self.read(
            "SELECT kind, settledAt FROM followUps ORDER BY id"),
            [("feature", None), ("guardrail", None)])


if __name__ == "__main__":
    unittest.main()

"""KO-748: a store-mode claim's read-back reads the issue's open blockers.

The claim reads its candidate back from the board, one issue, before the
lease. That read now answers `blocked_by` -- on Linear the board ids of
the blockers still open, read from the issue's inverse relations -- and
the claim mirrors it as `dependsOn`: a ticket that gained a blocker since
the pass's sync walks to `blocked_on_deps` and is not claimed.

Run: python3 -m unittest discover -s tests -p 'test_claim_blockers.py' -v
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from fake_agent import APPROVE, Commit  # noqa: E402
from loop_fixture import VALID_BODY, LoopFixture  # noqa: E402
from test_provider import FakeLinear  # noqa: E402
from test_store_claim_loop import STORE_MODE, StoreFiles  # noqa: E402

import linear_provider  # noqa: E402


def blocked_by(linear, blocker, blocked):
    """`blocker` blocks `blocked`, as the blocked issue sees it: an inverse
    relation whose issue is the blocker."""
    issue = linear.issues[blocker]
    linear.issues[blocked].setdefault("inverseRelations", {"nodes": []})[
        "nodes"].append({"type": "blocks", "issue": {
            "id": issue["id"], "state": {"type": issue["state"]["type"]}}})


class FetchTaskBlockersTests(unittest.TestCase):
    """Open KO-1 and Done KO-3 each block KO-2; KO-4 has no relations."""

    def setUp(self):
        self.linear = FakeLinear()
        self.linear.add("KO-1", "the blocker", VALID_BODY)
        self.linear.add("KO-2", "the blocked", VALID_BODY)
        self.linear.add("KO-3", "a done blocker", VALID_BODY, state="Done")
        self.linear.add("KO-4", "a free one", VALID_BODY)
        blocked_by(self.linear, "KO-1", "KO-2")
        blocked_by(self.linear, "KO-3", "KO-2")
        patcher = patch.object(linear_provider, "_gql", self.linear.gql)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_read_back_names_only_the_open_blocker(self):
        task = linear_provider.fetch_task("uuid-KO-2")

        self.assertEqual(task["blocked_by"], ["uuid-KO-1"])
        (query, _), = self.linear.calls
        self.assertIn("inverseRelations", query)

    def test_an_issue_with_no_inverse_relations_is_blocked_by_nothing(self):
        self.assertEqual(linear_provider.fetch_task("uuid-KO-4")["blocked_by"],
                         [])


class BlockedFiles(StoreFiles):
    """The store-mode file board where, once `blocked` is set, KO-130
    blocks KO-131: its one-issue read and its listing both say so, as
    Linear's would."""

    blocked = False

    def _blockers(self, task):
        return ["KO-130"] if self.blocked and task["id"] == "KO-131" else []

    def fetch_task(self, issue_id):
        task = super().fetch_task(issue_id)
        if task is not None and self._blockers(task):
            task["blocked_by"] = self._blockers(task)
        return task

    def listing(self):
        return [dict(task, blocked_by=self._blockers(task))
                for task in self.ready_issues()]


class ClaimBlockersLoopTests(LoopFixture):
    def setUp(self):
        super().setUp()
        self.configure(STORE_MODE)
        files = self.target.parent / "team-1"
        files.mkdir()
        (files / "KO-131.md").write_text(VALID_BODY)
        self.board = BlockedFiles(files)

    def test_a_blocker_gained_after_the_sync_is_waited_on(self):
        def gain_a_blocker(*_):
            self.board.blocked = True
            return True

        out = io.StringIO()
        with patch("holophyte.freshness.critic_admits", gain_a_blocker), \
                patch.object(sys, "stdout", out):
            self.loop(Commit("the scripted work"), APPROVE,
                      provider=self.board)

        self.assertEqual(self.read("SELECT COUNT(*) FROM runs"), [(0,)])
        ((status, column, depends),) = self.read(
            "SELECT status, boardColumn, dependsOn FROM tickets"
            " WHERE linearIdentifier = 'KO-131'")
        self.assertEqual((status, column, json.loads(depends)),
                         ("blocked_on_deps", "ready", ["KO-130"]))
        self.assertIn("KO-131 gained a blocker on the board since the last"
                      " sync; waiting on it", out.getvalue())


if __name__ == "__main__":
    unittest.main()

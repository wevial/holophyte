"""KO-751: the Linear board lists every open issue of its project for an import.

`listing()` is the ready column only: Backlog is filtered out by the query
and an issue without the board's label by the listing. `open_issues()` is
the whole open board -- every state type but completed and canceled,
labelled or not -- each with its column and its open blockers' board ids,
so a move to the native board strands nothing in Linear.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from loop_fixture import VALID_BODY  # noqa: E402
from test_provider import FakeLinear  # noqa: E402

import linear_provider  # noqa: E402
import provider as board_seam  # noqa: E402


class OpenIssuesTests(unittest.TestCase):
    """KO-1 Todo labelled, KO-2 Backlog, KO-3 Todo unlabelled, KO-4 Done,
    KO-5 Canceled; open KO-2 blocks KO-1."""

    def setUp(self):
        self.linear = FakeLinear()
        self.linear.add("KO-1", "ready one", VALID_BODY, priority=2)
        self.linear.add("KO-2", "shelved one", VALID_BODY + "\nshelved\n",
                        state="Backlog", priority=3)
        self.linear.add("KO-3", "unlabelled one", "an untemplated body")
        self.linear.add("KO-4", "done one", VALID_BODY, state="Done")
        self.linear.add("KO-5", "dropped one", VALID_BODY, state="Canceled")
        for ident in ("KO-1", "KO-2", "KO-4", "KO-5"):
            self.linear.issues[ident]["labels"]["nodes"].append(
                {"id": "label-holophyte", "name": "holophyte"})
        self.linear.issues["KO-2"]["relations"]["nodes"].append(
            {"type": "blocks", "relatedIssue": {"identifier": "KO-1"}})
        patcher = patch.object(linear_provider, "_gql", self.linear.gql)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.board = board_seam.LinearBoard("p-1", "T", label="holophyte")

    def test_every_open_issue_with_its_column_and_open_blockers(self):
        got = {t["id"]: t for t in self.board.open_issues()}

        self.assertEqual(sorted(got), ["KO-1", "KO-2", "KO-3"])
        self.assertEqual(
            {i: (t["column"], t["blocked_by"]) for i, t in got.items()},
            {"KO-1": ("ready", ["uuid-KO-2"]), "KO-2": ("backlog", []),
             "KO-3": ("backlog", [])})
        self.assertEqual(
            {i: (t["title"], t["body"], t["priority"]) for i, t in got.items()},
            {"KO-1": ("ready one", VALID_BODY, 2),
             "KO-2": ("shelved one", VALID_BODY + "\nshelved\n", 3),
             "KO-3": ("unlabelled one", "an untemplated body", 0)})

    def test_the_ready_listings_answer_as_before(self):
        listed = self.board.listing()
        self.assertEqual([(t["id"], t["blocked_by"]) for t in listed],
                         [("KO-1", ["uuid-KO-2"])])
        self.assertEqual(self.board.ready_issues(), [])


if __name__ == "__main__":
    unittest.main()

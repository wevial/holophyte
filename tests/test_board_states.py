"""`states()` through the board seam (KO-735): both boards answer each asked
identifier open (with its state name and column), completed, canceled or
gone, and Linear's refused comment raises. The Linear transport is faked
with nothing else patched.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import linear_provider  # noqa: E402
import provider as board_seam  # noqa: E402
from tests.test_provider import FakeLinear, ticket_body  # noqa: E402

BOARD = {"KO-1": "Todo", "KO-2": "Backlog", "KO-3": "Done", "KO-4": "Canceled"}
ASKED = [*BOARD, "KO-5"]


class StatesLinear(FakeLinear):
    """`FakeLinear` whose `CLOSED_QUERY` answer carries each issue's state
    name and labels, as the widened query asks, and whose `commentCreate`
    answers `self.comment_success`."""

    comment_success = True

    def gql(self, query, variables=None):
        variables = variables or {}
        if "number: { in:" in query:
            self.calls.append((query, variables))
            nodes = [{"identifier": i["identifier"], "archivedAt": None,
                      "state": dict(i["state"]), "labels": i["labels"]}
                     for i in self.issues.values()
                     if i["identifier"].split("-")[0] == variables["key"]
                     and int(i["identifier"].split("-")[1]) in variables["numbers"]]
            return {"issues": {"nodes": nodes, "pageInfo": {
                "hasNextPage": False, "endCursor": None}}}
        if "commentCreate" in query and not self.comment_success:
            self.calls.append((query, variables))
            return {"commentCreate": {"success": False}}
        return super().gql(query, variables)


def expected(label_column="ready"):
    gone = {"state": board_seam.GONE, "name": None, "column": None}
    return {"KO-1": {"state": "open", "name": "Todo", "column": label_column},
            "KO-2": {"state": "open", "name": "Backlog", "column": "backlog"},
            "KO-3": {"state": "completed", "name": "Done", "column": None},
            "KO-4": {"state": "canceled", "name": "Canceled",
                     "column": "canceled"},
            "KO-5": gone}


class FileBoardStatesTests(unittest.TestCase):

    def test_each_identifier_answers_open_with_its_column_closed_or_gone(self):
        root = Path(tempfile.mkdtemp()) / "KO"
        root.mkdir()
        board = board_seam.FileProvider(root)
        for identifier, state in BOARD.items():
            (root / f"{identifier}.md").write_text(ticket_body())
            if state != "Todo":
                (root / f"{identifier}.state").write_text(f"{state}\n")

        self.assertEqual(board.states(ASKED), expected())


class LinearBoardStatesTests(unittest.TestCase):

    def setUp(self):
        self.linear = StatesLinear()
        patcher = patch.object(linear_provider, "_gql", self.linear.gql)
        patcher.start()
        self.addCleanup(patcher.stop)
        for identifier, state in BOARD.items():
            self.linear.add(identifier, "t", ticket_body(), state=state)

    def test_each_identifier_answers_open_with_its_column_closed_or_gone(self):
        board = board_seam.LinearBoard("test-project", "test-team")
        self.assertEqual(board.states(ASKED), expected())

    def test_an_open_issue_without_the_boards_label_sits_in_backlog(self):
        board = board_seam.LinearBoard("test-project", "test-team",
                                       label="holophyte")
        self.assertEqual(board.states(ASKED), expected(label_column="backlog"))
        self.linear.issues["KO-1"]["labels"]["nodes"].append(
            {"id": "label-holophyte", "name": "holophyte"})
        self.assertEqual(board.states(["KO-1"])["KO-1"]["column"], "ready")

    def test_a_transport_failure_raises_rather_than_answering_gone(self):
        board = board_seam.LinearBoard("test-project", "test-team")

        def unreachable(query, variables=None):
            raise RuntimeError("Linear's issues request failed: timed out")

        with patch.object(linear_provider, "_gql", unreachable), \
                self.assertRaises(RuntimeError):
            board.states(ASKED)

    def test_a_refused_comment_raises_naming_the_issue(self):
        self.linear.comment_success = False
        with self.assertRaisesRegex(RuntimeError, "KO-1"):
            linear_provider.comment("KO-1", "a note")

    def test_an_accepted_comment_returns(self):
        linear_provider.comment("KO-1", "a note")
        self.assertEqual(self.linear.comments, [("KO-1", "a note")])


if __name__ == "__main__":
    unittest.main()

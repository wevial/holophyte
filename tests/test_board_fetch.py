"""Phase 3 stage 3: a store-mode claim reads its candidate back with one
`fetch_task()`, so the one-issue read must answer what the ready listing
answers -- raw labels (a `holo:` lease label included), priority,
`filed_at` -- and the issue's column by the rule `states()` uses. The
Linear transport is faked with nothing else patched.

Run: python3 -m unittest discover -s tests -p 'test_board_fetch.py' -v
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

FILED = "2026-09-20T10:00:00.000Z"
FILED_MS = 1_789_898_400_000


class FetchLinear(FakeLinear):
    """`FakeLinear` whose `issue(id:` answer carries what the widened
    `ISSUE_QUERY` asks -- the dates, `archivedAt` and the state's type --
    and nothing the query does not name."""

    ASKED = ("identifier", "id", "url", "title", "description", "estimate",
             "priority", "createdAt", "updatedAt", "archivedAt", "state",
             "labels")

    def gql(self, query, variables=None):
        if "issue(id:" in query and "labels" in query:
            self.calls.append((query, variables))
            issue = self.find(variables["id"])
            if issue is None:
                return {"issue": None}
            full = dict(issue, createdAt=FILED, updatedAt=FILED,
                        archivedAt=issue.get("archivedAt"))
            return {"issue": {k: full[k] for k in self.ASKED if k in full}}
        return super().gql(query, variables)


class LinearFetchTests(unittest.TestCase):
    def setUp(self):
        self.linear = FetchLinear()
        patcher = patch.object(linear_provider, "_gql", self.linear.gql)
        patcher.start()
        self.addCleanup(patcher.stop)

    def board(self, label=None):
        return board_seam.LinearBoard("project-1", "team-1", label)

    def seed(self, identifier, state="Todo", labels=(), archived=False):
        self.linear.add(identifier, "do the thing", ticket_body(), estimate=25,
                        state=state, priority=2)
        issue = self.linear.issues[identifier]
        issue["labels"] = {"nodes": [{"id": f"l-{n}", "name": n}
                                     for n in labels]}
        if archived:
            issue["archivedAt"] = FILED

    def test_the_fetch_answers_the_listings_fields_raw_labels_included(self):
        self.seed("KO-1", labels=("ui", "holo:other-host", "factory"))

        task = self.board("factory").fetch_task("uuid-KO-1")

        self.assertEqual(task["labels"], ["ui", "holo:other-host", "factory"])
        self.assertEqual((task["priority"], task["filed_at"],
                          task["updatedAt"], task["column"]),
                         (2, FILED_MS, FILED_MS, "ready"))

    def test_the_fetch_answers_each_column(self):
        cases = (("KO-1", "Todo", ("factory",), False, "ready"),
                 ("KO-2", "Backlog", ("factory",), False, "backlog"),
                 ("KO-3", "Todo", (), False, "backlog"),
                 ("KO-4", "Canceled", ("factory",), False, "canceled"),
                 ("KO-5", "Todo", ("factory",), True, "canceled"),
                 ("KO-6", "Done", ("factory",), False, None))
        for identifier, state, labels, archived, column in cases:
            with self.subTest(identifier=identifier):
                self.seed(identifier, state, labels, archived)
                task = self.board("factory").fetch_task(f"uuid-{identifier}")
                self.assertEqual(task["column"], column)

    def test_without_a_board_label_an_unlabelled_open_issue_is_ready(self):
        self.seed("KO-1")
        self.assertEqual(self.board().fetch_task("uuid-KO-1")["column"],
                         "ready")


class FileFetchTests(unittest.TestCase):
    def test_the_file_boards_column_follows_its_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "team-1"
            root.mkdir()
            board = board_seam.FileProvider(root)
            cases = ((None, "ready"), ("In Progress", "ready"),
                     ("Backlog", "backlog"), ("Canceled", "canceled"),
                     ("Done", None))
            for n, (state, column) in enumerate(cases, 1):
                with self.subTest(state=state):
                    (root / f"KO-{n}.md").write_text(ticket_body())
                    if state is not None:
                        (root / f"KO-{n}.state").write_text(f"{state}\n")
                    self.assertEqual(board.fetch_task(f"KO-{n}")["column"],
                                     column)


if __name__ == "__main__":
    unittest.main()

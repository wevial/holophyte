"""Filing through the board seam (KO-732): `file()`, `update()` and
`stored_body()` answer alike on the file board and on Linear, the Linear
transport faked with nothing else patched.
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
import ticket_template  # noqa: E402
from tests.test_provider import FakeLinear, ticket_body  # noqa: E402


class FilingLinear(FakeLinear):
    """`FakeLinear` that also takes the filing mutations: `issueCreate`,
    `issueRelationCreate`, the title/description/estimate form of
    `issueUpdate`, and the `inverseRelations` read `blockers_of()` makes."""

    def add(self, identifier, *args, **kwargs):
        super().add(identifier, *args, **kwargs)
        self.issues[identifier]["inverseRelations"] = {"nodes": []}

    def hold_blocker(self, identifier, blocker):
        self.issues[identifier]["inverseRelations"]["nodes"].append(
            {"type": "blocks", "issue": {"identifier": blocker}})

    def gql(self, query, variables=None):
        variables = variables or {}
        if "issueCreate" in query:
            self.calls.append((query, variables))
            fields = variables["input"]
            number = 1 + max((int(i.split("-")[1]) for i in self.issues),
                             default=0)
            identifier = f"KO-{number}"
            self.add(identifier, fields["title"], fields["description"],
                     estimate=fields["estimate"],
                     state=fields["stateId"].removeprefix("state-"),
                     priority=fields.get("priority", 0))
            return {"issueCreate": {"success": True, "issue": {
                "id": self.issues[identifier]["id"], "identifier": identifier}}}
        if "issueRelationCreate" in query:
            self.calls.append((query, variables))
            blocker = self.find(variables["input"]["issueId"])
            blocked = self.find(variables["input"]["relatedIssueId"])
            self.hold_blocker(blocked["identifier"], blocker["identifier"])
            return {"issueRelationCreate": {"success": True}}
        if "issueUpdate" in query and "input" in variables:
            self.calls.append((query, variables))
            issue = self.find(variables["id"])
            if issue is None:
                return {"issueUpdate": {"success": False}}
            fields = variables["input"]
            issue.update(title=fields["title"], description=fields["description"],
                         estimate=fields["estimate"])
            return {"issueUpdate": {"success": True}}
        return super().gql(query, variables)

    def relations_created(self):
        return [v["input"] for q, v in self.calls if "issueRelationCreate" in q]


class FilingConformance:
    """What filing promises on any board; a board supplies `self.provider`."""

    def file(self, title, state="Todo", **kwargs):
        body = ticket_body(title=title)
        self.assertEqual(ticket_template.blocking(
            ticket_template.validate(ticket_template.parse(body))), [])
        return self.provider.file(title, body, 25, state, **kwargs), body

    def ready_ids(self):
        return [task["id"] for task in self.provider.ready_issues()]

    def test_a_filed_ticket_reads_back_verbatim_and_only_todo_is_ready(self):
        todo, body = self.file("file the thing")
        backlog, _ = self.file("park the thing", state="Backlog")

        self.assertEqual(self.provider.stored_body(todo), body)
        self.assertEqual(self.provider.fetch_task(todo)["title"], "file the thing")
        self.assertEqual(self.provider.fetch_task(backlog)["title"],
                         "park the thing")
        self.assertIn(todo, self.ready_ids())
        self.assertNotIn(backlog, self.ready_ids())

    def test_an_update_replaces_title_and_body_and_leaves_the_state(self):
        identifier, _ = self.file("park the thing", state="Backlog")
        body = ticket_body(title="revise the thing", summary="Revised.")

        answer = self.provider.update(identifier, "revise the thing", body, 30)

        self.assertEqual(answer, ([], []))
        self.assertEqual(self.provider.stored_body(identifier), body)
        self.assertEqual(self.provider.fetch_task(identifier)["title"],
                         "revise the thing")
        self.assertNotIn(identifier, self.ready_ids())
        with self.assertRaises(RuntimeError):
            self.provider.update("KO-404", "nothing", body, 30)
        with self.assertRaises(RuntimeError):
            self.provider.stored_body("KO-404")


class FileBoardFilingTests(FilingConformance, unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "KO"
        self.root.mkdir()
        self.provider = board_seam.FileProvider(self.root)

    def snapshot(self):
        return {p.name: p.read_text() for p in self.root.iterdir()}

    def test_filing_with_a_blocker_raises_and_writes_nothing(self):
        self.file("file the thing")
        before = self.snapshot()

        with self.assertRaises(RuntimeError):
            self.file("wait for the thing", blockers=("KO-1",))

        self.assertEqual(self.snapshot(), before)


class LinearBoardFilingTests(FilingConformance, unittest.TestCase):
    def setUp(self):
        self.board = FilingLinear()
        patcher = patch.object(linear_provider, "_gql", self.board.gql)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = board_seam.LinearBoard("test-project", "test-team")

    def test_an_update_records_only_the_blockers_the_board_lacks(self):
        for identifier in ("KO-5", "KO-6", "KO-7"):
            self.board.add(identifier, identifier, ticket_body(title=identifier))
        identifier, body = self.file("wait for the thing")
        self.board.hold_blocker(identifier, "KO-5")
        self.board.hold_blocker(identifier, "KO-6")

        answer = self.provider.update(identifier, "wait for the thing", body, 25,
                                      blockers=("KO-6", "KO-7"))

        self.assertEqual(answer, (["KO-7"], ["KO-5"]))
        self.assertEqual(self.board.relations_created(), [
            {"issueId": "uuid-KO-7", "relatedIssueId": f"uuid-{identifier}",
             "type": "blocks"}])


if __name__ == "__main__":
    unittest.main()

"""Conformance suite for the board seam: one set of assertions, two boards.

`provider.Provider` is what `holophyte.loop.main()` drives, and the loop observes a
board through five things only -- what `claim_next()` hands out and in what
order, whether `skip` is honored, whether `fetch_task()` sees an edit made
after the claim, whether `set_state()` is reflected by `fetch_task()` and
`claim_next()`, and whether `comment()` lands on the ticket. The mixin asserts
exactly those, through the protocol, and each board supplies only the seeding
and the readback it alone knows how to do: files on disk for `FileProvider`,
and for `LinearProvider` a fake of the one transport function
(`linear_provider._gql`) serving the canned GraphQL shapes the real module
parses. Nothing above the transport is stubbed, so the Linear case exercises
`list_ready_issues()`, `parse_task()`, `_state_id()` and the mutations as they
run against the API.

Run: python3 -m unittest tests.test_provider -v
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import provider as board_seam  # noqa: E402 - after the sys.path insert above
import ticket_template  # noqa: E402

TITLE = "do the thing"
CRITERION = "Given a claim, when it lands, then the mirror exists."
VERIFY = "python3 -m unittest discover -s tests"
ESTIMATE = 25


def ticket_body(title=TITLE, summary="The thing gets done.", criterion=CRITERION,
                verify=VERIFY, estimate=ESTIMATE):
    """A body `ticket_template.validate()` passes, the shape both boards hand over."""
    return f"""# {title}

## Summary

{summary}

## What / Why / How

**What:** The thing is done by the loop.

**How:** Do it in the obvious place.

## In scope

- Doing the thing.

## Out of scope

- Doing the other thing.

## Acceptance criteria

- [ ] {criterion}

## Verify command(s)

```
{verify}
```

## Implementation notes

- None worth noting.

## Estimate & dependencies

Estimate: {estimate} min · Depends on: none

## Open questions

- None
"""


class ConformanceMixin:
    """The protocol's observable behavior; a board supplies `seed`, `edit`,
    `comments_on` and `self.provider`."""

    def seed(self, identifier, state="Todo", **fields):
        raise NotImplementedError

    def edit(self, identifier, body):
        raise NotImplementedError

    def comments_on(self, identifier):
        raise NotImplementedError

    def claim(self, skip=(), **kwargs):
        # `linear_provider.claim_next()` prints the claim line; a passing
        # suite should not narrate it.
        with contextlib.redirect_stdout(io.StringIO()):
            return self.provider.claim_next(skip=skip, **kwargs)

    def test_claim_offers_the_lowest_todo_identifier_and_honors_skip(self):
        self.seed("KO-2")
        self.seed("KO-1")
        self.seed("KO-3", state="Done")

        self.assertEqual(self.claim()["id"], "KO-1")
        self.assertEqual(self.claim(skip=("KO-1",))["id"], "KO-2")
        # KO-3 is Done: with the two Todo tickets refused there is nothing left.
        self.assertIsNone(self.claim(skip=("KO-1", "KO-2")))

    def test_a_claimed_task_carries_the_parsed_contract(self):
        """The task dict is the shape `parse_task()` produces, with the
        values the seeded body says -- the two boards parse the same body to
        the same contract, and the body is one the claim-time validator
        accepts."""
        self.seed("KO-1", title="add a thing", criterion="Given x, when y, then z.",
                  verify="echo ok", estimate=25)

        task = self.claim()

        self.assertEqual(task["id"], "KO-1")
        self.assertTrue(task["issue_id"])
        self.assertEqual(task["title"], "add a thing")
        self.assertEqual(task["verify"], "echo ok")
        self.assertEqual(task["criteria"], ["Given x, when y, then z."])
        self.assertEqual(task["contracts"], [])
        self.assertEqual(task["budget_min"], 25)
        self.assertIn("## Acceptance criteria", task["body"])
        self.assertEqual(ticket_template.blocking(ticket_template.validate(
            ticket_template.parse(task["body"]))), [])

    def test_fetch_task_sees_an_edit_made_after_the_claim(self):
        self.seed("KO-1")
        task = self.claim()

        self.edit("KO-1", ticket_body(summary="The thing changed."))
        live = self.provider.fetch_task(task["issue_id"])

        self.assertEqual(live["id"], "KO-1")
        self.assertIn("The thing changed.", live["body"])
        self.assertNotIn("The thing changed.", task["body"])

    def test_fetch_task_of_an_unknown_issue_is_none(self):
        self.seed("KO-1")
        self.assertIsNone(self.provider.fetch_task("no-such-issue"))

    def test_a_terminal_state_leaves_fetch_but_not_the_ready_set(self):
        self.seed("KO-1")
        self.seed("KO-2")
        task = self.claim()

        self.provider.set_state(task["issue_id"], "Done")

        self.assertEqual(self.provider.fetch_task(task["issue_id"])["id"], "KO-1")
        self.assertEqual(self.claim()["id"], "KO-2")

    def test_a_comment_is_recorded_on_the_ticket_under_either_id(self):
        """`ledger()` comments by the human id and `escalate()` by the
        board's; both have to land on the same ticket, in order."""
        self.seed("KO-1")
        self.seed("KO-2")
        task = self.claim()

        self.provider.comment(task["id"], "first")
        self.provider.comment(task["issue_id"], "second")

        self.assertEqual(self.comments_on("KO-1"), ["first", "second"])
        self.assertEqual(self.comments_on("KO-2"), [])


class FileProviderTests(ConformanceMixin, unittest.TestCase):
    """`FileProvider` over a temporary directory, in the documented format."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "board"
        self.root.mkdir()
        self.provider = board_seam.FileProvider(self.root)

    def seed(self, identifier, state="Todo", **fields):
        (self.root / f"{identifier}.md").write_text(ticket_body(**fields))
        if state != "Todo":
            (self.root / f"{identifier}.state").write_text(f"{state}\n")

    def edit(self, identifier, body):
        (self.root / f"{identifier}.md").write_text(body)

    def comments_on(self, identifier):
        path = self.root / f"{identifier}.comments.md"
        if not path.exists():
            return []
        return re.findall(r"^## \S+\n\n(.*?)\n\n", path.read_text(), re.S | re.M)

    def test_priority_order_is_identifier_order_on_a_board_without_priority(self):
        """`[loop] order = "priority"` against the file board: a ticket file
        has no priority, so the keyword is accepted and the lowest identifier
        is offered, exactly as under `"identifier"`."""
        self.seed("KO-2")
        self.seed("KO-1")

        self.assertEqual(self.claim(order="priority")["id"], "KO-1")
        self.assertEqual(self.claim(skip=("KO-1",), order="priority")["id"], "KO-2")

    def test_team_is_the_directory_name(self):
        self.assertEqual(self.provider.team, "board")

    def test_in_progress_hides_the_ticket_from_claim_but_not_from_fetch(self):
        """The file board offers Todo only: a ticket in progress is one the
        loop is already working, and its state is what the state file says."""
        self.seed("KO-1")
        self.seed("KO-2")

        self.provider.set_state("KO-1", "In Progress")

        self.assertEqual((self.root / "KO-1.state").read_text().strip(),
                         "In Progress")
        self.assertEqual(self.provider.fetch_task("KO-1")["id"], "KO-1")
        self.assertEqual(self.claim()["id"], "KO-2")

    def test_a_sibling_file_is_not_a_ticket(self):
        """`KO-1.comments.md` sits beside `KO-1.md`; a board that read every
        `*.md` would offer a ticket called `KO-1.comments`."""
        self.seed("KO-1")
        self.provider.comment("KO-1", "a note")

        self.assertEqual(self.claim()["id"], "KO-1")
        self.assertIsNone(self.claim(skip=("KO-1",)))
        self.assertIsNone(self.provider.fetch_task("KO-1.comments"))

    def test_a_state_change_on_a_missing_ticket_raises(self):
        with self.assertRaises(RuntimeError):
            self.provider.set_state("KO-9", "Done")
        self.assertFalse((self.root / "KO-9.state").exists())


STATE_TYPES = {"Todo": "unstarted", "In Progress": "started", "Done": "completed",
               "Canceled": "canceled", "Backlog": "backlog"}


class FakeLinear:
    """`linear_provider._gql` with a board behind it, serving the shapes the
    module's queries and mutations expect and keeping what they wrote."""

    def __init__(self):
        self.issues = {}
        self.comments = []
        self.calls = []

    def add(self, identifier, title, description, estimate=None, state="Todo",
            priority=0):
        self.issues[identifier] = {
            "identifier": identifier, "id": f"uuid-{identifier}",
            "title": title, "description": description, "estimate": estimate,
            "priority": priority,
            "state": {"name": state, "type": STATE_TYPES[state]},
            "relations": {"nodes": []}}

    def find(self, ref):
        """Linear resolves an issue by its UUID or its identifier."""
        for issue in self.issues.values():
            if ref in (issue["id"], issue["identifier"]):
                return issue
        return None

    def gql(self, query, variables=None):
        variables = variables or {}
        self.calls.append((query, variables))
        if "workflowStates" in query:
            return {"workflowStates": {"nodes": [
                {"id": f"state-{name}", "name": name, "type": kind}
                for name, kind in STATE_TYPES.items()]}}
        if "issueUpdate" in query:
            issue = self.find(variables["id"])
            if issue is None:
                return {"issueUpdate": {"success": False}}
            name = variables["state"].removeprefix("state-")
            issue["state"] = {"name": name, "type": STATE_TYPES[name]}
            return {"issueUpdate": {"success": True}}
        if "commentCreate" in query:
            issue = self.find(variables["issue"])
            if issue is None:
                raise RuntimeError("Linear GraphQL error: issue not found")
            self.comments.append((issue["identifier"], variables["body"]))
            return {"commentCreate": {"success": True}}
        if "issue(id:" in query:
            issue = self.find(variables["id"])
            return {"issue": dict(issue) if issue else None}
        nodes = list(self.issues.values())
        if "nin:" in query:  # READY_QUERY's state filter; RELATIONS_QUERY has none
            nodes = [i for i in nodes
                     if i["state"]["type"] not in ("completed", "canceled", "backlog")]
        return {"project": {"issues": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}


class LinearProviderTests(ConformanceMixin, unittest.TestCase):
    """`LinearProvider` with the transport faked and nothing else."""

    @classmethod
    def setUpClass(cls):
        # linear_provider refuses to import without a configured project; the
        # fake serves every project alike, so the value is a placeholder.
        os.environ.setdefault("HOLO2_PROJECT_ID", "test-project")
        os.environ.setdefault("HOLO2_TEAM", "test-team")
        import linear_provider
        cls.linear = linear_provider

    def setUp(self):
        self.board = FakeLinear()
        patcher = patch.object(self.linear, "_gql", self.board.gql)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = board_seam.LinearProvider()

    def seed(self, identifier, state="Todo", **fields):
        # `priority` is an issue field, not part of the body's template.
        priority = fields.pop("priority", 0)
        title = fields.get("title", TITLE)
        self.board.add(identifier, title, ticket_body(**fields),
                       estimate=fields.get("estimate", ESTIMATE), state=state,
                       priority=priority)

    def edit(self, identifier, body):
        self.board.issues[identifier]["description"] = body

    def comments_on(self, identifier):
        return [body for issue, body in self.board.comments if issue == identifier]

    def test_priority_order_claims_the_most_urgent_first_and_unprioritised_last(self):
        """`order="priority"`: Linear's 1 (urgent) before 3 (medium) before
        0 (none), whatever the identifiers say -- KO-3 is the urgent one and
        KO-2 the unprioritised one, and the claim walks 3, 1, 2."""
        self.seed("KO-3", priority=1)
        self.seed("KO-1", priority=3)
        self.seed("KO-2", priority=0)

        self.assertEqual(self.claim(order="priority")["id"], "KO-3")
        self.assertEqual(self.claim(skip=("KO-3",), order="priority")["id"], "KO-1")
        self.assertEqual(self.claim(skip=("KO-3", "KO-1"), order="priority")["id"],
                         "KO-2")

    def test_priority_order_breaks_ties_by_identifier(self):
        self.seed("KO-2", priority=2)
        self.seed("KO-1", priority=2)
        self.seed("KO-3", priority=1)

        self.assertEqual(self.claim(order="priority")["id"], "KO-3")
        self.assertEqual(self.claim(skip=("KO-3",), order="priority")["id"], "KO-1")

    def test_identifier_order_ignores_priority(self):
        """The default, spelled out: a P1 filed after a P3 waits behind it."""
        self.seed("KO-3", priority=1)
        self.seed("KO-1", priority=3)

        self.assertEqual(self.claim(order="identifier")["id"], "KO-1")
        self.assertEqual(self.claim()["id"], "KO-1")

    def test_the_ready_query_asks_for_priority(self):
        """The sort is only as good as the field: the ready query names
        `priority` so the fake's canned value is what the real board would
        also return."""
        self.seed("KO-1")
        self.claim(order="priority")

        asked = [query for query, _ in self.board.calls if "nin:" in query]
        self.assertTrue(asked and all("priority" in q for q in asked))

    def test_team_is_the_team_the_state_lookup_asks_for(self):
        """`team` names the board the states are resolved in: the workflow
        state query goes to exactly that team."""
        self.seed("KO-1")
        self.provider.set_state("uuid-KO-1", "Done")

        asked = [variables["team"] for query, variables in self.board.calls
                 if "workflowStates" in query]
        self.assertEqual(asked, [self.provider.team])

    def test_construction_and_team_reach_no_transport(self):
        def tripwire(query, variables=None):
            raise AssertionError(f"_gql was reached: {query[:40]}")

        with patch.object(self.linear, "_gql", tripwire):
            fresh = board_seam.LinearProvider()
            self.assertEqual(fresh.team, self.provider.team)


class LinearTeamConfigTests(unittest.TestCase):
    """`team` comes from `HOLO2_TEAM`, the way the project id comes from
    `HOLO2_PROJECT_ID`: from the environment or a `.env` beside the module,
    never from a name written into the code.

    Each test imports a fresh copy of `linear_provider.py` from a directory
    with no `.env` in it, so the operator's own file cannot stand in for the
    variable, and pops it again afterwards so the copy never serves anyone
    else.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shutil.copy(ROOT / "linear_provider.py", tmp.name)
        self.saved = sys.modules.pop("linear_provider", None)
        self.addCleanup(self.restore)
        sys.path.insert(0, tmp.name)
        self.addCleanup(sys.path.remove, tmp.name)

    def restore(self):
        sys.modules.pop("linear_provider", None)
        if self.saved is not None:
            sys.modules["linear_provider"] = self.saved

    def test_an_unset_team_is_a_startup_error_naming_the_variable(self):
        env = {"HOLO2_PROJECT_ID": "test-project"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError) as raised:
                board_seam.LinearProvider().team
        self.assertIn("HOLO2_TEAM", str(raised.exception))

    def test_the_team_is_the_one_the_environment_names(self):
        env = {"HOLO2_PROJECT_ID": "test-project", "HOLO2_TEAM": "Example Team"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(board_seam.LinearProvider().team, "Example Team")


if __name__ == "__main__":
    unittest.main()

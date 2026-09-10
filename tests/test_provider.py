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
import subprocess
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

    def issue_id(self, identifier):
        """The board's canonical id for a seeded ticket."""
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

    def test_ready_issues_lists_every_task_claim_would_offer(self):
        """The listing the claim chooses from, parsed: the Todo tickets and
        not the closed one, each in the shape `claim_next()` hands out."""
        self.seed("KO-2")
        self.seed("KO-1")
        self.seed("KO-3", state="Done")

        ready = self.provider.ready_issues()

        self.assertEqual(sorted(task["id"] for task in ready), ["KO-1", "KO-2"])
        for task in ready:
            self.assertTrue(task["issue_id"])
            self.assertEqual(task["title"], TITLE)
            self.assertEqual(task["criteria"], [CRITERION])
            self.assertEqual(task["verify"], VERIFY)
            self.assertIn("## Acceptance criteria", task["body"])

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

    def test_closed_identifiers_names_the_done_and_cancelled_ones_by_type(self):
        """One ask over the open mirror's identifiers: a Done ticket answers
        `completed`, a Canceled one `canceled`, and an open or unknown
        identifier is absent rather than answered."""
        self.seed("KO-1", state="Done")
        self.seed("KO-2", state="Canceled")
        self.seed("KO-3")
        self.seed("KO-4", state="In Progress")

        closed = self.provider.closed_identifiers(
            ["KO-1", "KO-2", "KO-3", "KO-4", "KO-9"])

        self.assertEqual(closed, {"KO-1": "completed", "KO-2": "canceled"})

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

    def test_a_label_added_rides_the_listing_and_comes_off_on_unlabel(self):
        """The board lease (KO-351): `label_issue()` puts a label on the
        ticket the next claim and listing can read back in `labels`,
        creating the label on first use; `unlabel_issue()` takes exactly
        that one off and leaves the ticket's other labels alone."""
        self.seed("KO-1")
        self.assertEqual(self.claim()["labels"], [])

        self.provider.label_issue(self.issue_id("KO-1"), "holo:writer-1")
        self.provider.label_issue(self.issue_id("KO-1"), "other")
        self.provider.label_issue(self.issue_id("KO-1"), "holo:writer-1")

        (task,) = self.provider.ready_issues()
        self.assertEqual(task["labels"], ["holo:writer-1", "other"])

        self.provider.unlabel_issue(self.issue_id("KO-1"), "holo:writer-1")
        self.provider.unlabel_issue(self.issue_id("KO-1"), "holo:writer-1")

        self.assertEqual(self.claim()["labels"], ["other"])

    def test_a_lease_label_another_holder_carries_refuses_the_write(self):
        """A `prefix:` label is a lease, exclusive per prefix (KO-351):
        `label_issue()` reads the ticket at the write, and one already
        carrying `holo:writer-2` refuses `holo:writer-1` with `LeaseHeld`
        naming writer-2, writing nothing. An ordinary label is not a lease
        and lands beside it; the holder's own label is idempotent."""
        self.seed("KO-1")
        self.provider.label_issue(self.issue_id("KO-1"), "holo:writer-2")

        with self.assertRaises(board_seam.LeaseHeld) as held:
            self.provider.label_issue(self.issue_id("KO-1"), "holo:writer-1")
        self.assertEqual(held.exception.holder, "writer-2")

        self.provider.label_issue(self.issue_id("KO-1"), "other")
        self.provider.label_issue(self.issue_id("KO-1"), "holo:writer-2")
        self.assertEqual(self.claim()["labels"], ["holo:writer-2", "other"])


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

    def issue_id(self, identifier):
        return identifier  # a ticket file has one name

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
        self.team_labels = {}  # name -> id, created on first use
        self.on_add = None

    def add(self, identifier, title, description, estimate=None, state="Todo",
            priority=0):
        self.issues[identifier] = {
            "identifier": identifier, "id": f"uuid-{identifier}",
            "title": title, "description": description, "estimate": estimate,
            "priority": priority,
            "state": {"name": state, "type": STATE_TYPES[state]},
            "labels": {"nodes": []},
            "relations": {"nodes": []}}

    def find(self, ref):
        """Linear resolves an issue by its UUID or its identifier."""
        for issue in self.issues.values():
            if ref in (issue["id"], issue["identifier"]):
                return issue
        return None

    def labels_gql(self, query, variables):
        """The label half of the transport (KO-351): the team's label
        lookup and creation, and the `addedLabelIds`/`removedLabelIds`
        forms of `issueUpdate`, each touching only the labels named; None
        for a query that is none of those. `on_add`, when set, runs after
        an add lands -- a second writer's hand on the same ticket, for the
        race test."""
        if "issueUpdate" in query and "LabelIds" in query:
            issue = self.find(variables["id"])
            if issue is None:
                return {"issueUpdate": {"success": False}}
            by_id = {i: n for n, i in self.team_labels.items()}
            nodes = issue["labels"]["nodes"]
            if "addedLabelIds" in query:
                nodes.extend({"id": i, "name": by_id[i]} for i in variables["labels"]
                             if all(n["id"] != i for n in nodes))
                if self.on_add is not None:
                    self.on_add(issue)
            else:
                nodes[:] = [n for n in nodes if n["id"] not in variables["labels"]]
            return {"issueUpdate": {"success": True}}
        if "issueLabels(filter:" in query:
            label_id = self.team_labels.get(variables["name"])
            return {"issueLabels": {"nodes": [{"id": label_id}] if label_id else []}}
        if "issueLabelCreate" in query:
            name = variables["input"]["name"]
            self.team_labels[name] = f"label-{name}"
            return {"issueLabelCreate": {"success": True,
                                         "issueLabel": {"id": f"label-{name}"}}}
        if "teams(filter:" in query:
            return {"teams": {"nodes": [{"id": "team-1"}]}}
        return None

    def gql(self, query, variables=None):
        variables = variables or {}
        self.calls.append((query, variables))
        if "workflowStates" in query:
            return {"workflowStates": {"nodes": [
                {"id": f"state-{name}", "name": name, "type": kind}
                for name, kind in STATE_TYPES.items()]}}
        labelled = self.labels_gql(query, variables)
        if labelled is not None:
            return labelled
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
        if "number: { in:" in query:  # CLOSED_QUERY: team key + numbers
            nodes = [i for i in self.issues.values()
                     if i["identifier"].split("-")[0] == variables["key"]
                     and int(i["identifier"].split("-")[1]) in variables["numbers"]]
            return {"issues": {
                "nodes": [{"identifier": i["identifier"], "archivedAt": None,
                           "state": i["state"]}
                          for i in nodes],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}
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
        import linear_provider
        cls.linear = linear_provider

    def setUp(self):
        self.board = FakeLinear()
        patcher = patch.object(self.linear, "_gql", self.board.gql)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.provider = board_seam.LinearProvider("test-project", "test-team")

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

    def issue_id(self, identifier):
        return self.board.issues[identifier]["id"]

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

    def test_two_writers_whose_reads_both_saw_nothing_do_not_both_hold(self):
        """The lease race the label cannot lose (KO-351 review): Linear has
        no compare-and-swap, so writer-1 reads no lease, and writer-2 --
        having read the same -- lands its own label the moment writer-1's
        add does. The whole-list `labelIds` write would let the last one
        replace the first and both would start; the additive write keeps
        both labels on the ticket, writer-1's read-back sees writer-2 and
        writer-1 yields: `LeaseHeld` naming writer-2, its own label taken
        back, and nothing else on the ticket touched. Writer-2's later
        read-back then sees only itself and holds -- one writer, not two."""
        self.seed("KO-1")
        self.provider.label_issue(self.issue_id("KO-1"), "human-added")
        writer_2 = self.linear._label_id("holo:writer-2", "test-team")

        def writer_2_lands_too(issue):
            issue["labels"]["nodes"].append({"id": writer_2, "name": "holo:writer-2"})
            self.board.on_add = None
        self.board.on_add = writer_2_lands_too

        with self.assertRaises(board_seam.LeaseHeld) as held:
            self.provider.label_issue(self.issue_id("KO-1"), "holo:writer-1")
        self.assertEqual(held.exception.holder, "writer-2")
        self.assertEqual(self.claim()["labels"], ["human-added", "holo:writer-2"])
        # Writer-2, reading back, finds only itself: its claim stands.
        self.provider.label_issue(self.issue_id("KO-1"), "holo:writer-2")
        self.assertEqual(self.claim()["labels"], ["human-added", "holo:writer-2"])

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

    def test_closed_identifiers_sees_archived_issues_and_cancels_an_archived_open(self):
        """Linear omits archived issues unless asked, and a Done ticket
        Linear archived on its own was a ghost on the board. The answer is
        read straight off a fake `_paginate`: a completed issue is
        `completed` archived or not, an archived issue whose state is still
        open is `canceled` (nobody will work it), and an unarchived open
        issue is absent; and the query sent asks for archived issues and
        for the field the rule reads."""
        recorded = []

        def paginate(query, variables, path):
            recorded.append(query)
            return [
                {"identifier": "KO-1", "archivedAt": None,
                 "state": {"type": "completed"}},
                {"identifier": "KO-2", "archivedAt": "2026-09-02T00:00:00.000Z",
                 "state": {"type": "completed"}},
                {"identifier": "KO-3", "archivedAt": "2026-09-02T00:00:00.000Z",
                 "state": {"type": "unstarted"}},
                {"identifier": "KO-4", "archivedAt": None,
                 "state": {"type": "unstarted"}},
            ]

        with patch.object(self.linear, "_paginate", paginate):
            closed = self.provider.closed_identifiers(
                ["KO-1", "KO-2", "KO-3", "KO-4"])

        self.assertEqual(closed, {"KO-1": "completed", "KO-2": "completed",
                                  "KO-3": "canceled"})
        self.assertEqual(len(recorded), 1)
        self.assertIn("includeArchived: true", recorded[0])
        self.assertIn("archivedAt", recorded[0])

    def test_construction_and_team_reach_no_transport(self):
        def tripwire(query, variables=None):
            raise AssertionError(f"_gql was reached: {query[:40]}")

        with patch.object(self.linear, "_gql", tripwire):
            fresh = board_seam.LinearProvider("test-project", "test-team")
            self.assertEqual(fresh.team, "test-team")


class LinearImportTests(unittest.TestCase):
    """Importing `linear_provider` reads no configuration.

    The board is the target's `[board]` table, handed to `LinearProvider`;
    the module holds no project or team of its own, so importing it with no
    `HOLO2_*` variables and no `.env` beside it succeeds. The import runs in
    a subprocess from a copy of the module in a directory with no `.env`, so
    neither this process's modules nor the operator's own file can stand in
    for the configuration the import must not need.
    """

    def test_the_module_imports_with_no_configuration_at_all(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in ("linear_provider.py", "ticket_template.py"):
            shutil.copy(ROOT / name, tmp.name)
        env = {k: v for k, v in os.environ.items()
               if k not in ("HOLO2_PROJECT_ID", "HOLO2_TEAM")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        done = subprocess.run(
            [sys.executable, "-c",
             "import linear_provider; "
             "print(hasattr(linear_provider, 'PROJECT_ID'), "
             "hasattr(linear_provider, 'TEAM'))"],
            cwd=tmp.name, env=env, capture_output=True, text=True)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "False False")

    def test_the_provider_carries_the_pair_it_was_built_with(self):
        """`team` is the stored value, not a module read: two providers on
        one host answer with their own boards."""
        one = board_seam.LinearProvider("p-1", "Team One")
        two = board_seam.LinearProvider("p-2", "Team Two")

        self.assertEqual((one.project_id, one.team), ("p-1", "Team One"))
        self.assertEqual((two.project_id, two.team), ("p-2", "Team Two"))


if __name__ == "__main__":
    unittest.main()

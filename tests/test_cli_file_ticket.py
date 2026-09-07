"""`factory.py TARGET --file-ticket TICKET.md [--state] [--priority]`: a
validated markdown
file becomes a Linear issue, and the body Linear stored is validated again.

The transport (`linear_provider._gql`) is patched to capture every mutation
and to answer the lookups the sequence makes, so what is witnessed is the
wiring: the `issueCreate` input the file becomes, the `blocks` relation its
Depends-on line becomes, the printed line, and the three exits -- a file
that fails validation creates nothing, a stored body that fails it prints
the identifier with the problem, a target with no board exits before the
file is read.

Run: python3 -m unittest discover -s tests -p 'test_cli_*' -v
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import holophyte.config
import holophyte.target
import linear_provider
import ticket_template

TICKET = """\
# Add export endpoint

## Summary

Add a CSV export endpoint for the orders list.

## What / Why / How

**What:** GET /orders.csv streams the current user's orders as CSV.

**Why:** Ops needs orders in spreadsheets without database access.

**How:** Reuse the orders query service and stream via the csv module.

## In scope

- CSV serialization of the orders list

## Out of scope

- Excel-specific formatting

## Acceptance criteria

- [ ] Given 3 orders, when GET /orders.csv, then 4 lines including header.

## Verify command(s)

```
.venv/bin/python -m unittest test_orders_export
```

## Implementation notes

- Endpoint lives beside the other order routes.

## Estimate & dependencies

Estimate: 25 min · Depends on: KO-7000

## Open questions

- None
"""

STATES = [{"id": "state-todo", "name": "Todo", "type": "unstarted"},
          {"id": "state-backlog", "name": "Backlog", "type": "backlog"},
          {"id": "state-done", "name": "Done", "type": "completed"}]


class FakeLinear:
    """`_gql` with a board behind it: records every call, answers the lookups
    `create_issue()`/`add_blocker()`/`fetch_description()`/`blockers_of()`
    make, and hands back `stored` as the created issue's description.

    `blockers` are the identifiers the board already holds as blocking the
    issue, in the board's order, served through `inverseRelations` beside
    one non-`blocks` edge so the filter is exercised; `refuse_update` makes
    the body update answer `success: false`."""

    def __init__(self, stored=TICKET, blockers=(), refuse_update=False):
        self.calls = []
        self.stored = stored
        self.blockers = list(blockers)
        self.refuse_update = refuse_update

    def __call__(self, query, variables=None):
        self.calls.append((query, variables))
        if "teams(" in query:
            self.assert_team(variables["team"])
            return {"teams": {"nodes": [{"id": "team-uuid"}]}}
        if "workflowStates" in query:
            self.assert_team(variables["team"])
            return {"workflowStates": {"nodes": STATES}}
        if "issueCreate" in query:
            return {"issueCreate": {"success": True, "issue": {
                "id": "new-uuid", "identifier": "KO-9"}}}
        if "issueRelationCreate" in query:
            return {"issueRelationCreate": {"success": True}}
        if "issueUpdate" in query:
            return {"issueUpdate": {"success": not self.refuse_update}}
        if "inverseRelations" in query:
            nodes = [{"type": "blocks", "issue": {"identifier": b}}
                     for b in self.blockers]
            nodes.append({"type": "related", "issue": {"identifier": "KO-99"}})
            return {"issue": {"inverseRelations": {"nodes": nodes}}}
        if "{ id }" in query:
            return {"issue": {"id": f"uuid-{variables['id']}"}}
        if "description" in query:
            return {"issue": {"description": self.stored}}
        raise AssertionError(f"unexpected query: {query[:60]}")

    @staticmethod
    def assert_team(team):
        if team != "T":
            raise AssertionError(f"looked up in team {team!r}, not 'T'")

    def mutations(self):
        return [(q, v) for q, v in self.calls if q.startswith("mutation")]

    def created(self):
        inputs = [v["input"] for q, v in self.mutations() if "issueCreate" in q]
        assert len(inputs) == 1, inputs
        return inputs[0]

    def relations(self):
        return [v["input"] for q, v in self.mutations()
                if "issueRelationCreate" in q]

    def updates(self):
        return [(v["id"], v["input"]) for q, v in self.mutations()
                if "issueUpdate" in q]

    def relation_reads(self):
        return [q for q, _ in self.calls if "inverseRelations" in q]


class FileTicketCliTests(unittest.TestCase):
    # The retired `HOLO2_*` fallback, set so a stand-in would be caught.
    RETIRED_ENV = {"HOLO2_PROJECT_ID": "p-env", "HOLO2_TEAM": "Env Team"}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.dict(os.environ,
                             {"HOLOPHYTE_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.target = holophyte.target.Target.locate(self.repo)
        self.ticket = self.root / "tickets" / "01-export.md"
        self.ticket.parent.mkdir()
        self.ticket.write_text(TICKET)

    def with_board(self):
        self.target.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.target.config_path.write_text(
            '[board]\nproject_id = "p-1"\nteam = "T"\n')

    def cli(self, *args, linear=None):
        """Run the command line against `linear` as the transport; the exit
        status and what was printed."""
        linear = linear or FakeLinear()
        out = io.StringIO()
        with patch.object(linear_provider, "_gql", linear), \
                contextlib.redirect_stdout(out):
            status = holophyte.cli.cli(
                [str(self.repo), "--file-ticket", str(self.ticket), *args])
        return status, out.getvalue()

    def test_a_valid_file_is_filed_with_its_body_estimate_state_and_blocker(self):
        self.with_board()
        linear = FakeLinear()

        status, printed = self.cli(linear=linear)

        self.assertEqual(status, 0)
        self.assertEqual(linear.created(), {
            "projectId": "p-1", "teamId": "team-uuid",
            "title": "Add export endpoint", "description": TICKET,
            "estimate": 25, "stateId": "state-todo"})
        self.assertEqual(linear.relations(), [
            {"issueId": "uuid-KO-7000", "relatedIssueId": "new-uuid",
             "type": "blocks"}])
        self.assertEqual(printed.strip(),
                         "[holo2] filed KO-9: Add export endpoint "
                         "(Todo, 25 min, blocked by KO-7000)")

    def test_a_file_with_a_blocker_creates_nothing_and_exits_1(self):
        self.with_board()
        self.ticket.write_text(TICKET.replace(
            "**What:** GET /orders.csv streams the current user's orders as CSV.",
            "**What:** <What observable behavior are we delivering?>"))
        linear = FakeLinear()

        status, printed = self.cli(linear=linear)

        self.assertEqual(status, 1)
        self.assertIn("placeholder", printed)
        self.assertIn(str(self.ticket), printed)
        self.assertEqual(linear.mutations(), [])

    def test_a_stored_body_that_fails_validation_prints_the_id_and_exits_2(self):
        self.with_board()
        rewritten = TICKET.replace("**What:**", "What:").replace(
            "- Endpoint lives beside the other order routes.",
            "- <Known constraints>")
        linear = FakeLinear(stored=rewritten)

        status, printed = self.cli(linear=linear)

        self.assertEqual(status, 2)
        lines = printed.strip().splitlines()
        self.assertEqual(lines[0], "[holo2] filed KO-9: Add export endpoint "
                                   "(Todo, 25 min, blocked by KO-7000)")
        # The problem printed is the re-validation's first, not the file's:
        # the file itself passed, and the issue was created.
        first = ticket_template.blocking(
            ticket_template.validate(ticket_template.parse(rewritten)))[0]
        self.assertTrue(lines[1].startswith("[holo2] KO-9: "), lines[1])
        self.assertIn(first, lines[1])
        self.assertEqual(linear.created()["description"], TICKET)

    def test_state_backlog_is_used_and_state_done_is_refused(self):
        self.with_board()
        linear = FakeLinear()

        status, printed = self.cli("--state", "Backlog", linear=linear)

        self.assertEqual(status, 0)
        self.assertEqual(linear.created()["stateId"], "state-backlog")
        self.assertIn("(Backlog, 25 min", printed)

        refused = FakeLinear()
        with self.assertRaises(SystemExit) as raised, \
                contextlib.redirect_stderr(io.StringIO()):
            self.cli("--state", "Done", linear=refused)
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(refused.calls, [])

    def test_priority_high_is_sent_as_2_and_printed_by_its_word(self):
        self.with_board()
        linear = FakeLinear()

        status, printed = self.cli("--priority", "high", linear=linear)

        self.assertEqual(status, 0)
        self.assertEqual(linear.created()["priority"], 2)
        self.assertTrue(printed.strip().endswith("high)"), printed)

    def test_no_priority_flag_sends_no_priority_field(self):
        self.with_board()
        linear = FakeLinear()

        status, _ = self.cli(linear=linear)

        self.assertEqual(status, 0)
        self.assertNotIn("priority", linear.created())

    def test_priority_without_file_ticket_is_refused_naming_file_ticket(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as raised, \
                contextlib.redirect_stderr(err):
            holophyte.cli.cli([str(self.repo), "--priority", "high"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--file-ticket", err.getvalue())

    def test_update_replaces_title_body_and_estimate_and_creates_nothing(self):
        self.with_board()
        revised = TICKET.replace("# Add export endpoint", "# T2").replace(
            "Estimate: 25 min", "Estimate: 20 min")
        self.ticket.write_text(revised)
        linear = FakeLinear(stored=revised, blockers=["KO-7000"])

        status, printed = self.cli("--update", "KO-7000", linear=linear)

        self.assertEqual(status, 0)
        self.assertEqual(linear.updates(), [("uuid-KO-7000", {
            "title": "T2", "description": revised, "estimate": 20})])
        self.assertEqual(len(linear.mutations()), 1)
        self.assertEqual(linear.relations(), [])
        self.assertEqual(printed.strip(), "[holo2] updated KO-7000: T2")

    def test_update_of_a_file_with_a_blocker_sends_nothing_and_exits_1(self):
        self.with_board()
        self.ticket.write_text(TICKET.replace(
            "**What:** GET /orders.csv streams the current user's orders as CSV.",
            "**What:** <What observable behavior are we delivering?>"))
        linear = FakeLinear()

        status, printed = self.cli("--update", "KO-7000", linear=linear)

        self.assertEqual(status, 1)
        self.assertIn("placeholder", printed)
        self.assertEqual(linear.mutations(), [])

    def test_update_whose_stored_body_fails_validation_prints_the_id_and_exits_2(self):
        self.with_board()
        rewritten = TICKET.replace(
            "- Endpoint lives beside the other order routes.",
            "- <Known constraints>")
        linear = FakeLinear(stored=rewritten, blockers=["KO-7000"])

        status, printed = self.cli("--update", "KO-7000", linear=linear)

        self.assertEqual(status, 2)
        lines = printed.strip().splitlines()
        self.assertEqual(lines[0], "[holo2] updated KO-7000: Add export endpoint")
        first = ticket_template.blocking(
            ticket_template.validate(ticket_template.parse(rewritten)))[0]
        self.assertTrue(lines[1].startswith("[holo2] KO-7000: "), lines[1])
        self.assertIn(first, lines[1])

    def update_naming(self, depends_on, **kw):
        """`--update KO-7000` from the fixture with its `Depends on:` line
        replaced by `depends_on`, against a board holding `kw['blockers']`;
        the fake, the exit status and what was printed."""
        self.with_board()
        revised = TICKET.replace("Depends on: KO-7000",
                                 f"Depends on: {depends_on}")
        self.ticket.write_text(revised)
        linear = FakeLinear(stored=revised, **kw)
        status, printed = self.cli("--update", "KO-7000", linear=linear)
        return linear, status, printed

    def test_update_records_the_blockers_the_file_names_and_the_board_lacks(self):
        linear, status, printed = self.update_naming(
            "KO-1, KO-2", blockers=["KO-1"])

        self.assertEqual(status, 0)
        self.assertEqual(linear.relations(), [{
            "issueId": "uuid-KO-2", "relatedIssueId": "uuid-KO-7000",
            "type": "blocks"}])
        self.assertEqual(
            printed.strip(),
            "[holo2] updated KO-7000: Add export endpoint (blocked by +KO-2)")

    def test_update_leaves_a_blocker_the_file_dropped_and_names_it(self):
        linear, status, printed = self.update_naming(
            "KO-1", blockers=["KO-1", "KO-3"])

        self.assertEqual(status, 0)
        self.assertEqual(linear.relations(), [])
        self.assertEqual(len(linear.mutations()), 1)
        self.assertEqual(
            printed.strip(),
            "[holo2] updated KO-7000: Add export endpoint "
            "(board also holds KO-3)")

    def test_update_with_depends_on_none_only_reads_the_relations(self):
        linear, status, printed = self.update_naming("none")

        self.assertEqual(status, 0)
        self.assertEqual(len(linear.relation_reads()), 1)
        self.assertEqual(linear.relations(), [])
        self.assertEqual(len(linear.mutations()), 1)
        self.assertEqual(printed.strip(),
                         "[holo2] updated KO-7000: Add export endpoint")

    def test_update_adds_both_lists_to_the_line_in_file_then_board_order(self):
        linear, status, printed = self.update_naming(
            "KO-2, KO-1, KO-4", blockers=["KO-3", "KO-1"])

        self.assertEqual(status, 0)
        self.assertEqual([r["issueId"] for r in linear.relations()],
                         ["uuid-KO-2", "uuid-KO-4"])
        self.assertEqual(
            printed.strip(),
            "[holo2] updated KO-7000: Add export endpoint "
            "(blocked by +KO-2, +KO-4; board also holds KO-3)")

    def test_a_refused_update_adds_no_relation(self):
        self.with_board()
        revised = TICKET.replace("Depends on: KO-7000", "Depends on: KO-2")
        self.ticket.write_text(revised)
        linear = FakeLinear(stored=revised, refuse_update=True)

        with self.assertRaises(RuntimeError) as raised:
            self.cli("--update", "KO-7000", linear=linear)

        self.assertIn("KO-7000", str(raised.exception))
        self.assertEqual(linear.relations(), [])
        self.assertEqual(len(linear.mutations()), 1)

    def test_update_without_file_ticket_and_update_with_priority_are_refused(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as raised, \
                contextlib.redirect_stderr(err):
            holophyte.cli.cli([str(self.repo), "--update", "KO-7000"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--update", err.getvalue())
        self.assertIn("--file-ticket", err.getvalue())

        err = io.StringIO()
        linear = FakeLinear()
        with self.assertRaises(SystemExit) as raised, \
                contextlib.redirect_stderr(err):
            self.cli("--update", "KO-7000", "--priority", "high",
                     linear=linear)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--priority", err.getvalue())
        self.assertIn("--update", err.getvalue())
        self.assertEqual(linear.calls, [])

    def test_a_target_with_no_board_exits_naming_the_key_before_reading_the_file(self):
        self.ticket.unlink()
        linear = FakeLinear()

        with patch.dict(os.environ, self.RETIRED_ENV), \
                self.assertRaises(SystemExit) as raised:
            self.cli(linear=linear)

        self.assertIn("[board] project_id", str(raised.exception))
        self.assertEqual(linear.calls, [])


if __name__ == "__main__":
    unittest.main()

"""`holo mcp`'s write tools: each requires a non-blank note and author, runs
its `holo` verb and records the author as `AUTHOR via MCP`; driven by the
SDK's stdio client against a real subprocess and read back from the store.

Run: python3 -m unittest discover -s tests -p 'test_holo_mcp_writes.py' -v
"""
import getpass
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
import holophyte.cli.operator
import store
import store.board
import store.tickets
from holophyte.config.project import Project
from tests.phase_fixture import park_run
from tests.test_holo_mcp import MINUTE, NOW, TOOLS, McpCase

WRITES = {"file_ticket", "send_back", "babysit", "requeue", "hold"}
URL = "https://example.test/pull/7"
AUTHOR = "test seat"
SIGNED = "test seat via MCP"
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
echo ok
```

## Implementation notes

- Endpoint lives beside the other order routes.

## Estimate & dependencies

Estimate: 25 min · Depends on: none

## Open questions

- None
"""


class WriteCase(McpCase):
    """A temporary home registering `holo`, a native board named by
    HOLO_PROJECT, whose store holds HOLO-1 with a failed run, HOLO-2 with a
    live run and HOLO-3 parked awaiting merge approval on a pull request."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.enterContext(patch.dict(os.environ,
                                     {"HOLOPHYTE_HOME": str(self.home)}))
        self.repo = self.root / "holo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        target = Project.locate(self.repo, adopt=False)
        target.holo_dir.mkdir(parents=True)
        target.config_path.write_text('[board]\nkind = "native"\n'
                                      'prefix = "HOLO"\n'
                                      '[serve]\nname = "holo"\n')
        self.store_path = target.store_path
        with open(os.devnull, "w") as quiet, patch("sys.stdout", quiet):
            self.assertFalse(holophyte.cli.entry.cli(
                ["project", "add", str(self.repo)]))
        self.seed()

    def seed(self):
        conn = store.open(str(self.store_path))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "native:HOLO",
                                                   self.repo)
            failed = self.ticket(conn, project, "HOLO-1")
            store.tickets.transition(conn, failed, "in_flight")
            run = store.claim(conn, project, failed, now=NOW - 30 * MINUTE)
            store.release(conn, run, "failed", reason="verify went red",
                          now=NOW - MINUTE)
            live = self.ticket(conn, project, "HOLO-2")
            store.tickets.transition(conn, live, "in_flight")
            store.claim(conn, project, live)
            parked = self.ticket(conn, project, "HOLO-3")
            store.tickets.transition(conn, parked, "in_flight")
            self.parked_run = store.claim(conn, project, parked)
            park_run(conn, self.parked_run, "awaiting_merge_approval",
                     "merge?", pr_url=URL, candidate_sha="a" * 40)
            store.tickets.transition(conn, parked, "blocked_on_operator")
        finally:
            conn.close()

    def ticket(self, conn, project, key):
        self.assertEqual(store.board.file_ticket(conn, project, "HOLO",
                                                 TICKET), key)
        return conn.execute("SELECT id FROM tickets WHERE linearIdentifier"
                            " = ?", (key,)).fetchone()[0]

    def environment(self, **extra):
        return super().environment(**{"HOLO_PROJECT": "holo", **extra})

    def query(self, sql, *params):
        conn = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def interventions(self):
        return self.query("SELECT id, action, note FROM interventions"
                          " ORDER BY id")

    def status(self, key):
        return self.query("SELECT status FROM tickets"
                          " WHERE linearIdentifier = ?", key)[0][0]

    def send_back_author(self):
        [(payload,)] = self.query(
            "SELECT payload FROM runEvents WHERE runId = ?"
            " AND kind = 'operator_note'", self.parked_run)
        return json.loads(payload)


class ListingTests(WriteCase):
    def test_the_reads_and_five_writes_each_write_signed_and_not_read_only(self):
        async def use(client, _):
            return (await client.list_tools()).tools
        tools = self.session(use)

        self.assertEqual({tool.name for tool in tools}, TOOLS | WRITES)
        for tool in tools:
            if tool.name not in WRITES:
                continue
            with self.subTest(tool=tool.name):
                self.assertLessEqual({"note", "author"},
                                     set(tool.input_schema["required"]))
                self.assertIs(tool.annotations.read_only_hint, False)
                self.assertIs(tool.annotations.destructive_hint, False)


class RequeueTests(WriteCase):
    def test_a_failed_run_is_requeued_with_the_author_in_the_recorded_text(self):
        result = self.call("requeue", {"ticket": "HOLO-1",
                                       "note": "rerun after the fix",
                                       "author": AUTHOR})

        self.assertIs(result.is_error, False, result.content)
        self.assertEqual(self.status("HOLO-1"), "ready")
        newest = self.interventions()[-1]
        self.assertEqual(newest, (result.structured_content["recorded"],
                                  "requeue",
                                  "test seat via MCP: rerun after the fix"))

    def test_a_live_run_is_the_verbs_refusal_and_records_nothing(self):
        before = self.interventions()

        result = self.call("requeue", {"ticket": "HOLO-2", "note": "again",
                                       "author": AUTHOR})
        oracle = self.holo("requeue", "HOLO-2", "again", "--json")

        self.assertEqual(oracle.returncode, 1, oracle.stderr)
        self.assertIs(result.is_error, True)
        self.assertEqual(result.structured_content["detail"],
                         json.loads(oracle.stdout)["detail"])
        self.assertEqual(self.interventions(), before)

    def test_a_blank_note_or_no_author_is_an_error_naming_it_and_runs_nothing(self):
        before = self.dump()

        async def use(client, _):
            return (await client.call_tool("requeue", {
                        "ticket": "HOLO-1", "note": "  ", "author": AUTHOR}),
                    await client.call_tool("requeue", {
                        "ticket": "HOLO-1", "note": "rerun"}))
        blank, unsigned = self.session(use)

        for result, field in ((blank, "note"), (unsigned, "author")):
            with self.subTest(field=field):
                self.assertIs(result.is_error, True)
                self.assertIn(field, result.content[0].text)
        self.assertEqual(self.dump(), before)


class SendBackTests(WriteCase):
    def test_send_back_records_the_note_by_the_author_via_mcp(self):
        result = self.call("send_back", {"run": self.parked_run,
                                         "note": "fix the padding",
                                         "author": AUTHOR})

        self.assertIs(result.is_error, False, result.content)
        self.assertEqual(self.send_back_author(),
                         {"note": "fix the padding", "author": SIGNED})

    def test_babysit_by_the_ticket_key_records_the_same(self):
        result = self.call("babysit", {"ticket": "HOLO-3",
                                       "note": "fix the padding",
                                       "author": AUTHOR})

        self.assertIs(result.is_error, False, result.content)
        self.assertEqual(self.send_back_author(),
                         {"note": "fix the padding", "author": SIGNED})

    def test_babysit_signs_a_note_matching_the_default_text_as_well(self):
        note = holophyte.cli.operator.BABYSIT_DEFAULT_NOTE

        result = self.call("babysit", {"ticket": "HOLO-3", "note": note,
                                       "author": AUTHOR})

        self.assertIs(result.is_error, False, result.content)
        self.assertEqual(self.send_back_author(),
                         {"note": note, "author": SIGNED})

    def test_holo_babysit_author_is_the_send_backs(self):
        done = self.holo("babysit", "HOLO-3", "look again", "--author", AUTHOR)

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.send_back_author(),
                         {"note": "look again", "author": AUTHOR})

    def test_holo_babysit_without_author_is_the_callers_login(self):
        done = self.holo("babysit", "HOLO-3", "look again")

        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.send_back_author(),
                         {"note": "look again", "author": getpass.getuser()})


class HoldTests(WriteCase):
    def test_hold_holds_admission_and_its_row_names_the_author_via_mcp(self):
        result = self.call("hold", {"note": "draining for a deploy",
                                    "author": AUTHOR})

        self.assertIs(result.is_error, False, result.content)
        self.assertEqual(self.query("SELECT admission FROM projects"),
                         [("held",)])
        self.assertEqual(self.interventions()[-1][1:],
                         ("hold", "test seat via MCP: draining for a deploy"))


class FileTicketTests(WriteCase):
    def test_a_valid_body_is_filed_to_backlog_with_the_note_its_first(self):
        before = self.query("SELECT id, boardColumn FROM tickets")

        result = self.call("file_ticket", {"body": TICKET,
                                           "note": "triage this",
                                           "author": AUTHOR})

        self.assertIs(result.is_error, False, result.content)
        [(ticket, column)] = (set(self.query("SELECT id, boardColumn FROM"
                                             " tickets")) - set(before))
        self.assertEqual(column, "backlog")
        notes = self.query("SELECT text FROM ticketNotes WHERE ticketId = ?"
                           " ORDER BY id", ticket)
        self.assertEqual(notes[0], ("test seat via MCP: triage this",))

    def test_an_invalid_body_is_the_checkers_first_problem_and_files_nothing(self):
        body = "# Only a title\n"
        before = self.query("SELECT id FROM tickets")

        result = self.call("file_ticket", {"body": body, "note": "triage",
                                           "author": AUTHOR})

        self.assertIs(result.is_error, True)
        self.assertIn(store.board.ticket_problems(body, self.repo)[0],
                      result.structured_content["detail"])
        self.assertEqual(self.query("SELECT id FROM tickets"), before)


if __name__ == "__main__":
    unittest.main()

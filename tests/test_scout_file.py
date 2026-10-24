"""Filing a done scout's action item to Backlog: `POST
/projects/NAME/actions/scout-file` on a host daemon, `holo scout file`
in-process over the local transport, and the `holo mcp` `scout_file` tool
over stdio, each over a native board in a real git repository registered
in a temporary host registry, the ticket checker the filing gate.

Run: python3 -m unittest discover -s tests -p 'test_scout_file.py' -v
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
import store
import store.board
import store.tickets
from holophyte.config.project import Project
from holophyte.holo.cli import main as holo_main
from store.scouts import create_scout, end_scout, move_scout, record_scout_actions
from tests.host_fixture import HostFixture
from tests.test_holo_mcp import McpCase
from tests.test_serve_board import MACHINE, NATIVE, OWN, ServeBoardCase
from tests.test_serve_ticket_check import body

FILE = "/projects/native/actions/scout-file"
DEPENDS_PROBLEM = "'Depends on' names no ticket of this project: NAT-9"


def without_verify():
    text = body()
    head, _, tail = text.partition("## Verify command(s)")
    rest = tail.split("```", 2)[2]
    return head + rest.lstrip("\n")


def items():
    return [{"title": title, "why": "the scout found it", "kind": kind,
             "body": text}
            for title, kind, text in (
                ("Add export endpoint", "fix", body()),
                ("Unverifiable item", "test", without_verify()),
                ("Unknown dependency", "fix", body(depends="NAT-9")))]


def done_scout(conn, project_id):
    """A done scout whose items 1-3 are a clean body, a body without a
    verify section and a body depending on a ticket that does not exist."""
    scout_id = create_scout(conn, project_id, "Map the merge gate", "recon",
                            30)
    move_scout(conn, scout_id, "planning", base_sha="a" * 40)
    move_scout(conn, scout_id, "swarming")
    record_scout_actions(conn, scout_id, 1, items())
    end_scout(conn, scout_id, "done", "answered")
    return scout_id


def written(conn, project_id, scout_id):
    """The store's tickets, the project's ticket sequence and the scout's
    filed items: `(position, key, filed)` for each item."""
    tickets = conn.execute(
        "SELECT linearIdentifier, boardColumn, body FROM tickets"
        " ORDER BY id").fetchall()
    (seq,) = conn.execute("SELECT ticketSeq FROM projects WHERE id = ?",
                          (project_id,)).fetchone()
    actions = conn.execute(
        "SELECT position, ticketKey, filedAt IS NOT NULL FROM scoutActions"
        " WHERE scoutId = ? ORDER BY position", (scout_id,)).fetchall()
    return tickets, seq, actions


def first_note(conn, key):
    row = conn.execute(
        "SELECT n.text FROM ticketNotes n JOIN tickets t ON t.id = n.ticketId"
        " WHERE t.linearIdentifier = ? ORDER BY n.at, n.id LIMIT 1",
        (key,)).fetchone()
    return None if row is None else row[0]


class ScoutFileCase(ServeBoardCase):
    """`native`, a fresh native board `NAT` with its own token file, whose
    store holds one done scout and no ticket."""

    def setUp(self):
        HostFixture.setUp(self)
        self.paths = {"native": self.repo("native")}
        own = self.root / "own.token"
        own.write_text(OWN + "\n")
        own.chmod(0o600)
        Project.locate(self.paths["native"]).config_path.write_text(
            NATIVE + f'[serve]\ntoken_file = "{own}"\n')
        self.cli("project", "add", str(self.paths["native"]))
        with self.store("native") as (conn, project):
            self.scout = done_scout(conn, project)

    def state(self):
        with self.store("native") as (conn, project):
            return written(conn, project, self.scout)

    def file(self, payload, **kwargs):
        status, answer, _ = self.request("POST", FILE, payload,
                                         revision=None, **kwargs)
        return status, answer


class DaemonScoutFileTests(ScoutFileCase):
    def test_a_clean_item_files_into_backlog_and_records_its_key(self):
        self.start_host()
        status, answer = self.file({"scout": self.scout, "item": 1})
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["ok"], answer["ticket"]), (True, "NAT-1"))
        tickets, _, actions = self.state()
        self.assertEqual(tickets, [("NAT-1", "backlog", body())])
        self.assertEqual(actions[0], (1, "NAT-1", 1))

    def test_a_body_without_verify_answers_the_check_routes_problems(self):
        self.start_host()
        before = self.state()
        status, checked, _ = self.request(
            "POST", "/projects/native/tickets/check",
            {"body": without_verify(), "column": "backlog"}, revision=None)
        self.assertEqual(status, 200)
        self.assertTrue(checked["problems"])
        status, answer = self.file({"scout": self.scout, "item": 2})
        self.assertEqual(status, 200, answer)
        self.assertIs(answer["ok"], False)
        self.assertEqual(answer["problems"], checked["problems"])
        self.assertEqual(self.state(), before)
        self.assertEqual(before[0], [])
        self.assertEqual(before[2][1], (2, None, 0))

    def test_an_unknown_dependency_is_refused_and_nothing_is_written(self):
        self.start_host()
        before = self.state()
        status, answer = self.file({"scout": self.scout, "item": 3})
        self.assertEqual(status, 200, answer)
        self.assertIs(answer["ok"], False)
        self.assertEqual(answer["problems"], [DEPENDS_PROBLEM])
        self.assertEqual(self.state(), before)

    def test_a_filed_item_filed_again_names_its_ticket_and_files_once(self):
        self.start_host()
        self.assertEqual(self.file({"scout": self.scout, "item": 1})[0], 200)
        after = self.state()
        status, answer = self.file({"scout": self.scout, "item": 1})
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["ok"], answer["ticket"]), (False, "NAT-1"))
        self.assertIn("already filed as NAT-1", answer["detail"])
        self.assertEqual(self.state(), after)
        self.assertEqual(len(after[0]), 1)

    def test_an_item_of_a_scout_that_did_not_finish_done_files_nothing(self):
        with self.store("native") as (conn, project):
            failed = create_scout(conn, project, "Map the merge gate",
                                  "recon", 30)
            move_scout(conn, failed, "planning", base_sha="a" * 40)
            record_scout_actions(conn, failed, 1, items()[:1])
            end_scout(conn, failed, "failed", "the lead turn failed")
            before = written(conn, project, failed)
        self.start_host()
        status, answer = self.file({"scout": failed, "item": 1})
        self.assertEqual(status, 200, answer)
        self.assertIs(answer["ok"], False)
        self.assertIn("failed", answer["detail"])
        with self.store("native") as (conn, project):
            self.assertEqual(written(conn, project, failed), before)
        self.assertEqual(before[0], [])

    def test_unknown_ids_bad_input_and_the_own_token_write_nothing(self):
        self.start_host()
        before = self.state()
        self.assertEqual(self.file({"scout": 999, "item": 1}),
                         (404, {"error": "no such scout", "scout": 999,
                                "item": 1, "detail": "no scout 999"}))
        status, answer = self.file({"scout": self.scout, "item": 99})
        self.assertEqual((status, answer["error"]),
                         (404, "no such action item"))
        self.assertEqual(self.file({"scout": str(self.scout), "item": 1})[0],
                         400)
        self.assertEqual(self.file({"scout": self.scout, "item": 1},
                                   token=OWN)[0], 401)
        self.assertEqual(self.state(), before)

    def test_disabled_actions_refuse_the_filing_without_writes(self):
        self.start_host(actions=False)
        before = self.state()
        self.assertEqual(self.file({"scout": self.scout, "item": 1},
                                   token=MACHINE)[0], 404)
        self.assertEqual(self.state(), before)


class HoloScoutFileTests(ScoutFileCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, {"HOLO_TRANSPORT": "local"}))

    def holo(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = holo_main(list(argv))
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), err.getvalue()

    def test_holo_files_the_item_with_the_note_as_its_first_board_note(self):
        code, out, err = self.holo(
            "scout", "file", str(self.scout), "1",
            "from the merge gate recon", "--author", "ko", "--json",
            "-p", str(self.paths["native"]))
        self.assertEqual(code, 0, err)
        answer = json.loads(out)
        self.assertIs(answer["ok"], True)
        self.assertIn("NAT-1", answer["detail"])
        with self.store("native") as (conn, _):
            self.assertEqual(first_note(conn, "NAT-1"),
                             "ko: from the merge gate recon")

    def test_holo_prints_each_problem_of_a_refused_item_and_exits_1(self):
        with self.store("native") as (conn, project):
            problems = store.board.check_ticket(
                conn, project, without_verify(), column="backlog")["problems"]
        self.assertGreater(len(problems), 1)
        before = self.state()
        code, out, err = self.holo("scout", "file", str(self.scout), "2",
                                   "-p", str(self.paths["native"]))
        self.assertEqual(code, 1)
        for problem in problems:
            self.assertIn(problem, out + err)
        self.assertEqual(self.state(), before)


class McpScoutFileTests(McpCase):
    """`holo mcp` over a temporary home registering `native`, a fresh native
    board `NAT` whose store holds one done scout."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.enterContext(patch.dict(os.environ,
                                     {"HOLOPHYTE_HOME": str(self.home)}))
        path = self.root / "native"
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True)
        target.config_path.write_text(NATIVE + '[serve]\nname = "native"\n')
        self.store_path = target.store_path
        with open(os.devnull, "w") as quiet, patch("sys.stdout", quiet):
            self.assertFalse(holophyte.cli.entry.cli(
                ["project", "add", str(path)]))
        conn = store.open(str(self.store_path))
        try:
            project = store.tickets.ensure_project(conn, "native:NAT", path)
            self.scout = done_scout(conn, project)
        finally:
            conn.close()

    def test_the_scout_file_tool_files_the_item_signed_via_mcp(self):
        async def use(client, _):
            return (await client.list_tools()).tools
        (tool,) = [tool for tool in self.session(use)
                   if tool.name == "scout_file"]
        self.assertLessEqual({"scout", "item", "note", "author"},
                             set(tool.input_schema["required"]))

        result = self.call("scout_file", {
            "project": "native", "scout": self.scout, "item": 1,
            "note": "file it", "author": "test seat"})
        self.assertIs(result.is_error, False, result.content)
        self.assertIs(result.structured_content["ok"], True)
        conn = store.open(str(self.store_path))
        try:
            self.assertEqual(first_note(conn, "NAT-1"),
                             "test seat via MCP: file it")
        finally:
            conn.close()

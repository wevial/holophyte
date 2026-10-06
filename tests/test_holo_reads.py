"""`holo runs`, `run N`, `attention`, `board` and `ticket KEY` answer from
the daemon's own view functions over a real store; `--json` is the route's
body and the host form of `attention` is the host daemon's root answer.

Run: python3 -m unittest discover -s tests -p 'test_holo_reads.py' -v
"""
import contextlib
import hashlib
import http.client
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import store
import store.board
import store.tickets
from holophyte.config.project import Project
from holophyte.holo.cli import main
from holophyte.host.registry import Host, settings
from holophyte.serve.serve_host import HostServer, host_tokens
from holophyte.serve.serve_runs import (
    run_detail,
    run_files,
    run_ledger,
    run_turns,
    runs,
)
from holophyte.serve.views import attention, board, ticket_detail
from tests.host_fixture import HostFixture
from tests.phase_fixture import finish_run, park_run
from tests.test_store_board import body as ticket_body

NOW = 1_750_000_000_000
MIN = 60 * 1000
GIT = ("git", "-c", "user.name=test", "-c", "user.email=test@example.com",
       "-c", "commit.gpgsign=false")


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def frozen_now():
    """The views' clock held at NOW, so the command and the view agree."""
    stack = contextlib.ExitStack()
    for module in ("holophyte.serve.views", "holophyte.serve.serve_host"):
        stack.enter_context(patch(f"{module}.time", return_value=NOW / 1000))
    return stack


def holo(*args):
    """`(exit code, stdout, stderr)` of `holo ARGS` run in-process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(args))
        except SystemExit as stopped:
            code = stopped.code
    return code, out.getvalue(), err.getvalue()


def ticket(conn, project, identifier, box=20 * MIN):
    return store.tickets.mirror_ticket(
        conn, project, linear_issue_id=f"issue-{identifier}",
        linear_identifier=identifier, title=f"ticket {identifier}",
        acceptance_criteria=[f"Given {identifier}, then it is worked"],
        verification_commands=["echo ok"], time_box_ms=box)


def block(conn, project, identifier, question):
    blocked = ticket(conn, project, identifier)
    store.tickets.transition(conn, blocked, "in_flight")
    run = store.claim(conn, project, blocked, now=NOW - 10 * MIN)
    store.tickets.transition(conn, blocked, "blocked_on_operator")
    store.set_question(conn, blocked, question)
    park_run(conn, run, "blocked_on_operator", question, now=NOW - MIN)


class StoreReadsCase(unittest.TestCase):
    """A real git repository whose `main` holds a merged task branch, and
    a store with three ended runs (the last merged at that merge) and one
    live run on KO-7 whose heartbeat is an hour old."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        environ = {key: value for key, value in os.environ.items()
                   if key != "HOLO_PROJECT"}
        environ["HOLOPHYTE_HOME"] = str(self.root / "home")
        self.enterContext(patch.dict(os.environ, environ, clear=True))
        self.target = self.root / "repo"
        self.target.mkdir()
        git(self.target, "init", "-q", "-b", "main")
        (self.target / "kept.txt").write_text("one\n")
        git(self.target, "add", ".")
        git(self.target, "commit", "-q", "-m", "base")
        git(self.target, "checkout", "-q", "-b", "task/ko-3")
        (self.target / "added.txt").write_text("a\nb\n")
        git(self.target, "add", ".")
        git(self.target, "commit", "-q", "-m", "task")
        git(self.target, "checkout", "-q", "main")
        git(self.target, "merge", "-q", "--no-ff", "-m", "merge", "task/ko-3")
        self.merge = git(self.target, "rev-parse", "HEAD")
        self.project = Project.locate(self.target, adopt=False)
        self.project.holo_dir.mkdir(parents=True)
        self.seed()

    def seed(self):
        conn = store.open(str(self.project.store_path))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "team-1", self.target)
            self.ended = {}
            plan = (("KO-1", 10 * MIN, "merged", None),
                    ("KO-2", 45 * MIN, "failed", None),
                    ("KO-3", 15 * MIN, "merged", self.merge))
            for n, (identifier, took, outcome, sha) in enumerate(plan):
                worked = ticket(conn, project, identifier)
                store.tickets.transition(conn, worked, "in_flight")
                started = NOW - (10 - n) * 60 * MIN
                run = store.claim(conn, project, worked, now=started)
                if sha is not None:
                    store.set_branch(conn, run, "task/ko-3")
                    store.record_event(
                        conn, run, "agent_turn", "implement turn ended",
                        level="detail", payload=json.dumps(
                            {"role": "implement", "route": "codex",
                             "label": "first", "seconds": 12}))
                    store.record_intervention(
                        conn, run, "redirect", "asked the operator",
                        source="supervisor", trigger="off_criteria",
                        question="which branch?", now=started + MIN)
                finish_run(conn, run, outcome, now=started + took,
                           merge_sha=sha)
                self.ended[identifier] = run
            live = ticket(conn, project, "KO-7")
            store.tickets.transition(conn, live, "in_flight")
            self.live = store.claim(conn, project, live, now=NOW - 2 * 60 * MIN)
            store.set_phase(conn, self.live, "working", now=NOW - 2 * 60 * MIN)
            store.heartbeat(conn, self.live, now=NOW - 60 * MIN)
            block(conn, project, "KO-8", "which API?")
        finally:
            conn.close()

    def read(self, *args):
        """`holo ARGS -p TARGET`, which must exit 0; its stdout."""
        code, out, err = holo(*args, "-p", str(self.target))
        self.assertEqual(code, 0, err)
        return out

    def store_bytes(self):
        """A missing WAL is read as empty: a reader opening the store
        creates an empty one, and holds no frame in it."""
        path = self.project.store_path
        return {suffix: hashlib.sha256(
                    Path(f"{path}{suffix}").read_bytes()
                    if Path(f"{path}{suffix}").exists() else b"").hexdigest()
                for suffix in ("", "-wal")}


class RunReadsTests(StoreReadsCase):
    def test_runs_json_is_the_runs_view_and_limit_two_keeps_two(self):
        out = self.read("runs", "--json")
        _, view = runs(self.project)
        self.assertEqual(out, json.dumps(view) + "\n")
        self.assertEqual([row["ticket"] for row in json.loads(out)["rows"]],
                         ["KO-1", "KO-2", "KO-3"])
        limited = json.loads(self.read("runs", "--limit", "2", "--json"))
        self.assertEqual(limited, runs(self.project, "limit=2")[1])
        self.assertEqual(len(limited["rows"]), 2)

    def test_run_json_and_its_files_ledger_and_turns_are_their_views(self):
        run = str(self.ended["KO-3"])
        for flags, view in (((), run_detail), (("--files",), run_files),
                            (("--ledger",), run_ledger),
                            (("--turns",), run_turns)):
            with self.subTest(flags=flags):
                out = self.read("run", run, *flags, "--json")
                code, body = view(self.project, run)
                self.assertEqual((code, out), (200, json.dumps(body) + "\n"))
        self.assertEqual(self.read("run", "show", run, "--json"),
                         json.dumps(run_detail(self.project, run)[1]) + "\n")
        files = json.loads(self.read("run", run, "--files", "--json"))
        self.assertEqual((files["files"], files["head"]),
                         ([{"path": "added.txt", "status": "A", "added": 2,
                            "deleted": 0}], self.merge))
        ledger = json.loads(self.read("run", run, "--ledger", "--json"))
        self.assertIn("asked the operator",
                      " ".join(entry["text"] for entry in ledger["entries"]))
        turns = json.loads(self.read("run", run, "--turns", "--json"))
        self.assertEqual([(t["role"], t["label"]) for t in turns["turns"]],
                         [("implement", "first")])

    def test_an_unknown_run_exits_one_with_the_404_error(self):
        code, out, err = holo("run", "9999", "-p", str(self.target))
        self.assertEqual(run_detail(self.project, "9999")[0], 404)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no such run", err)

    def test_runs_without_json_is_one_line_per_ended_run(self):
        lines = self.read("runs").splitlines()
        self.assertEqual(len(lines), 3)
        for line, (identifier, outcome) in zip(
                lines, (("KO-1", "merged"), ("KO-2", "failed"),
                        ("KO-3", "merged"))):
            self.assertIn(identifier, line)
            self.assertIn(outcome, line)

    def test_every_read_leaves_the_store_bytes_unchanged(self):
        before = self.store_bytes()
        run = str(self.ended["KO-3"])
        for args in (("runs",), ("runs", "--json"), ("run", run),
                     ("run", run, "--files"), ("run", run, "--ledger"),
                     ("run", run, "--turns"), ("attention",), ("board",),
                     ("ticket", "KO-7")):
            with self.subTest(args=args):
                self.read(*args)
                self.assertEqual(self.store_bytes(), before)


class AttentionReadTests(StoreReadsCase):
    def test_attention_json_is_the_view_with_the_blocked_and_stale_items(self):
        with frozen_now():
            out = self.read("attention", "--json")
        _, view = attention(self.project, now=NOW)
        self.assertEqual(out, json.dumps(view) + "\n")
        items = {item["kind"]: item for item in json.loads(out)["items"]}
        self.assertEqual((items["blocked"]["ticket"],
                          items["blocked"]["question"]), ("KO-8", "which API?"))
        self.assertEqual((items["stale_run"]["ticket"],
                          items["stale_run"]["run"]), ("KO-7", self.live))

    def test_runs_against_a_project_with_no_store_exits_one_naming_it(self):
        bare = self.root / "bare"
        bare.mkdir()
        code, out, err = holo("runs", "-p", str(bare))
        self.assertEqual(runs(Project.locate(bare, adopt=False))[0], 503)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no store", err)
        self.assertIn(str(bare), err)


class HostAttentionReadTests(HostFixture):
    def test_attention_with_no_project_is_the_host_daemons_root_answer(self):
        self.enterContext(patch.dict(os.environ, {"HOLO_PROJECT": ""}))
        paths = {name: self.repo(name, name=name) for name in ("alpha", "beta")}
        for path in paths.values():
            self.cli("project", "add", str(path))
        conn = store.open(str(Project.locate(paths["beta"]).store_path))
        try:
            project = store.tickets.ensure_project(conn, "team-beta",
                                                   paths["beta"])
            block(conn, project, "KO-8", "which API?")
        finally:
            conn.close()
        host = Host.locate()
        knobs = settings(host)
        read, write = host_tokens(host, knobs, "127.0.0.1", host.projects())
        server = HostServer(host, knobs, ("127.0.0.1", 0),
                            console_dir=self.root / "no-console",
                            read_token=read, write_token=write)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        with frozen_now():
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=10)
            try:
                connection.request("GET", "/attention")
                answer = connection.getresponse().read().decode()
            finally:
                connection.close()
            code, out, err = holo("attention", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, answer + "\n")
        (blocked,) = [item for item in json.loads(out)["items"]
                      if item["kind"] == "blocked"]
        self.assertEqual((blocked["project"], blocked["ticket"]),
                         ("beta", "KO-8"))


class BoardReadTests(HostFixture):
    def test_board_and_ticket_json_are_their_views(self):
        path = self.repo("native")
        target = Project.locate(path, adopt=False)
        target.config_path.write_text('[board]\nkind = "native"\nkey = "NAT"\n')
        conn = store.open(str(target.store_path))
        try:
            store.init(conn)
            project = store.tickets.ensure_project(conn, "native:NAT", path)
            ready = store.board.file_ticket(conn, project, "NAT",
                                            ticket_body("Ready one"))
            waiting = store.board.file_ticket(conn, project, "NAT",
                                              ticket_body("Backlog one"),
                                              column="backlog")
        finally:
            conn.close()
        with frozen_now():
            code, out, err = holo("board", "--json", "-p", str(path))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, json.dumps(board(target, now=NOW)[1]) + "\n")
        held = {column["state"]: [entry["ticket"] for entry in column["tickets"]]
                for column in json.loads(out)["columns"] if column["tickets"]}
        self.assertEqual(held, {"backlog": [waiting], "ready": [ready]})
        code, out, err = holo("ticket", ready, "--json", "-p", str(path))
        self.assertEqual(code, 0, err)
        self.assertEqual(out, json.dumps(ticket_detail(target, ready)[1]) + "\n")
        self.assertEqual(json.loads(out)["title"], "Ready one")


if __name__ == "__main__":
    unittest.main()

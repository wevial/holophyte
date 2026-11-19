"""`POST /actions/revert`: `holo revert`'s checks, dependents and filing
behind the daemon's write token, against a real git repository whose
`origin/main` holds the merges and a native board's store.

Run: python3 -m unittest discover -s tests -p 'test_serve_revert*.py' -v
"""
import contextlib
import http.client
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from phase_fixture import finish_run  # noqa: E402 - after the sys.path insert

import holophyte.cli.entry  # noqa: E402
import holophyte.config.project  # noqa: E402
import holophyte.revert.command  # noqa: E402
import holophyte.serve.server  # noqa: E402
import store  # noqa: E402
import store.tickets  # noqa: E402
from holophyte.board.provider import board_for  # noqa: E402
from holophyte.loop.runs import open_store  # noqa: E402
from tests.test_cli_native_update import body  # noqa: E402

TOKEN = "revert-action-token"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
REASON = "broke the header"


class RevertActionTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Tester", "-c", "user.email=t@example.org",
             *args], cwd=self.target, capture_output=True, text=True,
            check=True).stdout.strip()

    def commit(self, files, message):
        for path, text in files.items():
            (self.target / path).parent.mkdir(parents=True, exist_ok=True)
            (self.target / path).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def serve(self, key_files, later_files):
        """NAT-1's merge changed `key_files`, NAT-2's after it `later_files`,
        both pushed to origin; a daemon with actions behind `TOKEN`."""
        self.locate()
        token = self.root / "serve.token"
        token.write_text(TOKEN + "\n")
        token.chmod(0o600)
        self.write_config('[board]\nkind = "native"\nprefix = "NAT"\n'
                          "[revert]\nenabled = true\n"
                          f'[serve]\ntoken_file = "{token}"\nactions = true\n')
        self.git("init", "-q", "-b", "main")
        self.commit({"tests/test_thing.py": "", "src/header.py": "header\n",
                     "README.md": "readme\n"}, "base")
        for title in ("Header", "Footer"):
            path = self.root / f"{title}.md"
            path.write_text(body(title))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(holophyte.cli.entry.cli(
                    [str(self.target), "--file-ticket", str(path)]), 0)
        key_sha = self.commit(key_files, "NAT-1: header")
        self.later_sha = self.commit(later_files, "NAT-2: footer")
        self.git("init", "-q", "--bare", str(self.root / "origin.git"))
        self.git("remote", "add", "origin", str(self.root / "origin.git"))
        self.git("push", "-q", "origin", "main")
        self.runs = {}
        with contextlib.closing(open_store(self.project)) as conn:
            (project_id,) = conn.execute("SELECT id FROM projects").fetchone()
            for key, sha in (("NAT-1", key_sha), ("NAT-2", self.later_sha)):
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIdentifier = ?",
                    (key,)).fetchone()
                run = store.claim(conn, project_id, ticket_id)
                store.tickets.transition(conn, ticket_id, "in_flight")
                store.set_phase(conn, run, "working")
                finish_run(conn, run, "merged", merge_sha=sha)
                self.runs[key] = run
        project = holophyte.config.project.Project.locate(self.target)
        server = holophyte.serve.server.make_server(
            project, "127.0.0.1", 0, console_dir=self.root / "dist",
            token=holophyte.serve.server.resolve_token(project, "127.0.0.1"))
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def post(self, payload, headers=BEARER):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("POST", "/actions/revert",
                         body=json.dumps(payload).encode(), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def read(self, sql, *params):
        with contextlib.closing(sqlite3.connect(self.project.store_path)) as conn:
            return conn.execute(sql, params).fetchall()

    def tickets(self):
        return self.read("SELECT linearIdentifier FROM tickets ORDER BY id")

    def reverts(self):
        return self.read("SELECT runId, actor, note FROM interventions"
                         " WHERE action = 'revert'")

    def test_preview_lists_the_dependent_then_filing_puts_the_ticket_on_the_board(self):
        self.serve({"src/header.py": "broken\n"},
                   {"src/header.py": "fixed\n", "README.md": "later\n"})

        code, reply = self.post({"key": "NAT-1", "reason": REASON,
                                 "dry_run": True})

        self.assertEqual(code, 200, reply)
        self.assertIs(reply["enabled"], True)
        self.assertIsNone(reply["refused"])
        self.assertEqual(reply["dependents"], [
            {"key": "NAT-2", "sha": self.later_sha, "paths": ["src/header.py"]}])
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",)])
        self.assertEqual(self.reverts(), [])

        code, reply = self.post({"key": "NAT-1", "reason": REASON})

        self.assertEqual(code, 200, reply)
        self.assertEqual(reply["key"], "NAT-3")
        state = board_for(self.project).states(["NAT-3"])["NAT-3"]
        self.assertEqual((state["state"], state["column"]), ("open", "ready"))
        ((title,),) = self.read("SELECT title FROM tickets"
                                " WHERE linearIdentifier = 'NAT-3'")
        self.assertEqual(title, "[Revert] Undo NAT-1: Header")
        self.assertEqual(self.reverts(), [
            (self.runs["NAT-1"], "maintainer", f"filed NAT-3: {REASON}")])

    def test_a_merge_on_a_refused_path_is_409_naming_it_and_files_nothing(self):
        self.serve({"db/migrations/0002.sql": "drop table t;\n",
                    "src/header.py": "broken\n"}, {"README.md": "later\n"})

        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                code, reply = self.post({"key": "NAT-1", "reason": REASON,
                                         "dry_run": dry_run})

                self.assertEqual(code, 409, reply)
                self.assertEqual(reply["paths"], ["db/migrations/0002.sql"])
                self.assertIn("db/migrations/0002.sql", reply["error"])
                self.assertEqual(reply["refused"]["paths"],
                                 ["db/migrations/0002.sql"])
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",)])
        self.assertEqual(self.reverts(), [])

    def test_a_failed_filing_answers_500_without_the_origin_credentials(self):
        self.serve({"src/header.py": "broken\n"}, {"README.md": "later\n"})

        def rejected(target, path, state, board, out, priority):
            print("[holo2] fetch https://bot:hunter2@git.example.test/r.git"
                  " and 'https://git.example.test/q.git?access_token=hun'ter3'"
                  " and https://git.example.test?opaque=left@hunter4"
                  " failed; ticket rejected", file=out)
            return 2

        with patch.object(holophyte.revert.command, "file_ticket", rejected):
            code, reply = self.post({"key": "NAT-1", "reason": REASON})

        self.assertEqual(code, 500, reply)
        self.assertNotIn("hunter2", json.dumps(reply))
        self.assertNotIn("ter3", json.dumps(reply))
        self.assertNotIn("hunter4", json.dumps(reply))
        self.assertIn("https://[redacted]@git.example.test/r.git",
                      reply["error"])
        self.assertIn("https://git.example.test/q.git?[redacted]",
                      reply["error"])
        self.assertIn("https://git.example.test?[redacted]", reply["error"])
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",)])

    def test_reverts_disabled_after_the_daemon_started_are_refused(self):
        self.serve({"src/header.py": "broken\n"}, {"README.md": "later\n"})
        config = self.project.config_path.read_text()
        self.project.config_path.write_text(
            config.replace("enabled = true", "enabled = false"))

        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                code, reply = self.post({"key": "NAT-1", "reason": REASON,
                                         "dry_run": dry_run})

                self.assertEqual(code, 409, reply)
                self.assertIs(reply["enabled"], False)
                self.assertIn("[revert] enabled is not true",
                              reply["refused"]["reason"])
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",)])

    def test_a_path_refused_while_the_request_waits_on_the_lock_is_refused(self):
        self.serve({"src/header.py": "broken\n"}, {"README.md": "later\n"})
        replies = []
        sender = threading.Thread(target=lambda: replies.append(
            self.post({"key": "NAT-1", "reason": REASON})))

        with holophyte.revert.command.revert_turn(self.project):
            sender.start()
            time.sleep(0.5)
            config = self.project.config_path.read_text()
            self.project.config_path.write_text(config.replace(
                "enabled = true",
                'enabled = true\nrefuse_paths = ["src/header.py"]'))
        sender.join()

        ((code, reply),) = replies
        self.assertEqual(code, 409, reply)
        self.assertEqual(reply["paths"], ["src/header.py"])
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",)])

    def test_two_requests_at_once_file_one_revert(self):
        self.serve({"src/header.py": "broken\n"}, {"README.md": "later\n"})
        real = holophyte.revert.command.file_ticket

        def slow(*args, **kwargs):
            time.sleep(0.5)
            return real(*args, **kwargs)

        codes = []
        def send():
            codes.append(self.post({"key": "NAT-1", "reason": REASON})[0])

        with patch.object(holophyte.revert.command, "file_ticket", slow):
            senders = [threading.Thread(target=send) for _ in range(2)]
            for sender in senders:
                sender.start()
            for sender in senders:
                sender.join()

        self.assertEqual(sorted(codes), [200, 409])
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",), ("NAT-3",)])

    def test_an_empty_reason_is_400_and_files_nothing(self):
        self.serve({"src/header.py": "broken\n"}, {"README.md": "later\n"})

        for reason in ("", "   "):
            with self.subTest(reason=reason):
                code, reply = self.post({"key": "NAT-1", "reason": reason})
                self.assertEqual(code, 400, reply)
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",)])

    def test_without_the_write_token_the_action_is_401_and_files_nothing(self):
        self.serve({"src/header.py": "broken\n"}, {"README.md": "later\n"})

        for headers in ({}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                code, reply = self.post({"key": "NAT-1", "reason": REASON},
                                        headers=headers)
                self.assertEqual((code, reply), (401, {}))
        self.assertEqual(self.tickets(), [("NAT-1",), ("NAT-2",)])
        self.assertEqual(self.reverts(), [])

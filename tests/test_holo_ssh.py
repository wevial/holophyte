"""`holo` with a client config naming a host runs the same command there over
ssh. The fake `ssh` stands in for the network hop alone: it records the argv
`holo` built and runs the command line under `sh -c` with the host's home, as
sshd hands it to the remote user's shell; the remote `holo` and its store are
real.

Run: python3 -m unittest discover -s tests -p 'test_holo_ssh.py' -v
"""
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
import store
import store.tickets
from holophyte.config.project import Project
from tests.test_holo_grammar import T0
from tests.test_provider import ticket_body

ROOT = Path(__file__).resolve().parent.parent
NOTE = 'it\'s "done"; touch pwned'
REFUSED = "ssh: connect to host writer port 22: Connection refused"

FAKE_SSH = '''#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
with open({record!r}, "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if os.environ.get("FAKE_SSH_NESTED"):
    sys.exit(255)
while args[0] == "-o":
    args = args[2:]
sys.exit(subprocess.run(["sh", "-c", " ".join(args[1:])], cwd={cwd!r},
                        env={env!r}).returncode)
'''

UNREACHABLE_SSH = '''#!{python}
import json, sys
with open({record!r}, "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
print({message!r}, file=sys.stderr)
sys.exit(255)
'''


class SshTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.seat, self.host = self.root / "seat", self.root / "host"
        self.desk, self.login, self.bin = (self.root / "desk", self.root / "login",
                                           self.root / "bin")
        for path in (self.seat, self.desk, self.login, self.bin):
            path.mkdir()
        self.record = self.root / "ssh.jsonl"
        patcher = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.host)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.alpha = self.register("alpha")
        self.conn = store.open(str(Project.locate(self.alpha).store_path))
        self.addCleanup(self.conn.close)
        self.project_id = store.tickets.ensure_project(
            self.conn, "native:HOLO", self.alpha)
        self.fake(FAKE_SSH.format(
            python=sys.executable, record=str(self.record), cwd=str(self.login),
            env={"PATH": f"{self.bin}:{os.environ['PATH']}",
                 "HOME": str(self.login), "HOLOPHYTE_HOME": str(self.host),
                 "PYTHONPATH": str(ROOT), "FAKE_SSH_NESTED": "1",
                 "GIT_CEILING_DIRECTORIES": str(self.root)}))
        holo = self.bin / "holo"
        holo.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)}'
                        ' -m holophyte.holo "$@"\n')
        holo.chmod(0o755)
        (self.seat / "client.toml").write_text('host = "writer"\n')

    def register(self, name):
        path = self.root / name
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text(
            f'[board]\nkind = "native"\nprefix = "HOLO"\n[serve]\nname = "{name}"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(holophyte.cli.entry.cli(["project", "add", str(path)]))
        return path

    def fake(self, text):
        ssh = self.bin / "ssh"
        ssh.write_text(text)
        ssh.chmod(0o755)

    def holo(self, *args, home=None, cwd=None, **env):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("HOLO_PROJECT", "HOLO_TRANSPORT")}
        environment.update(PATH=f"{self.bin}:{os.environ['PATH']}",
                           HOLOPHYTE_HOME=str(home or self.seat),
                           PYTHONPATH=str(ROOT),
                           GIT_CEILING_DIRECTORIES=str(self.root), **env)
        return subprocess.run([sys.executable, "-m", "holophyte.holo", *args],
                              cwd=cwd or self.desk, capture_output=True,
                              text=True, env=environment)

    def host_status(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "factory.py"), str(self.alpha),
             "--status", "--json"], cwd=self.login, capture_output=True,
            text=True, env={**os.environ, "HOLOPHYTE_HOME": str(self.host),
                            "PYTHONPATH": str(ROOT)})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def calls(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def claim(self):
        self.ticket = store.tickets.mirror_ticket(
            self.conn, self.project_id, linear_issue_id="issue-1",
            linear_identifier="HOLO-1", title="a ticket",
            acceptance_criteria=["Given a ticket, then it is worked"],
            verification_commands=["echo ok"], time_box_ms=25 * 60_000)
        store.tickets.transition(self.conn, self.ticket, "in_flight")
        return store.claim(self.conn, self.project_id, self.ticket, now=T0)


class RemoteCommandTests(SshTests):
    def test_status_over_ssh_is_the_hosts_status_json_with_its_transport(self):
        completed = self.holo("status", "--json", "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout),
                         {**self.host_status(), "transport": "ssh"})
        self.assertIn("via ssh to writer", completed.stderr)
        (call,) = self.calls()
        self.assertEqual(call[:3], ["-o", "BatchMode=yes", "writer"])

    def test_a_note_with_quotes_and_a_semicolon_reaches_the_host_store_verbatim(self):
        run = self.claim()
        store.release(self.conn, run, "failed", "verify failed", now=T0 + 1)
        completed = self.holo("requeue", "HOLO-1", NOTE, "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (row_id,), = self.conn.execute(
            "SELECT id FROM interventions WHERE action = 'requeue'")
        (text,), = self.conn.execute(
            "SELECT text FROM ledger WHERE kind = 'intervention'")
        self.assertEqual(text, f"human requeue: {NOTE}")
        self.assertEqual(list(self.root.rglob("pwned")), [])
        (line,) = completed.stdout.splitlines()
        self.assertTrue(line.startswith("✓ HOLO-1"), line)
        self.assertIn(f"intervention {row_id} recorded", line)

    def test_the_remote_side_runs_locally_whatever_its_own_client_config_says(self):
        (self.host / "client.toml").write_text('host = "writer"\n')
        completed = self.holo("status", "--json", "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_a_positional_beginning_with_a_dash_reaches_the_host_as_a_positional(self):
        store.open(str(self.login / "-snapshot.db")).close()
        completed = self.holo("store", "import", "--dry-run", "-p", "alpha",
                              "--", "-snapshot.db")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        oracle = subprocess.run(
            [sys.executable, str(ROOT / "factory.py"), str(self.alpha),
             "--import-store=-snapshot.db", "--dry-run"], cwd=self.login,
            capture_output=True, text=True,
            env={**os.environ, "HOLOPHYTE_HOME": str(self.host),
                 "PYTHONPATH": str(ROOT)})
        self.assertEqual(oracle.returncode, 0, oracle.stderr)
        self.assertEqual(completed.stdout, oracle.stdout)

    def test_a_remote_refusal_exits_one_and_a_host_usage_error_exits_two(self):
        self.claim()
        refused = self.holo("requeue", "HOLO-1", "rerun", "-p", "alpha")
        with self.assertRaises(store.RequeueRefused) as expected:
            store.requeue(self.conn, self.ticket, "rerun")
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn(str(expected.exception), refused.stderr)
        unknown = self.holo("requeue", "HOLO-1", "rerun", "-p", "gamma")
        self.assertEqual(unknown.returncode, 2, unknown.stderr)
        self.assertIn("'gamma'", unknown.stderr)

    def test_ssh_failing_to_connect_exits_one_naming_the_host_and_its_message(self):
        self.fake(UNREACHABLE_SSH.format(python=sys.executable,
                                         record=str(self.record),
                                         message=REFUSED))
        for command in ("status", "sweep"):
            with self.subTest(command=command):
                completed = self.holo(command, "-p", "alpha")
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertEqual(completed.stderr.splitlines()[-1],
                                 f"[holo2] ssh to writer failed: {REFUSED}")
        written = self.holo("hold", "maintenance", "-p", "alpha", "--json")
        self.assertEqual(written.returncode, 1, written.stderr)
        result = json.loads(written.stdout)
        self.assertEqual((result["ok"], result["transport"]), (False, "ssh"))
        self.assertIn(REFUSED, result["detail"])


class FileOverSshTests(SshTests):
    def test_a_ticket_file_on_the_client_is_filed_on_the_host_through_stdin(self):
        (self.alpha / "tests").mkdir()
        (self.alpha / "tests" / "test_thing.py").write_text("")
        body = ticket_body(
            verify="python3 -m unittest discover -s tests -p 'test_thing.py'")
        (self.desk / "TICKET.md").write_text(body)
        completed = self.holo("file", "TICKET.md", "-p", "alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (stored,) = self.conn.execute(
            "SELECT body FROM tickets WHERE linearIdentifier = 'HOLO-1'")
        self.assertEqual(stored[0], body)
        (call,) = self.calls()
        self.assertEqual(shlex.split(call[-1])[-2:], ["--", "-"])

    def test_a_ticket_file_missing_on_the_client_exits_two_without_ssh(self):
        completed = self.holo("file", "MISSING.md", "-p", "alpha")
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("MISSING.md", completed.stderr)
        self.assertEqual(self.calls(), [])


class ChoiceTests(SshTests):
    def test_serve_and_supervise_are_refused_before_ssh(self):
        for words in (["serve"], ["supervise", "--once"]):
            with self.subTest(words=words):
                completed = self.holo(*words, "-p", "alpha")
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("runs on the host itself", completed.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_client_config_with_no_host_runs_locally_and_adds_no_key(self):
        (self.host / "client.toml").write_text('remote_command = "holo"\n')
        completed = self.holo("status", "--json", "-p", "alpha", home=self.host)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), self.host_status())
        self.assertNotIn("via", completed.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_project_goes_by_name_from_the_environment_never_a_client_path(self):
        work_tree = self.root / "checkout"
        subprocess.run(["git", "init", "-q", str(work_tree)], check=True)
        completed = self.holo("status", "--json", cwd=work_tree,
                              HOLO_PROJECT="alpha")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (call,) = self.calls()
        remote = shlex.split(call[-1])
        self.assertEqual(remote[remote.index("-p"):remote.index("-p") + 2],
                         ["-p", "alpha"])
        self.assertNotIn(str(work_tree), call[-1])
        self.assertEqual(json.loads(completed.stdout)["target"], str(self.alpha))


if __name__ == "__main__":
    unittest.main()

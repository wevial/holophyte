"""The host registry, `HOLOPHYTE_HOME/host.toml`, and the host form of
`--status` (consolidation stage 0).

Real git repositories, real stores and the real registry file under a
temporary home; the one concurrent writer is a second holder of the
registry's temporary file, driven by hand so the interleaving is certain.

Run: python3 -m unittest discover -s tests -p 'test_host*' -v
"""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import store
from holophyte.host import Host, HostError
from holophyte.project import Project
from holophyte.supervisor_lock import (
    acquire_supervisor_lock,
    release_supervisor_lock,
    supervisor_lock_path,
)

ROOT = Path(__file__).resolve().parent.parent


class HostFixture(unittest.TestCase):
    """A temporary home, repositories under it, the CLI in-process."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        patcher = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def repo(self, directory, team=None, name=None):
        """A git repository at `root/directory` with a `[board]` config,
        and `[serve] name` when `name` is given."""
        path = self.root / directory
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        text = (f'[board]\nteam = "{team or "team-" + directory}"\n'
                f'project_id = "p-{directory}"\n')
        if name is not None:
            text += f'[serve]\nname = "{name}"\n'
        target.config_path.write_text(text)
        return path

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = holophyte.cli.cli(list(args))
        return code, out.getvalue()

    def registered(self):
        return [(entry.name, entry.path) for entry in Host.locate().projects()]

    def interventions(self, path, action):
        conn = store.open(str(path))
        try:
            return conn.execute("SELECT count(*) FROM interventions"
                                " WHERE action = ?", (action,)).fetchone()[0]
        finally:
            conn.close()



class HostRegistryTests(HostFixture):
    def test_a_registered_path_or_name_is_refused_naming_the_entry(self):
        alpha = self.repo("alpha")
        self.cli("project", "add", str(alpha))
        with self.assertRaisesRegex(
                SystemExit, f"alpha {alpha} is already registered in"):
            self.cli("project", "add", str(alpha))
        # Another repository whose config gives the same route name is
        # refused before its store is written.
        other = self.repo("other", name="alpha")
        with self.assertRaisesRegex(
                SystemExit, f"alpha {alpha} is already registered in"):
            self.cli("project", "add", str(other))
        self.assertFalse(Project.locate(other).store_path.exists())
        self.assertEqual(self.registered(), [("alpha", alpha)])
        # A name edited into a collision after registration is refused at
        # the next start, naming both paths.
        beta = self.repo("beta")
        self.cli("project", "add", str(beta))
        Project.locate(beta).config_path.write_text(
            '[board]\nteam = "team-beta"\nproject_id = "p"\n'
            '[serve]\nname = "alpha"\n')
        with self.assertRaisesRegex(HostError, f"{beta} and {alpha} are both"
                                    " registered as name alpha"):
            Host.locate().projects()

    def test_adoption_records_register_project_once(self):
        alpha = self.repo("alpha")
        target = Project.locate(alpha)
        target.store_path.parent.mkdir(parents=True, exist_ok=True)
        conn = store.open(str(target.store_path))
        # The row a loop writes at startup before anyone registered it.
        store.ensure_project(conn, "team-alpha", alpha)
        conn.close()
        self.cli("project", "add", str(alpha))
        self.assertEqual(
            self.interventions(target.store_path, "register_project"), 1)
        _, out = self.cli("project", "remove", "alpha")
        self.assertIn("store is untouched", out)
        self.assertEqual(self.registered(), [])
        self.cli("project", "add", str(alpha))
        self.assertEqual(
            self.interventions(target.store_path, "register_project"), 1)
        self.assertEqual(self.registered(), [("alpha", alpha)])
        # The same team at another path is refused naming the row, and the
        # registry is left as it was.
        moved = self.repo("moved", team="team-alpha")
        with self.assertRaisesRegex(
                SystemExit, f"project 1 already registered: {alpha}"):
            self.cli("project", "add", str(moved),
                     "--store", str(target.store_path))
        self.assertEqual(self.registered(), [("alpha", alpha)])

    def test_two_adds_through_the_exclusive_temp_file_keep_both(self):
        alpha, beta = self.repo("alpha"), self.repo("beta")
        self.home.mkdir(parents=True, exist_ok=True)
        registry = self.home / "host.toml"
        temporary = self.home / "host.toml.tmp"
        # A rival writer holds the temporary file first.
        rival = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        failed = []

        def add_beta():
            try:
                self.cli("project", "add", str(beta))
            except BaseException as error:  # reported on the main thread
                failed.append(error)
        thread = threading.Thread(target=add_beta)
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "the second add did not wait")
        os.write(rival, f'[[project]]\npath = "{alpha}"\n'.encode())
        os.close(rival)
        os.replace(temporary, registry)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failed, [])
        self.assertEqual(self.registered(), [("alpha", alpha), ("beta", beta)])
        self.assertFalse(temporary.exists())

    def test_a_missing_path_is_refused_and_registers_nothing(self):
        with self.assertRaisesRegex(SystemExit, "not a repository root"):
            self.cli("project", "add", str(self.root / "missing"))
        self.assertFalse((self.home / "host.toml").exists())

    def test_a_registry_edit_is_seen_without_a_new_host(self):
        host = Host.locate()
        self.assertEqual(host.projects(), ())
        alpha = self.repo("alpha")
        self.cli("project", "add", str(alpha))
        self.assertEqual([entry.name for entry in host.projects()], ["alpha"])


class HostStatusTests(HostFixture):
    """The stage's checkpoint: `factory.py --status` on a temporary home with
    two seeded stores prints both, the builds and the locks."""

    def test_host_status_prints_every_project_its_build_and_locks(self):
        alpha, beta = self.repo("alpha"), self.repo("beta")
        self.cli("project", "add", str(alpha))
        self.cli("project", "add", str(beta))
        lock = supervisor_lock_path(Project.locate(alpha))
        acquire_supervisor_lock(lock, alpha)
        self.addCleanup(release_supervisor_lock, lock)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
        code, out = self.cli("--status")
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertIn(f"build head {head}, sweep none", lines)
        self.assertIn("sweep: none", lines)
        self.assertIn("home lock: free", lines)
        self.assertIn(f"[alpha] project {alpha} enabled", lines)
        self.assertIn(f"[alpha] supervisor lock: held, pid {os.getpid()}", lines)
        self.assertIn(f"[beta] project {beta} enabled", lines)
        self.assertIn("[beta] supervisor lock: free", lines)
        self.assertIn("[beta] merge lock: free", lines)
        # A registered project whose store is gone is its own error line;
        # the others stay whole and the exit says the answer is partial.
        gamma = self.repo("gamma")
        self.cli("project", "add", str(gamma))
        Project.locate(gamma).store_path.unlink()
        code, out = self.cli("--status", "--json")
        self.assertEqual(code, 1)
        projects = {p["name"]: p for p in json.loads(out)["projects"]}
        self.assertEqual(projects["alpha"]["store"]["supervisor_lock"],
                         {"pid": os.getpid(), "stale": False})
        self.assertIsNone(projects["beta"]["error"])
        self.assertIn("no store at", projects["gamma"]["error"])

    def test_only_status_has_a_host_form(self):
        for argv in ([], ["--serve", "7719"], ["--supervise"]):
            with self.subTest(argv=argv), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli(argv)
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

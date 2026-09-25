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
import threading
import time
import unittest
from pathlib import Path

import holophyte.cli
import store
from holophyte.gates import merge_lock_path
from holophyte.host import Host, HostError
from holophyte.project import Project
from holophyte.supervisor_lock import (
    acquire_supervisor_lock,
    release_supervisor_lock,
    supervisor_lock_path,
)
from tests.host_fixture import HostFixture

ROOT = Path(__file__).resolve().parent.parent


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

    def test_add_against_another_store_leaves_the_registry_alone(self):
        # The registry records a path and the host reads that project's own
        # store; a registration written elsewhere is one it could never find.
        alpha = self.repo("alpha")
        other = self.root / "other.db"
        _, out = self.cli("project", "add", str(alpha), "--store", str(other))
        self.assertIn(f"registered in {other} only", out)
        self.assertEqual(self.registered(), [])
        self.assertEqual(self.interventions(other, "register_project"), 1)
        self.assertFalse(Project.locate(alpha).store_path.exists())
        # Named explicitly, the project's own store is the default one.
        self.cli("project", "add", str(alpha),
                 "--store", str(Project.locate(alpha).store_path))
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

    def test_remove_by_path_drops_an_entry_whose_config_gives_no_name(self):
        # The recovery the operator needs most: a config that no longer
        # loads leaves its entry nameless, so only its path can name it.
        alpha, beta = self.repo("alpha"), self.repo("beta")
        self.cli("project", "add", str(alpha))
        self.cli("project", "add", str(beta))
        Project.locate(beta).config_path.write_bytes(b"\xff\xfe[board]\n")
        self.assertEqual(self.registered(), [("alpha", alpha), (None, beta)])
        _, out = self.cli("project", "remove", str(beta))
        self.assertIn(f"{beta} removed from the host registry", out)
        self.assertEqual(self.registered(), [("alpha", alpha)])

    def test_remove_by_path_resolves_a_name_collision(self):
        # A name edited into a collision makes the registry unreadable to
        # the daemon and the sweep; remove must still get it back.
        alpha, beta = self.repo("alpha"), self.repo("beta")
        self.cli("project", "add", str(alpha))
        self.cli("project", "add", str(beta))
        Project.locate(beta).config_path.write_text(
            '[board]\nteam = "team-beta"\nproject_id = "p"\n'
            '[serve]\nname = "alpha"\n')
        with self.assertRaises(HostError):
            Host.locate().projects()
        # The shared name alone is ambiguous: refused, naming both paths.
        with self.assertRaisesRegex(SystemExit,
                                    f"alpha matches {alpha} and {beta}"):
            self.cli("project", "remove", "alpha")
        self.cli("project", "remove", str(beta))
        self.assertEqual(self.registered(), [("alpha", alpha)])

    def test_add_on_a_registered_path_writes_a_missing_store_row(self):
        # The store was recreated after registration: the daemon and the
        # sweep name `project add PATH`, which must write the row back.
        alpha = self.repo("alpha")
        self.cli("project", "add", str(alpha))
        registry = (self.home / "host.toml").read_bytes()
        target = Project.locate(alpha)
        for suffix in ("", "-wal", "-shm"):
            Path(str(target.store_path) + suffix).unlink(missing_ok=True)
        _, out = self.cli("project", "add", str(alpha))
        self.assertIn("host.toml unchanged", out)
        self.assertEqual(
            self.interventions(target.store_path, "register_project"), 1)
        self.assertEqual((self.home / "host.toml").read_bytes(), registry)
        # With its row back, the path is refused as any registered one is.
        with self.assertRaisesRegex(
                SystemExit, f"alpha {alpha} is already registered in"):
            self.cli("project", "add", str(alpha))

    def test_project_list_finds_a_row_written_under_another_spelling(self):
        # The loop can write its row before anyone resolved the path (a
        # symlinked checkout); list finds it by canonical path, as the
        # daemon, the sweep and --status do.
        alpha = self.repo("alpha")
        self.cli("project", "add", str(alpha))
        link = self.root / "link"
        link.symlink_to(alpha)
        conn = store.open(str(Project.locate(alpha).store_path))
        conn.execute("UPDATE projects SET repoPath = ?, admission = 'held',"
                     " holdNote = 'paused'", (str(link),))
        conn.commit()
        conn.close()
        _, out = self.cli("project", "list")
        self.assertEqual(out.splitlines(), [f"alpha\t{alpha}\theld\tpaused"])

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

    def test_only_status_serve_and_supervise_have_a_host_form(self):
        for argv in ([], ["--sweep"], ["--report"], ["--once"],
                     ["--status", "--once"]):
            with self.subTest(argv=argv), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                holophyte.cli.cli(argv)
            self.assertEqual(raised.exception.code, 2)


class HostFaultIsolationTests(HostFixture):
    """One project's config, store or lock that cannot be read is that
    project's error in `--status` and `project list`; the others stay
    whole and the exit is 1."""

    def setUp(self):
        super().setUp()
        self.names = ("alpha", "beta", "gamma", "delta")
        for name in self.names:
            self.cli("project", "add", str(self.repo(name)))
        targets = {name: Project.locate(self.root / name)
                   for name in self.names}
        # beta: a config that is not UTF-8.
        targets["beta"].config_path.write_bytes(b"\xff\xfe[board]\n")
        # gamma: a store that is not a database.
        for suffix in ("", "-wal", "-shm"):
            Path(str(targets["gamma"].store_path) + suffix).unlink(
                missing_ok=True)
        targets["gamma"].store_path.write_bytes(b"not a database " * 256)
        # delta: a merge lock that cannot be read as a file.
        merge_lock_path(targets["delta"]).mkdir()

    def test_host_status_reports_each_failure_as_its_project_error(self):
        code, out = self.cli("--status", "--json")
        self.assertEqual(code, 1)
        projects = {p["path"]: p for p in json.loads(out)["projects"]}
        alpha = projects[str(self.root / "alpha")]
        self.assertIsNone(alpha["error"])
        self.assertEqual([row["admission"] for row in alpha["store"]["projects"]],
                         ["enabled"])
        for name, error in (("beta", "UnicodeDecodeError"),
                            ("gamma", "DatabaseError"),
                            ("delta", "IsADirectoryError")):
            with self.subTest(name=name):
                self.assertIn(error, projects[str(self.root / name)]["error"])

    def test_project_list_lists_every_project_past_a_failure(self):
        code, out = self.cli("project", "list")
        self.assertEqual(code, 1)
        lines = {line.split("\t")[1]: line for line in out.splitlines()}
        self.assertEqual(sorted(lines),
                         sorted(str(self.root / name) for name in self.names))
        self.assertTrue(lines[str(self.root / "alpha")].endswith(
            "\tenabled\t-"))
        self.assertIn("error=", lines[str(self.root / "beta")])
        self.assertIn("error=DatabaseError", lines[str(self.root / "gamma")])


if __name__ == "__main__":
    unittest.main()

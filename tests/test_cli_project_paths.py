"""Explicit registration recognizes paths stored by implicit registration."""
import os
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

import holophyte.admission
import holophyte.cli
import holophyte.project
import store


class ProjectPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patcher = patch.dict(os.environ,
                             {"HOLOPHYTE_HOME": str(self.root / "home")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = store.open(self.root / "store.db", migrate="owner")
        self.addCleanup(self.conn.close)

    def test_project_add_refuses_implicit_symlink_registration_after_team_change(self):
        repo = self.root / "fresh"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        alias = self.root / "alias"
        alias.symlink_to(repo, target_is_directory=True)
        project = store.ensure_project(self.conn, "original-team", alias)
        store.set_admission(self.conn, project, "disabled", "retired")
        target = holophyte.project.Project.locate(repo)
        target.holo_dir.mkdir(parents=True)
        target.config_path.write_text('[board]\nteam = "changed-team"\n'
                                      'project_id = "fresh-project"\n')
        with self.assertRaisesRegex(
                SystemExit, f"project {project} already registered: {repo}"):
            holophyte.cli.cli(["project", "add", str(repo), "--store",
                               str(self.root / "store.db")])
        self.assertEqual(self.conn.execute(
            "SELECT linearTeamId, repoPath, admission, holdNote FROM projects "
            "WHERE id = ?", (project,)).fetchone(),
            ("original-team", str(repo), "disabled", "retired"))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM projects").fetchone(), (1,))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action = 'register_project'"
        ).fetchone(), (0,))

    def test_relative_registration_stays_disabled_after_cwd_change(self):
        repo = self.root / "repo"
        repo.mkdir()
        with chdir(self.root):
            project = store.ensure_project(self.conn, "team", Path("repo"))
        store.set_admission(self.conn, project, "disabled", "retired")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        with chdir(elsewhere):
            target = holophyte.project.Project.locate(repo)
            self.assertEqual(holophyte.admission.state(self.conn, target),
                             ("disabled", "retired"))
            with self.assertRaisesRegex(ValueError, "already registered"):
                store.register_project(self.conn, "changed-team", repo)
        self.assertEqual(self.conn.execute(
            "SELECT repoPath FROM projects").fetchall(), [(str(repo),)])

    def test_legacy_relative_path_requires_repair_after_cwd_change(self):
        repo = self.root / "repo"
        repo.mkdir()
        project = store.ensure_project(self.conn, "team", repo)
        store.set_admission(self.conn, project, "disabled", "retired")
        self.conn.execute("UPDATE projects SET repoPath = 'repo' WHERE id = ?",
                          (project,))
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        with chdir(elsewhere):
            for operation in (
                lambda: store.register_project(self.conn, "changed-team", repo),
                lambda: store.ensure_project(self.conn, "team", repo),
                lambda: holophyte.admission.state(
                    self.conn, holophyte.project.Project.locate(repo)),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(
                            ValueError, f"project {project}.*operator repair"):
                        operation()
        self.assertEqual(self.conn.execute(
            "SELECT repoPath, admission, holdNote FROM projects").fetchall(),
            [("repo", "disabled", "retired")])

"""Explicit registration recognizes paths stored by implicit registration."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli
import holophyte.target
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
        self.conn = store.open(self.root / "store.db")
        self.addCleanup(self.conn.close)

    def test_project_add_refuses_implicit_symlink_registration_after_team_change(self):
        repo = self.root / "fresh"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        alias = self.root / "alias"
        alias.symlink_to(repo, target_is_directory=True)
        project = store.ensure_project(self.conn, "original-team", alias)
        store.set_admission(self.conn, project, "disabled", "retired")
        target = holophyte.target.Target.locate(repo)
        target.holo_dir.mkdir(parents=True)
        target.config_path.write_text('[board]\nteam = "changed-team"\n'
                                      'project_id = "fresh-project"\n')
        with self.assertRaisesRegex(
                SystemExit, f"project {project} already registered: {alias}"):
            holophyte.cli.cli(["project", "add", str(repo), "--store",
                               str(self.root / "store.db")])
        self.assertEqual(self.conn.execute(
            "SELECT linearTeamId, repoPath, admission, holdNote FROM projects "
            "WHERE id = ?", (project,)).fetchone(),
            ("original-team", str(alias), "disabled", "retired"))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM projects").fetchone(), (1,))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM interventions WHERE action = 'register_project'"
        ).fetchone(), (0,))

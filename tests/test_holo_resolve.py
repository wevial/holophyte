"""`holo` resolves its project from -p, HOLO_PROJECT, the current repository
and the client config's default_project, in that order, with real git.

Run: python3 -m unittest discover -s tests -p 'test_holo_resolve.py' -v
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import holophyte.cli.entry
from holophyte.config.project import Project

ROOT = Path(__file__).resolve().parent.parent


def tree(path):
    return sorted((str(file.relative_to(path)), file.stat().st_mtime_ns)
                  for file in path.rglob("*") if file.is_file())


class ResolveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        patcher = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.alpha, self.beta = self.register("alpha"), self.register("beta")
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def register(self, name):
        path = self.root / name
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        target = Project.locate(path, adopt=False)
        target.holo_dir.mkdir(parents=True, exist_ok=True)
        target.config_path.write_text(
            f'[board]\nkind = "native"\nprefix = "{name.upper()}"\n'
            f'[serve]\nname = "{name}"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(holophyte.cli.entry.cli(["project", "add", str(path)]))
        return path

    def client(self, text):
        (self.home / "client.toml").write_text(text)

    def holo(self, *args, cwd, project=None):
        env = {key: value for key, value in os.environ.items()
               if key != "HOLO_PROJECT"}
        env.update(HOLOPHYTE_HOME=str(self.home), PYTHONPATH=str(ROOT),
                   GIT_CEILING_DIRECTORIES=str(self.root))
        if project is not None:
            env["HOLO_PROJECT"] = project
        return subprocess.run([sys.executable, "-m", "holophyte.holo", *args],
                              cwd=cwd, capture_output=True, text=True, env=env)

    def status_target(self, *args, cwd, project=None):
        result = self.holo("status", "--json", *args, cwd=cwd, project=project)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(json.loads(result.stdout)["target"])

    def test_flag_beats_environment_beats_current_repository_beats_default(self):
        self.client('default_project = "beta"\n')
        self.assertEqual(self.status_target("-p", "alpha", cwd=self.alpha,
                                            project="beta"), self.alpha)
        self.assertEqual(self.status_target(cwd=self.alpha, project="beta"),
                         self.beta)
        self.assertEqual(self.status_target(cwd=self.alpha), self.alpha)

    def test_a_subdirectory_of_a_registered_work_tree_is_that_project(self):
        inner = self.alpha / "src" / "deep"
        inner.mkdir(parents=True)
        self.assertEqual(self.status_target(cwd=inner), self.alpha)

    def test_default_project_answers_outside_any_registered_work_tree(self):
        self.client('default_project = "beta"\n')
        unregistered = self.root / "unregistered"
        subprocess.run(["git", "init", "-q", str(unregistered)], check=True)
        for cwd in (self.outside, unregistered):
            with self.subTest(cwd=cwd.name):
                self.assertEqual(self.status_target(cwd=cwd), self.beta)

    def test_a_command_needing_a_project_with_no_source_exits_two_writing_nothing(self):
        before = tree(self.root)
        result = self.holo("requeue", "HOLO-1", "note", cwd=self.outside)
        self.assertEqual(result.returncode, 2, result.stderr)
        for source in ("-p", "HOLO_PROJECT", "current repository",
                       "default_project"):
            self.assertIn(source, result.stderr)
        self.assertEqual(tree(self.root), before)

    def test_status_with_no_source_is_the_host_form(self):
        result = self.holo("status", "--json", cwd=self.outside)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual([project["name"] for project in document["projects"]],
                         ["alpha", "beta"])

    def test_an_unregistered_name_is_refused_not_replaced_by_the_next_source(self):
        result = self.holo("status", cwd=self.alpha, project="gamma")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        for name in ("gamma", "alpha", "beta"):
            self.assertIn(name, result.stderr)

    def test_an_unknown_client_config_key_is_refused_by_every_command(self):
        self.client('default_project = "beta"\ntransport = "ssh"\n')
        for args in (["status"], ["hold", "note", "-p", "alpha"],
                     ["project", "list"], ["--version"]):
            with self.subTest(args=args):
                result = self.holo(*args, cwd=self.alpha)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(str(self.home / "client.toml"), result.stderr)
                self.assertIn("transport", result.stderr)

    def test_verbose_names_the_project_and_its_source(self):
        result = self.holo("status", "--json", "--verbose", cwd=self.alpha)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("project alpha", result.stderr)
        self.assertIn("from current repository", result.stderr)


if __name__ == "__main__":
    unittest.main()

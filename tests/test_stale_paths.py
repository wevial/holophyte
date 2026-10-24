"""Config path entries that match no file tracked on main, as `--status` and
`holo status` report them.

Run: python3 -m unittest discover -s tests -p 'test_stale_paths.py' -v
"""
import io
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import holophyte.config.project
from holophyte.cli.stale_paths import stale_paths
from holophyte.cli.status import status_report
from holophyte.holo.status_page import page
from tests.host_fixture import git
from tests.sweep_fixture import T0, SweepTestCase

UI = ('[merge]\nui_capture = "true"\n'
      'ui_paths = ["app/home.py", "app/old/*", "features/*/page.py"]\n')


class StalePathsTests(SweepTestCase):
    """A main whose second commit moves app/old/page.py to a feature folder."""

    def setUp(self):
        super().setUp()
        git(self.target, "init", "-q", "-b", "main")
        self.write("app/home.py")
        self.write("app/old/page.py")
        git(self.target, "add", ".")
        git(self.target, "commit", "-q", "-m", "first")
        (self.target / "features/page").mkdir(parents=True)
        git(self.target, "mv", "app/old/page.py", "features/page/page.py")
        git(self.target, "commit", "-q", "-m", "move")

    def write(self, name):
        path = self.target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n")

    def stale_ui(self):
        return {"key": "[merge] ui_paths", "entry": "app/old/*", "ref": "main",
                "sha": git(self.target, "rev-parse", "--short=12", "main")}

    def test_a_moved_ui_path_glob_is_the_one_stale_entry(self):
        self.configure(UI)
        self.assertEqual(stale_paths(self.project), [self.stale_ui()])

    def test_review_globs_and_setup_inputs_match_as_their_consumers_do(self):
        self.configure(
            '[review]\nhigh_paths = ["app/old/page.py"]\n'
            'medium_paths = ["page.py", "features/*"]\n'
            '[worktree]\n'
            'setup = [{run = "true", inputs = ["app/old", "features"]}]\n')
        self.assertEqual(
            [(item["key"], item["entry"]) for item in stale_paths(self.project)],
            [("[review] high_paths", "app/old/page.py"),
             ("[worktree] setup inputs", "app/old")])

    def test_only_files_tracked_on_main_count(self):
        git(self.target, "checkout", "-q", "-b", "side")
        self.write("app/old/page.py")
        git(self.target, "add", "app/old/page.py")
        git(self.target, "commit", "-q", "-m", "restore on side")
        self.write("app/old/extra.py")
        self.configure(UI)
        self.assertIn(self.stale_ui(), stale_paths(self.project))

    def test_no_repository_no_main_or_a_refused_table_raises_nothing(self):
        for name, init in (("plain", False), ("empty", True)):
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                if init:
                    git(tmp, "init", "-q", "-b", "main")
                project = holophyte.config.project.Project.locate(Path(tmp))
                self.assertEqual(stale_paths(project), [])
        with self.subTest("refused high_paths"):
            self.configure(UI + '[review]\nhigh_paths = "x"\n')
            self.assertEqual(stale_paths(self.project), [self.stale_ui()])

    def test_a_main_commit_lists_its_tree_once_and_a_new_one_is_reread(self):
        self.configure(UI)
        stale_paths(self.project)
        with patch.object(subprocess, "run", wraps=subprocess.run) as spy:
            stale_paths(self.project)
        self.assertEqual([call for call in spy.call_args_list
                          if call.args[0][:2] == ["git", "ls-tree"]], [])
        self.write("app/old/page.py")
        git(self.target, "add", "app/old/page.py")
        git(self.target, "commit", "-q", "-m", "restore")
        self.assertNotIn("[merge] ui_paths",
                         [item["key"] for item in stale_paths(self.project)])

    def report(self, as_json):
        out = io.StringIO()
        self.assertEqual(status_report(self.project, as_json=as_json, out=out,
                                       now=T0), 0)
        return out.getvalue()

    def test_status_report_carries_the_item_in_json_and_text(self):
        self.configure(UI)
        snap = json.loads(self.report(as_json=True))
        self.assertEqual(snap["stale_config_paths"], [self.stale_ui()])
        self.assertIn(
            'stale config [merge] ui_paths "app/old/*": matches no tracked'
            f" file on main {self.stale_ui()['sha']}",
            self.report(as_json=False).splitlines())

    def test_status_page_lists_the_item_under_problems(self):
        self.configure(UI)
        snap = json.loads(self.report(as_json=True))
        lines = page(snap, T0)
        problems = lines[lines.index("Problems (1)") + 1:]
        self.assertEqual(len(problems), 1)
        self.assertRegex(
            problems[0], r'^  ✗  repo  config  \[merge\] ui_paths "app/old/\*"'
            r" matches nothing on main$")
        del snap["stale_config_paths"]
        self.assertFalse([line for line in page(snap, T0) if "config" in line])

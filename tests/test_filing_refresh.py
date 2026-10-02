"""`--file-ticket` and `--file-story` on a pull-request-mode project bring
`main` up to date with origin before they validate, never by force. The
command line runs against a real bare origin, a real clone and a real store
under a throwaway home.

Run: python3 -m unittest discover -s tests -p 'test_filing_refresh.py' -v
"""
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_fixture import ConfigTestCase  # noqa: E402 - after the sys.path insert
from story_fixture import write_story  # noqa: E402

import holophyte.cli.entry  # noqa: E402
import linear_provider  # noqa: E402
from tests.test_cli_native_update import NATIVE, body, no_linear  # noqa: E402

PR_MODE = NATIVE + '[merge]\nmode = "pr"\n'
MERGED = "tests/test_thing.py"
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def commit(repo, path, message):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text("")
    git(repo, "add", path)
    git(repo, *IDENTITY, "commit", "-q", "-m", message)


class FilingRefreshTests(ConfigTestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "LINEAR_API_KEY"}
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(linear_provider, "_gql", no_linear)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def clone(self, config):
        """A clone of a bare origin on a clean `main`, then a commit adding
        MERGED pushed to origin from elsewhere; origin's new `main`."""
        self.locate(config)
        self.origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        upstream = self.root / "upstream"
        git(self.root, "clone", "-q", str(self.origin), str(upstream))
        commit(upstream, "README", "base")
        git(upstream, "push", "-q", "origin", "main")
        git(self.root, "clone", "-q", str(self.origin), str(self.target))
        commit(upstream, MERGED, "add the merged test module")
        git(upstream, "push", "-q", "origin", "main")
        return git(upstream, "rev-parse", "main")

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                status = holophyte.cli.entry.cli([str(self.target), *args]) or 0
            except SystemExit as exited:
                status = exited.code
        return status, out.getvalue()

    def ticket(self, verify_path=MERGED):
        path = self.root / "T1.md"
        path.write_text(body("First").replace(
            "'test_thing.py'", f"'{Path(verify_path).name}'"))
        return path

    def test_a_ticket_naming_a_file_merged_on_origin_files_after_main_moves(self):
        merged = self.clone(PR_MODE)

        status, printed = self.cli("--file-ticket", str(self.ticket()))

        self.assertEqual(status, 0, printed)
        self.assertIn("[holo2] filed NAT-1", printed)
        self.assertEqual(git(self.target, "rev-parse", "main"), merged)
        self.assertTrue((self.target / MERGED).exists())

    def test_a_story_whose_child_names_a_file_merged_on_origin_files(self):
        merged = self.clone(PR_MODE)
        directory = write_story(self.project.holo_dir / "stories", name="s")
        (child,) = (directory / "children").glob("*.md")
        child.write_text(child.read_text().replace(
            ".venv/bin/python -m unittest tests.test_story_all",
            "python3 -m unittest discover -s tests -p 'test_thing.py'"))

        status, printed = self.cli("--file-story", "s")

        self.assertEqual(status, 0, printed)
        self.assertIn("[holo2] filed NAT-2", printed)
        self.assertEqual(git(self.target, "rev-parse", "main"), merged)

    def test_a_diverged_main_is_left_alone_and_named_in_one_warning(self):
        merged = self.clone(PR_MODE)
        commit(self.target, "tests/test_local.py", "local only")
        local = git(self.target, "rev-parse", "main")

        status, printed = self.cli(
            "--file-ticket", str(self.ticket("tests/test_local.py")))

        self.assertEqual(status, 0, printed)
        self.assertEqual(git(self.target, "rev-parse", "main"), local)
        self.assertEqual(git(self.target, "rev-parse", "origin/main"), merged)
        warnings = [line for line in printed.splitlines() if "warning" in line]
        self.assertEqual(len(warnings), 1, printed)
        self.assertIn("main diverged from origin/main", warnings[0])
        self.assertIn("[holo2] filed NAT-1", printed)
        self.assertFalse((self.target / MERGED).exists())

    def test_a_direct_mode_project_files_without_fetching(self):
        self.clone(NATIVE)
        before = git(self.target, "rev-parse", "origin/main")

        status, printed = self.cli("--file-ticket", str(self.ticket()))

        self.assertEqual(status, 1, printed)
        self.assertIn("path does not exist", printed)
        self.assertEqual(git(self.target, "rev-parse", "origin/main"), before)
        self.assertEqual(git(self.target, "rev-parse", "main"), before)

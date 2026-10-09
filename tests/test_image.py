"""The reviewer image, run through the implementer's hardened container route."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import review_runner
from holophyte.isolation import launcher


@unittest.skipUnless(
    os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
    "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
)
class ImplementerImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker absent")
        review_runner._ensure_image(
            review_runner.IMAGE, review_runner.DOCKERFILE.read_text(),
            candidate="working tree")

    def launch(self, *argv):
        with tempfile.TemporaryDirectory() as workspace:
            route = launcher.Route("container", writable=False)
            return launcher.launch(route, Path(workspace), {}, list(argv),
                                    timeout=120)

    def test_claude_cli_runs_release_2_1_286(self):
        code, output = self.launch("claude", "--version")

        self.assertEqual(code, 0, output)
        self.assertEqual(output.split()[0], "2.1.286", output)

    def test_managed_settings_default_to_bypass_permissions(self):
        code, output = self.launch(
            "cat", "/etc/claude-code/managed-settings.json")

        self.assertEqual(code, 0, output)
        settings = json.loads(output)
        self.assertEqual(settings["permissions"]["defaultMode"],
                         "bypassPermissions")

    def test_node_24_and_npm_run(self):
        code, output = self.launch("node", "--version")
        self.assertEqual(code, 0, output)
        self.assertTrue(output.strip().startswith("v24."), output)

        code, output = self.launch("npm", "--version")
        self.assertEqual(code, 0, output)
        self.assertRegex(output.strip(), r"^\d+\.\d+\.\d+$")

    def test_go_reports_release_1_26_9(self):
        code, output = self.launch("go", "version")

        self.assertEqual(code, 0, output)
        self.assertEqual(output.split()[2], "go1.26.9", output)

    def test_python3_creates_a_virtual_environment_under_home(self):
        code, output = self.launch(
            "sh", "-c",
            "python3 -m venv /home/implementer/venv"
            " && /home/implementer/venv/bin/python -c"
            " 'import sys; print(sys.prefix)'")

        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip().splitlines()[-1],
                         "/home/implementer/venv", output)

    def test_browsers_path_holds_chromium_and_headless_shell(self):
        code, output = self.launch(
            "sh", "-c", 'echo "$PLAYWRIGHT_BROWSERS_PATH"'
            ' && ls "$PLAYWRIGHT_BROWSERS_PATH"')

        self.assertEqual(code, 0, output)
        path, *entries = output.split()
        self.assertTrue(path.startswith("/"), output)
        self.assertTrue(any(re.fullmatch(r"chromium-\d+", e) for e in entries),
                        output)
        self.assertTrue(
            any(re.fullmatch(r"chromium_headless_shell-\d+", e)
                for e in entries), output)

    def test_headless_shell_dumps_the_dom_of_a_data_url(self):
        marker = "holophyte-capture-marker"
        code, output = self.launch(
            "sh", "-c",
            'exec "$(find "$PLAYWRIGHT_BROWSERS_PATH" -type f'
            ' -name chrome-headless-shell)" --no-sandbox --dump-dom "$1"',
            "sh", f"data:text/html,<p>{marker}</p>")

        self.assertEqual(code, 0, output)
        self.assertIn(f"<p>{marker}</p>", output)


@unittest.skipUnless(
    os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
    "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
)
class ReviewerCodexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise AssertionError("HOLOPHYTE_TEST_DOCKER=1 but docker is not on PATH")
        cls.codex = shutil.which("codex")
        if not cls.codex:
            raise AssertionError("HOLOPHYTE_TEST_DOCKER=1 but codex is not on PATH")
        review_runner._ensure_image(
            review_runner.IMAGE, review_runner.DOCKERFILE.read_text(),
            candidate="working tree")

    def run_codex(self, *argv):
        """Codex in the reviewer's container, with the review's own mounts."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = root / "auth.json"
            auth.write_text("{}")
            home, toolchain = review_runner._prepare_runtime(
                root / "runtime", auth, Path(self.codex))
            (root / "candidate").mkdir()
            command = review_runner.container_command(
                image=review_runner.IMAGE, workspace=root / "candidate",
                reviewer_home=home, toolchain=toolchain,
                name=f"holophyte-review-codex-{os.getpid()}", prompt="unused",
                uid=os.getuid(), gid=os.getgid())
            image = command.index(review_runner.IMAGE)
            return subprocess.run(
                [*command[:image + 1], "/opt/codex/bin/codex", *argv],
                capture_output=True, text=True, timeout=120)

    def test_pinned_codex_accepts_disabling_multi_agent(self):
        result = self.run_codex("exec", "--disable", "multi_agent", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)

        result = self.run_codex("--disable", "multi_agent", "features", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"(?m)^multi_agent\s+\S+\s+false\s*$")

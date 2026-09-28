"""The reviewer image, run through the implementer's hardened container route."""

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import review_runner
from holophyte import isolation


@unittest.skipUnless(
    os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
    "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
)
class ImplementerImageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise unittest.SkipTest("Docker absent")
        dockerfile = review_runner.DOCKERFILE.read_text()
        review_runner._ensure_image(
            review_runner.IMAGE, dockerfile, candidate="working tree")
        cls.pinned = re.search(
            r"^ARG CLAUDE_VERSION=(\S+)$", dockerfile, re.M).group(1)

    def launch(self, *argv):
        with tempfile.TemporaryDirectory() as workspace:
            route = isolation.Route("container", writable=False)
            return isolation.launch(route, Path(workspace), {}, list(argv),
                                    timeout=120)

    def test_claude_cli_runs_at_the_pinned_version(self):
        code, output = self.launch("claude", "--version")

        self.assertEqual(code, 0, output)
        self.assertEqual(output.split()[0], self.pinned, output)

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

    def test_python3_creates_a_virtual_environment_under_home(self):
        code, output = self.launch(
            "sh", "-c",
            "python3 -m venv /home/implementer/venv"
            " && /home/implementer/venv/bin/python -c"
            " 'import sys; print(sys.prefix)'")

        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip().splitlines()[-1],
                         "/home/implementer/venv", output)

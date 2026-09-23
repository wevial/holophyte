"""The Playwright capture runner, run as a script by its path (KO-650)."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "holophyte" / "capture_playwright.py"
MODULES = "HOLOPHYTE_TEST_PLAYWRIGHT_MODULES"

# Records its argv, environment and whether the config it was handed exists,
# then writes the file named by FAKE_WRITES into CAPTURE_OUT and exits with
# FAKE_EXIT.
FAKE = """\
import json, os, sys
config = sys.argv[sys.argv.index('--config') + 1]
with open('record.json', 'w') as file:
    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ),
               'config_existed': os.path.exists(config)}, file)
open(os.path.join(os.environ['CAPTURE_OUT'], os.environ['FAKE_WRITES']), 'wb').close()
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
"""


class FakeBootTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name) / "repo"
        (self.repo / ".holophyte-capture").mkdir(parents=True)
        (self.repo / ".holophyte-capture" / "KO-7.capture.ts").write_text("")
        (self.repo / "playwright.config.ts").write_text("export default {};\n")
        (self.repo / "fake.py").write_text(FAKE)

    def capture(self, ticket="KO-7", writes="01-open.png", code=0):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HOLOPHYTE_")}
        env.update(FAKE_WRITES=writes, FAKE_EXIT=str(code))
        if ticket:
            env["HOLOPHYTE_TICKET"] = ticket
        return subprocess.run(
            [sys.executable, str(RUNNER),
             "--boot", shlex.join([sys.executable, "fake.py"]),
             "--env", "HANDLE=-capture-{key}", "out"],
            cwd=self.repo, env=env, capture_output=True, text=True, timeout=60)

    def record(self):
        return json.loads((self.repo / "record.json").read_text())

    def leftovers(self):
        return sorted(p.name for p in (self.repo / ".holophyte-capture").iterdir())

    def test_the_boot_command_gets_the_spec_config_and_environment(self):
        result = self.capture()

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        flag, config, spec = record["argv"][-3:]
        self.assertEqual((flag, spec),
                         ("--config", ".holophyte-capture/KO-7.capture.ts"))
        self.assertTrue(os.path.isabs(config), config)
        self.assertEqual(Path(config).parent,
                         (self.repo / ".holophyte-capture").resolve())
        self.assertTrue(record["config_existed"])
        self.assertEqual(Path(record["env"]["CAPTURE_OUT"]),
                         (self.repo / "out").resolve())
        self.assertEqual(record["env"]["HANDLE"], "-capture-ko7")
        self.assertEqual(self.leftovers(), ["KO-7.capture.ts"])

    def test_a_run_without_a_numbered_screenshot_fails(self):
        result = self.capture(writes="shot.png")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str((self.repo / "out").resolve()), result.stderr)
        self.assertIn("NN-slug.png", result.stderr)

    def test_a_missing_ticket_or_spec_refuses_before_booting(self):
        for ticket, named in ((None, "HOLOPHYTE_TICKET"),
                              ("KO-8", ".holophyte-capture/KO-8.capture.ts")):
            with self.subTest(ticket=ticket):
                result = self.capture(ticket=ticket)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(named, result.stderr)
                self.assertFalse((self.repo / "record.json").exists())

    def test_a_failed_boot_command_fails_the_run_and_cleans_up(self):
        result = self.capture(code=3)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed with exit 3", result.stderr)
        self.assertTrue(self.record()["config_existed"])
        self.assertEqual(self.leftovers(), ["KO-7.capture.ts"])


PLAYWRIGHT_CONFIG = """\
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  projects: [
    { name: 'setup', testMatch: /e2e\\/setup\\/.*\\.setup\\.ts$/ },
    { name: 'chromium', dependencies: ['setup'] },
  ],
});
"""
SPEC = "import { test } from '@playwright/test';\n\ntest('%s', async () => {});\n"


class RealPlaywrightTests(unittest.TestCase):
    def test_the_generated_config_lists_the_capture_spec_and_the_setup(self):
        modules = os.environ.get(MODULES, "")
        if not (Path(modules) / "@playwright" / "test").is_dir():
            self.skipTest(f"{MODULES} does not name a node_modules directory "
                          "holding @playwright/test")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        (repo / "node_modules").symlink_to(Path(modules).resolve())
        (repo / "package.json").write_text('{"type": "module"}\n')
        (repo / "playwright.config.ts").write_text(PLAYWRIGHT_CONFIG)
        (repo / "e2e" / "setup").mkdir(parents=True)
        (repo / "e2e" / "app.spec.ts").write_text(SPEC % "app")
        (repo / "e2e" / "setup" / "auth.setup.ts").write_text(SPEC % "auth")
        (repo / ".holophyte-capture").mkdir()
        (repo / ".holophyte-capture" / ".gitignore").write_text("*\n")
        (repo / ".holophyte-capture" / "KO-7.capture.ts").write_text(SPEC % "capture")
        env = {k: v for k, v in os.environ.items() if not k.startswith("HOLOPHYTE_")}

        result = subprocess.run(
            [sys.executable, str(RUNNER),
             "--boot", "npx playwright test --list --reporter=json", "out"],
            cwd=repo, env={**env, "HOLOPHYTE_TICKET": "KO-7"},
            capture_output=True, text=True, timeout=180)

        listing = json.loads(result.stdout)
        listed = {(test["projectName"], Path(spec["file"]).name)
                  for suite in _suites(listing["suites"])
                  for spec in suite.get("specs", [])
                  for test in spec["tests"]}
        self.assertIn(("chromium", "KO-7.capture.ts"), listed, result.stderr)
        self.assertIn(("setup", "auth.setup.ts"), listed, result.stderr)
        self.assertNotIn("app.spec.ts", {name for _, name in listed})


def _suites(suites):
    for suite in suites:
        yield suite
        yield from _suites(suite.get("suites", []))


if __name__ == "__main__":
    unittest.main()

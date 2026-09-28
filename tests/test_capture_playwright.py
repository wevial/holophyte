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

# Records its argv, environment and the config it was handed (None when it
# does not exist), then writes the file named by FAKE_WRITES into CAPTURE_OUT
# (a directory when the name ends in a slash) and exits with FAKE_EXIT.
FAKE = """\
import json, os, sys
config = sys.argv[sys.argv.index('--config') + 1]
with open('record.json', 'w') as file:
    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ),
               'config_existed': os.path.exists(config),
               'config': open(config).read() if os.path.exists(config) else None},
              file)
written = os.path.join(os.environ['CAPTURE_OUT'], os.environ['FAKE_WRITES'])
if written.endswith('/'):
    os.makedirs(written)
else:
    open(written, 'wb').close()
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

    def capture(self, ticket="KO-7", writes="01-open.png", code=0,
                options=(), states=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HOLOPHYTE_")}
        env.update(FAKE_WRITES=writes, FAKE_EXIT=str(code))
        if ticket:
            env["HOLOPHYTE_TICKET"] = ticket
        if states is not None:
            env["HOLOPHYTE_EVIDENCE_STATES"] = states
        return subprocess.run(
            [sys.executable, str(RUNNER),
             "--boot", shlex.join([sys.executable, "fake.py"]),
             "--env", "HANDLE=-capture-{key}", *options, "out"],
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
        for writes in ("shot.png", "01-open.png/"):
            with self.subTest(writes=writes):
                result = self.capture(writes=writes)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str((self.repo / "out").resolve()), result.stderr)
                self.assertIn("NN-slug.png", result.stderr)
                (self.repo / "record.json").unlink()

    def test_a_missing_ticket_or_spec_refuses_before_booting(self):
        for ticket, named in ((None, "HOLOPHYTE_TICKET"),
                              ("KO-8", ".holophyte-capture/KO-8.capture.ts")):
            with self.subTest(ticket=ticket):
                result = self.capture(ticket=ticket)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(named, result.stderr)
                self.assertFalse((self.repo / "record.json").exists())

    def default_spec(self):
        default = self.repo / "console" / "e2e" / "CAPTURE-0.capture.ts"
        default.parent.mkdir(parents=True)
        default.write_text("")
        return default

    def test_a_ticket_without_spec_or_states_runs_the_default_spec(self):
        default = self.default_spec()

        result = self.capture(ticket="HOLO-9", writes="01-default.png",
                              options=("--default", "console/e2e/CAPTURE-0.capture.ts"))

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        flag, config, spec = record["argv"][-3:]
        self.assertEqual((flag, spec),
                         ("--config", "console/e2e/CAPTURE-0.capture.ts"))
        self.assertEqual(Path(config).parent, default.parent.resolve())
        self.assertIn('testMatch: ["CAPTURE-0.capture.ts"]', record["config"])
        self.assertEqual(sorted(p.name for p in default.parent.iterdir()),
                         ["CAPTURE-0.capture.ts"])

    def test_listed_states_without_a_ticket_spec_refuse_despite_a_default(self):
        self.default_spec()

        result = self.capture(ticket="HOLO-9", states="Board open\nCard moved",
                              options=("--default", "console/e2e/CAPTURE-0.capture.ts"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".holophyte-capture/HOLO-9.capture.ts", result.stderr)
        self.assertFalse((self.repo / "record.json").exists())

    def test_a_ticket_spec_wins_over_the_default_and_a_missing_default_refuses(self):
        self.default_spec()
        result = self.capture(options=("--default", "console/e2e/CAPTURE-0.capture.ts"))

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        self.assertEqual(record["argv"][-1], ".holophyte-capture/KO-7.capture.ts")
        self.assertIn('testMatch: ["KO-7.capture.ts"]', record["config"])
        (self.repo / "record.json").unlink()

        result = self.capture(ticket="HOLO-9",
                              options=("--default", "console/e2e/GONE.capture.ts"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("console/e2e/GONE.capture.ts", result.stderr)
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
    def setUp(self):
        modules = os.environ.get(MODULES, "")
        if not (Path(modules) / "@playwright" / "test").is_dir():
            self.skipTest(f"{MODULES} does not name a node_modules directory "
                          "holding @playwright/test")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        (self.repo / "node_modules").symlink_to(Path(modules).resolve())
        (self.repo / "package.json").write_text('{"type": "module"}\n')
        (self.repo / "playwright.config.ts").write_text(PLAYWRIGHT_CONFIG)
        (self.repo / "e2e" / "setup").mkdir(parents=True)
        (self.repo / "e2e" / "app.spec.ts").write_text(SPEC % "app")
        (self.repo / "e2e" / "setup" / "auth.setup.ts").write_text(SPEC % "auth")
        (self.repo / ".holophyte-capture").mkdir()
        (self.repo / ".holophyte-capture" / ".gitignore").write_text("*\n")

    def listed(self, ticket, options=()):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HOLOPHYTE_")}
        result = subprocess.run(
            [sys.executable, str(RUNNER),
             "--boot", "npx playwright test --list --reporter=json",
             *options, "out"],
            cwd=self.repo, env={**env, "HOLOPHYTE_TICKET": ticket},
            capture_output=True, text=True, timeout=180)
        listing = json.loads(result.stdout)
        return result, {(test["projectName"], Path(spec["file"]).name)
                        for suite in _suites(listing["suites"])
                        for spec in suite.get("specs", [])
                        for test in spec["tests"]}

    def test_the_generated_config_lists_the_capture_spec_and_the_setup(self):
        spec = self.repo / ".holophyte-capture" / "KO-7.capture.ts"
        spec.write_text(SPEC % "capture")

        result, listed = self.listed("KO-7")

        self.assertIn(("chromium", "KO-7.capture.ts"), listed, result.stderr)
        self.assertIn(("setup", "auth.setup.ts"), listed, result.stderr)
        self.assertNotIn("app.spec.ts", {name for _, name in listed})

    def test_a_default_spec_inside_the_test_tree_is_listed_alone(self):
        # The default lives in the project's own testDir, beside a spec the
        # capture must not run, and the generated config is written there.
        (self.repo / "e2e" / "CAPTURE-0.capture.ts").write_text(SPEC % "default")

        result, listed = self.listed(
            "HOLO-9", options=("--default", "e2e/CAPTURE-0.capture.ts"))

        self.assertIn(("chromium", "CAPTURE-0.capture.ts"), listed, result.stderr)
        self.assertIn(("setup", "auth.setup.ts"), listed, result.stderr)
        self.assertNotIn("app.spec.ts", {name for _, name in listed})
        self.assertEqual(sorted(p.name for p in (self.repo / "e2e").iterdir()),
                         ["CAPTURE-0.capture.ts", "app.spec.ts", "setup"])


def _suites(suites):
    for suite in suites:
        yield suite
        yield from _suites(suite.get("suites", []))


if __name__ == "__main__":
    unittest.main()

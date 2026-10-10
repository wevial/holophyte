"""The Playwright capture runner, run as a script by its path (KO-650)."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / "holophyte" / "capture_playwright.py"
MODULES = "HOLOPHYTE_TEST_PLAYWRIGHT_MODULES"

# Records its argv, environment, the config it was handed (None when it does
# not exist) and the files beside that config, then writes the file named by
# FAKE_WRITES into CAPTURE_OUT (a directory when the name ends in a slash) and
# exits with FAKE_EXIT.
FAKE = """\
import json, os, sys
config = sys.argv[sys.argv.index('--config') + 1]
beside = os.path.dirname(config)
with open('record.json', 'w') as file:
    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ),
               'config_existed': os.path.exists(config),
               'config': open(config).read() if os.path.exists(config) else None,
               'beside': {name: open(os.path.join(beside, name)).read()
                          for name in os.listdir(beside)
                          if os.path.isfile(os.path.join(beside, name))}},
              file)
written = os.path.join(os.environ['CAPTURE_OUT'], os.environ['FAKE_WRITES'])
if written.endswith('/'):
    os.makedirs(written)
else:
    open(written, 'wb').close()
sys.exit(int(os.environ.get('FAKE_EXIT', '0')))
"""
# Writes 01-a.png into CAPTURE_OUT and each FAKE_VIDEOS [test, bytes] pair as
# test/video.webm under the config's outputDir, one second apart, and records
# that directory in recordings.txt.
VIDEO_FAKE = """\
import json, os, re, sys
config = open(sys.argv[sys.argv.index('--config') + 1]).read()
recordings = re.search(r'outputDir: "([^"]+)"', config)[1]
open('recordings.txt', 'w').write(recordings)
open(os.path.join(os.environ['CAPTURE_OUT'], '01-a.png'), 'wb').close()
for number, (test, data) in enumerate(json.loads(os.environ['FAKE_VIDEOS'])):
    os.makedirs(os.path.join(recordings, test))
    video = os.path.join(recordings, test, 'video.webm')
    open(video, 'w').write(data)
    written = (1_700_000_000 + number) * 10**9
    os.utime(video, ns=(written, written))
"""
# Imports the generated config and records it as the boot received it.
NODE_BOOT = """\
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const argv = process.argv.slice(2);
const generated = argv[argv.indexOf('--config') + 1];
const config = (await import(pathToFileURL(generated).href)).default;
fs.writeFileSync('record.json', JSON.stringify({
  text: fs.readFileSync(generated, 'utf8'), config,
  outputs: (config.projects || [config]).filter((entry) => entry.outputDir)
    .map((entry) => fs.statSync(entry.outputDir).isDirectory())}));
fs.writeFileSync(path.join(process.env.CAPTURE_OUT, '01-a.png'), '');
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
        (self.repo / "video_fake.py").write_text(VIDEO_FAKE)
        (self.repo / "boot.mjs").write_text(NODE_BOOT)

    def capture(self, ticket="KO-7", writes="01-open.png", code=0,
                options=(), states=None, boot=(sys.executable, "fake.py"),
                video=False, videos=()):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HOLOPHYTE_")}
        env.update(FAKE_WRITES=writes, FAKE_EXIT=str(code),
                   FAKE_VIDEOS=json.dumps(videos))
        if video:
            env["HOLOPHYTE_CAPTURE_VIDEO"] = "1"
        if ticket:
            env["HOLOPHYTE_TICKET"] = ticket
        if states is not None:
            env["HOLOPHYTE_EVIDENCE_STATES"] = states
        return subprocess.run(
            [sys.executable, str(RUNNER),
             "--boot", shlex.join(boot),
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
                         ("--config", r"\.holophyte-capture/KO-7\.capture\.ts"))
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
                         ("--config", r"console/e2e/CAPTURE-0\.capture\.ts"))
        self.assertEqual(Path(config).parent, default.parent.resolve())
        self.assertIn('testMatch: ["CAPTURE-0.capture.ts"]', record["config"])
        self.assertEqual(sorted(p.name for p in default.parent.iterdir()),
                         ["CAPTURE-0.capture.ts"])

    def test_listed_states_without_a_ticket_spec_refuse_despite_a_default(self):
        self.default_spec()
        default = ("--default", "console/e2e/CAPTURE-0.capture.ts")
        # States inherited from the factory, or handed over through --env.
        for states, options in (
                ("Board open\nCard moved", default),
                (None, ("--env", "HOLOPHYTE_EVIDENCE_STATES=Board open", *default))):
            with self.subTest(options=options):
                result = self.capture(ticket="HOLO-9", states=states,
                                      options=options)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(".holophyte-capture/HOLO-9.capture.ts", result.stderr)
                self.assertFalse((self.repo / "record.json").exists())

    def test_a_ticket_spec_wins_over_the_default_and_a_missing_default_refuses(self):
        self.default_spec()
        result = self.capture(options=("--default", "console/e2e/CAPTURE-0.capture.ts"))

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        self.assertEqual(record["argv"][-1], r"\.holophyte-capture/KO-7\.capture\.ts")
        self.assertIn('testMatch: ["KO-7.capture.ts"]', record["config"])
        (self.repo / "record.json").unlink()

        result = self.capture(ticket="HOLO-9",
                              options=("--default", "console/e2e/GONE.capture.ts"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("console/e2e/GONE.capture.ts", result.stderr)
        self.assertFalse((self.repo / "record.json").exists())

    def outside_default(self):
        # Beside the factory's config, a sibling of the project.
        default = self.repo.parent / "factory" / "CAPTURE-0.capture.ts"
        default.parent.mkdir()
        default.write_text("// the factory's default capture\n")
        return default

    def test_a_default_outside_the_working_directory_runs_from_a_copy(self):
        default = self.outside_default()

        result = self.capture(ticket="HOLO-9", writes="01-default.png",
                              options=("--default", str(default)))

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        config = record["argv"][record["argv"].index("--config") + 1]
        self.assertEqual(Path(config).parent,
                         (self.repo / ".holophyte-capture").resolve())
        copy = re.search(r'testMatch: \["([^"]+)"\]', record["config"])[1]
        self.assertTrue(copy.endswith(".capture.ts"), copy)
        self.assertEqual(record["beside"][copy], default.read_text())
        self.assertEqual(self.leftovers(), ["KO-7.capture.ts"])
        self.assertEqual(sorted(p.name for p in default.parent.iterdir()),
                         ["CAPTURE-0.capture.ts"])

    def test_a_failed_boot_removes_the_copy_of_an_outside_default(self):
        default = self.outside_default()

        result = self.capture(ticket="HOLO-9", code=1,
                              options=("--default", str(default)))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed with exit 1", result.stderr)
        record = self.record()
        config = record["argv"][record["argv"].index("--config") + 1]
        self.assertEqual(Path(config).parent,
                         (self.repo / ".holophyte-capture").resolve())
        self.assertIn(default.read_text(), record["beside"].values())
        self.assertEqual(self.leftovers(), ["KO-7.capture.ts"])

    @unittest.skipIf(os.geteuid() == 0, "root reads a file without permission")
    def test_an_unreadable_outside_default_leaves_no_copy(self):
        default = self.outside_default()
        default.chmod(0)
        self.addCleanup(default.chmod, 0o644)

        result = self.capture(ticket="HOLO-9",
                              options=("--default", str(default)))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PermissionError", result.stderr)
        self.assertFalse((self.repo / "record.json").exists())
        self.assertEqual(self.leftovers(), ["KO-7.capture.ts"])

    def test_an_outside_default_without_a_capture_directory_refuses(self):
        default = self.outside_default()

        result = self.capture(ticket="HOLO-9",
                              options=("--dir", "gone", "--default", str(default)))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected gone", result.stderr)
        self.assertFalse((self.repo / "record.json").exists())

    @unittest.skipUnless(shutil.which("node"), "node imports the generated config")
    def test_video_records_only_the_capture_project_and_off_leaves_the_config(self):
        (self.repo / "playwright.config.mjs").write_text(
            "export default {use: {viewport: {width: 800, height: 600}}, projects: [\n"
            "  {name: 'setup', testMatch: /auth\\.setup\\.ts$/},\n"
            "  {name: 'chromium', dependencies: ['setup'],"
            " use: {colorScheme: 'dark'}},\n]};\n")
        options = ("--config", "playwright.config.mjs")

        result = self.capture(boot=("node", "boot.mjs"), options=options,
                              video=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        setup, chromium = record["config"]["projects"]
        self.assertEqual(chromium["use"], {"colorScheme": "dark", "video": "on"})
        self.assertTrue(os.path.isabs(chromium["outputDir"]), chromium)
        self.assertEqual(record["outputs"], [True])
        self.assertFalse(os.path.exists(chromium["outputDir"]))
        self.assertNotIn("use", setup)
        self.assertNotIn("outputDir", setup)
        self.assertNotIn("outputDir", record["config"])

        result = self.capture(boot=("node", "boot.mjs"), options=options)

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        self.assertNotIn("video", record["text"])
        self.assertNotIn("outputDir", record["text"])
        self.assertEqual(record["config"]["projects"][1]["use"],
                         {"colorScheme": "dark"})

    @unittest.skipUnless(shutil.which("node"), "node imports the generated config")
    def test_video_on_a_config_without_projects_records_at_the_top_level(self):
        (self.repo / "playwright.config.mjs").write_text(
            "export default {use: {viewport: {width: 800, height: 600}}};\n")

        result = self.capture(boot=("node", "boot.mjs"), video=True,
                              options=("--config", "playwright.config.mjs"))

        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.record()
        self.assertEqual(record["config"]["use"],
                         {"viewport": {"width": 800, "height": 600}, "video": "on"})
        self.assertEqual(record["outputs"], [True])

    def test_recordings_are_copied_in_written_order_and_their_directory_removed(self):
        result = self.capture(boot=(sys.executable, "video_fake.py"), video=True,
                              videos=[["z-first", "first"], ["a-second", "second"]])

        self.assertEqual(result.returncode, 0, result.stderr)
        output = self.repo / "out"
        self.assertEqual(sorted(p.name for p in output.iterdir()),
                         ["01-a.png", "video-01.webm", "video-02.webm"])
        self.assertEqual((output / "video-01.webm").read_text(), "first")
        self.assertEqual((output / "video-02.webm").read_text(), "second")
        recordings = (self.repo / "recordings.txt").read_text()
        self.assertFalse(os.path.exists(recordings), recordings)
        self.assertEqual(self.leftovers(), ["KO-7.capture.ts"])

    def test_a_video_run_that_recorded_nothing_still_passes_and_says_so(self):
        result = self.capture(boot=(sys.executable, "video_fake.py"), video=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("capture_playwright: no video was recorded", result.stderr)
        self.assertEqual([p.name for p in (self.repo / "out").iterdir()],
                         ["01-a.png"])

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
        # Its `+` would be a quantifier in Playwright's regex file filter.
        (self.repo / "e2e" / "CAPTURE+0.capture.ts").write_text(SPEC % "default")

        result, listed = self.listed(
            "HOLO-9", options=("--default", "e2e/CAPTURE+0.capture.ts"))

        self.assertIn(("chromium", "CAPTURE+0.capture.ts"), listed, result.stderr)
        self.assertIn(("setup", "auth.setup.ts"), listed, result.stderr)
        self.assertNotIn("app.spec.ts", {name for _, name in listed})
        self.assertEqual(sorted(p.name for p in (self.repo / "e2e").iterdir()),
                         ["CAPTURE+0.capture.ts", "app.spec.ts", "setup"])

    def test_a_default_outside_the_project_resolves_the_project_modules(self):
        # Node resolves @playwright/test upward from the spec's directory,
        # which for this default holds no node_modules.
        factory = tempfile.TemporaryDirectory()
        self.addCleanup(factory.cleanup)
        default = Path(factory.name) / "CAPTURE-0.capture.ts"
        default.write_text(SPEC % "default")

        result, listed = self.listed("HOLO-9", options=("--default", str(default)))

        chromium = {name for project, name in listed if project == "chromium"}
        self.assertEqual(len(chromium), 1, result.stderr)
        self.assertTrue(chromium.pop().endswith(".capture.ts"), listed)
        self.assertIn(("setup", "auth.setup.ts"), listed, result.stderr)
        self.assertEqual(
            sorted(p.name for p in (self.repo / ".holophyte-capture").iterdir()),
            [".gitignore"])
        self.assertEqual([p.name for p in Path(factory.name).iterdir()],
                         ["CAPTURE-0.capture.ts"])


def _suites(suites):
    for suite in suites:
        yield suite
        yield from _suites(suite.get("suites", []))


if __name__ == "__main__":
    unittest.main()

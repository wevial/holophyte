"""KO-639: the per-module parallel runner and the `unit` workflow that runs it."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "run_modules.py"
WORKFLOW = ROOT / ".github" / "workflows" / "unit.yml"

PASSING = """
import unittest

class Passing(unittest.TestCase):
    def test_passes(self):
        self.assertTrue(True)
"""

FAILING = """
import unittest

class Failing(unittest.TestCase):
    def test_fails(self):
        self.assertEqual("wanted", "got-this-instead")
"""

EMPTY = """
import unittest

class Empty(unittest.TestCase):
    pass
"""

CLAIMS_HOME = """
import os
import unittest
from pathlib import Path

class ClaimsHome(unittest.TestCase):
    def test_home_is_fresh(self):
        marker = Path(os.environ["HOLOPHYTE_HOME"]) / "claimed"
        self.assertFalse(marker.exists(), "another module shared this home")
        marker.write_text("claimed")
"""


class RunModulesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()

    def run_over(self, modules, *args):
        for name, body in modules.items():
            (self.tests / name).write_text(textwrap.dedent(body))
        # One shared home in the runner's own environment: a module that
        # inherited it instead of getting its own would see its sibling's files.
        shared = self.root / "shared-home"
        shared.mkdir()
        return subprocess.run(
            [sys.executable, str(RUNNER), "--dir", str(self.tests), *args],
            env=dict(os.environ, HOLOPHYTE_HOME=str(shared)),
            capture_output=True, text=True, timeout=120)

    def test_failing_module_fails_the_run_and_its_output_follows_the_summary(self):
        done = self.run_over({"test_good.py": PASSING, "test_bad.py": FAILING},
                             "--jobs", "2")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        lines = done.stdout.splitlines()
        good = next(i for i, line in enumerate(lines)
                    if line.startswith("test_good.py"))
        bad = next(i for i, line in enumerate(lines)
                   if line.startswith("test_bad.py"))
        self.assertIn("ok", lines[good])
        self.assertIn("FAILED", lines[bad])
        report = next(i for i, line in enumerate(lines)
                      if "test_bad.py" in line and i > max(good, bad))
        self.assertIn("got-this-instead", "\n".join(lines[report:]))
        self.assertNotIn("got-this-instead", "\n".join(lines[:report]))

    def test_module_without_tests_fails_and_is_named(self):
        done = self.run_over({"test_good.py": PASSING, "test_empty.py": EMPTY})
        self.assertNotEqual(done.returncode, 0, done.stdout)
        empty = [line for line in done.stdout.splitlines()
                 if line.startswith("test_empty.py")]
        self.assertEqual(len(empty), 1, done.stdout)
        self.assertIn("zero tests", empty[0])

    def test_each_module_gets_its_own_holophyte_home(self):
        done = self.run_over({"test_first.py": CLAIMS_HOME,
                              "test_second.py": CLAIMS_HOME}, "--jobs", "2")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(list((self.root / "shared-home").iterdir()), [])

    def test_no_module_found_fails(self):
        done = self.run_over({})
        self.assertNotEqual(done.returncode, 0, done.stdout)


class UnitWorkflowTests(unittest.TestCase):
    def test_unit_job_runs_the_runner_on_pull_requests_merge_groups_and_main(self):
        text = WORKFLOW.read_text()
        on = text[text.index("\non:"):text.index("\njobs:")]
        self.assertIn("pull_request", on)
        # A merge queue waits for its required checks on the merge group.
        self.assertRegex(on, r"\n  merge_group:\n")
        self.assertRegex(on, r"push:\s*\n\s+branches: \[main\]")
        jobs = text[text.index("\njobs:"):]
        self.assertRegex(jobs, r"\n  unit:\n")
        self.assertIn('python-version: "3.14"', jobs)
        self.assertIn("pip install -r requirements.txt", jobs)
        self.assertIn("python3 tests/run_modules.py", jobs)


if __name__ == "__main__":
    unittest.main()

"""KO-639: the per-module parallel runner and the `unit` workflow that runs it."""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
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

SLEEPS_AFTER_OUTPUT = """
import sys
import time
import unittest

class Sleeps(unittest.TestCase):
    def test_sleeps(self):
        for n in range(1, 61):
            print(f"progress line {n}", file=sys.stderr, flush=True)
        time.sleep(30)
"""

HANGS_WITH_A_CHILD = """
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

class HangsWithAChild(unittest.TestCase):
    def test_hangs(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(60)"])
        Path({record!r}).write_text(f"{{os.getpgid(0)}} {{child.pid}}")
        time.sleep(60)
"""


def live_members(pgid):
    """Pids in process group `pgid` that are running (not zombies), from /proc."""
    members = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rpartition(")")[2].split()
        except OSError:
            continue
        if int(fields[2]) == pgid and fields[0] != "Z":
            members.append(int(stat.parent.name))
    return members


def block_scalars(text, *path):
    """The `key: value` scalars directly under the block mapping at `path` in
    YAML `text`, read by indentation: requirements.txt ships no YAML parser,
    so this reads only block mappings, skipping sequence items under them."""
    found, stack = {}, []
    for raw in text.splitlines():
        line = "" if raw.lstrip().startswith("#") else raw.split(" #")[0]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        content = line.strip()
        if content.startswith("-") or ":" not in content:
            continue
        key, _, value = content.partition(":")
        if [k for _, k in stack] == list(path) and value.strip():
            found[key] = value.strip().strip('"')
        stack.append((indent, key))
    return found


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
        shared.mkdir(exist_ok=True)
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

    def module_lines(self, done):
        return {line.split()[0]: line for line in done.stdout.splitlines()
                if line.startswith("test_") and line.split()[0].endswith(".py")}

    def test_named_modules_and_globs_run_only_the_modules_they_match(self):
        modules = {"test_alpha.py": PASSING, "test_beta.py": PASSING,
                   "test_gamma_one.py": PASSING, "test_bad.py": FAILING,
                   "test_unnamed.py": FAILING}
        done = self.run_over(modules, "test_alpha.py", "tests/test_beta.py",
                             "tests/test_gamma*.py")
        self.assertEqual(done.returncode, 0, done.stdout)
        lines = self.module_lines(done)
        self.assertEqual(sorted(lines), ["test_alpha.py", "test_beta.py",
                                         "test_gamma_one.py"])
        self.assertTrue(all(line.endswith("ok") for line in lines.values()),
                        done.stdout)

        done = self.run_over(modules, "test_alpha.py", "tests/test_beta.py",
                             "tests/test_gamma*.py", "tests/test_bad.py")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertEqual(sorted(self.module_lines(done)),
                         ["test_alpha.py", "test_bad.py", "test_beta.py",
                          "test_gamma_one.py"])
        summary = done.stdout.rstrip().splitlines()[-1]
        self.assertIn("test_bad.py", summary)
        self.assertNotIn("test_alpha.py", summary)

    def test_a_glob_matching_no_module_fails_the_run_and_is_named(self):
        done = self.run_over({"test_good.py": PASSING},
                             "tests/test_good.py", "tests/test_typo*.py")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("tests/test_typo*.py: matches no", done.stdout)
        self.assertEqual(self.module_lines(done), {}, done.stdout)

    def test_hung_module_is_killed_at_its_timeout_named_and_its_tail_shown(self):
        started = time.monotonic()
        done = self.run_over({"test_sleeps.py": SLEEPS_AFTER_OUTPUT,
                              "test_good.py": PASSING},
                             "--jobs", "2", "--module-timeout", "2")
        self.assertLess(time.monotonic() - started, 15, done.stdout)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        lines = self.module_lines(done)
        self.assertTrue(lines["test_sleeps.py"].endswith(
            "FAILED (timed out after 2 s)"), done.stdout)
        self.assertTrue(lines["test_good.py"].endswith("ok"), done.stdout)
        report = done.stdout[done.stdout.index("===== test_sleeps.py"):]
        shown = report.splitlines()
        self.assertIn("progress line 60", shown)
        self.assertIn("progress line 21", shown)
        self.assertNotIn("progress line 20", shown)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "reads /proc")
    def test_timed_out_module_leaves_no_process_of_its_group_running(self):
        record = self.root / "group"
        done = self.run_over(
            {"test_hangs.py": HANGS_WITH_A_CHILD.format(record=str(record))},
            "--module-timeout", "5")
        self.assertIn("FAILED (timed out after 5 s)", done.stdout)
        pgid, child = (int(n) for n in record.read_text().split())
        self.addCleanup(self.kill_survivors, pgid)
        self.assertNotEqual(pgid, os.getpgid(0))
        deadline = time.monotonic() + 5
        while live_members(pgid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(live_members(pgid), [], f"child {child} survived")

    @staticmethod
    def kill_survivors(pgid):
        for pid in live_members(pgid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


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

    def test_unit_job_is_capped_at_thirty_minutes(self):
        unit = block_scalars(WORKFLOW.read_text(), "jobs", "unit")
        self.assertEqual(unit.get("runs-on"), "ubuntu-latest", unit)
        self.assertLessEqual(int(unit["timeout-minutes"]), 30)


if __name__ == "__main__":
    unittest.main()

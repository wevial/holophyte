"""KO-639: the per-module parallel runner and the `unit` workflow that runs it."""
import json
import os
import re
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

COMMITS_WITHOUT_REPACKING = """
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

class CommitWithoutRepacking(unittest.TestCase):
    def test_commit_leaves_no_automatic_pack_files(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)

            def git(*args, **kwargs):
                return subprocess.run(
                    ["git", *args], cwd=repo, check=True,
                    capture_output=True, text=True, **kwargs).stdout.strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            found = 0
            candidate = 0
            while found < 2:
                content = f"x{candidate}\\n"
                candidate += 1
                if git("hash-object", "--stdin", input=content).startswith("17"):
                    (repo / f"blob-{found}").write_text(content)
                    found += 1
            git("add", "-A")
            git("commit", "-qm", "m")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                packs = list((repo / ".git/objects/pack").iterdir())
                self.assertEqual(packs, [], "automatic maintenance wrote pack files")
                time.sleep(0.01)
"""

SLEEPS = """
import time
import unittest

class Sleeps(unittest.TestCase):
    def test_sleeps(self):
        time.sleep({seconds})
"""

BUSY = """
import time
import unittest

class Busy(unittest.TestCase):
    def test_keeps_the_cpu_busy(self):
        started = time.process_time()
        while time.process_time() - started < 0.5:
            pass
"""

LINE = re.compile(r"^(test_\S+\.py)\s+([0-9.]+)s\s+([0-9.]+)s cpu\s")

CHECKS_GIT_CONFIG = """
import subprocess
import unittest

class GitConfig(unittest.TestCase):
    def test_existing_config_survives_with_automatic_maintenance_disabled(self):
        def config(key):
            return subprocess.run(
                ["git", "config", "--get", key],
                capture_output=True, text=True).stdout.strip()

        self.assertEqual(config("core.abbrev"), "12")
        self.assertEqual(config("maintenance.auto"), "false")
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
    so this reads only block mappings, skipping sequence items under them.
    Lines under a key that has a value of its own are not its mapping."""
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
            found[key] = value.strip()
        stack.append((indent, None if value.strip() else key))
    return found


class RunModulesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()

    def run_over(self, modules, *args, git_config=None):
        for name, body in modules.items():
            (self.tests / name).write_text(textwrap.dedent(body))
        # One shared home in the runner's own environment: a module that
        # inherited it instead of getting its own would see its sibling's files.
        shared = self.root / "shared-home"
        shared.mkdir(exist_ok=True)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_CONFIG_")}
        env.update(git_config or {})
        return subprocess.run(
            [sys.executable, str(RUNNER), "--dir", str(self.tests), *args],
            env=dict(env, HOLOPHYTE_HOME=str(shared)),
            capture_output=True, text=True, timeout=120)

    def test_module_commits_do_not_start_automatic_repacking(self):
        done = self.run_over({"test_commit.py": COMMITS_WITHOUT_REPACKING})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(self.module_lines(done)["test_commit.py"].endswith("ok"),
                        done.stdout)

    def test_existing_git_config_is_preserved_with_maintenance_disabled(self):
        done = self.run_over({"test_config.py": CHECKS_GIT_CONFIG}, git_config={
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.abbrev",
            "GIT_CONFIG_VALUE_0": "12",
        })
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

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

    def timed_lines(self, done):
        return {m[1]: (float(m[2]), float(m[3]))
                for m in map(LINE.match, done.stdout.splitlines()) if m}

    def test_each_line_reports_the_module_cpu_seconds_beside_its_wall(self):
        done = self.run_over({"test_busy.py": BUSY,
                              "test_sleep.py": SLEEPS.format(seconds=1)},
                             "--jobs", "2")
        self.assertEqual(done.returncode, 0, done.stdout)
        lines = self.timed_lines(done)
        self.assertGreaterEqual(lines["test_busy.py"][1], 0.4, done.stdout)
        wall, cpu = lines["test_sleep.py"]
        self.assertGreaterEqual(wall, 1.0, done.stdout)
        self.assertLess(cpu, wall / 2, done.stdout)

    def slowest_block(self, done):
        lines = done.stdout.rstrip().splitlines()
        heading = next(i for i, line in enumerate(lines) if "slowest" in line)
        block = [line.split() for line in lines[heading + 1:-1] if line]
        return heading, lines, [(name, float(wall.rstrip("s")))
                                for name, wall in block]

    def test_the_ten_slowest_modules_follow_the_failures_longest_first(self):
        modules = {f"test_m{n:02d}.py": SLEEPS.format(seconds=n / 20)
                   for n in range(11)}
        modules["test_bad.py"] = FAILING
        done = self.run_over(modules, "--jobs", "12")
        heading, lines, block = self.slowest_block(done)
        report = next(i for i, line in enumerate(lines)
                      if line.startswith("===== test_bad.py"))
        self.assertLess(report, heading)
        self.assertRegex(lines[-1], r"^12 modules, ")
        self.assertEqual(len(block), 10, done.stdout)
        walls = [wall for _, wall in block]
        self.assertEqual(walls, sorted(walls, reverse=True))
        timed = {name: wall for name, (wall, _) in self.timed_lines(done).items()}
        unlisted = [timed[name] for name in set(timed) - {n for n, _ in block}]
        self.assertEqual(len(unlisted), 2)
        self.assertGreaterEqual(min(walls), max(unlisted))

    def test_a_run_of_three_lists_all_three_as_slowest(self):
        names = ["test_one.py", "test_two.py", "test_three.py"]
        done = self.run_over(dict.fromkeys(names, PASSING), "--jobs", "3")
        self.assertEqual(sorted(name for name, _ in self.slowest_block(done)[2]),
                         sorted(names))

    def test_a_module_over_the_ceiling_fails_and_is_named(self):
        modules = {"test_slow.py": SLEEPS.format(seconds=1.5)}
        done = self.run_over(modules, "--max-module-sec", "1")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("over the 1 s ceiling",
                      self.module_lines(done)["test_slow.py"])
        summary = done.stdout.rstrip().splitlines()[-1]
        self.assertIn("failed: test_slow.py", summary)
        self.assertIn("===== test_slow.py: FAILED (ran", done.stdout)
        for args in (("--max-module-sec", "5"), ()):
            with self.subTest(args=args):
                done = self.run_over(modules, *args)
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertTrue(
                    self.module_lines(done)["test_slow.py"].endswith("ok"))

    def test_recorded_times_are_written_and_order_a_later_list(self):
        record = self.root / "times.json"
        done = self.run_over({"test_fast.py": PASSING,
                              "test_slow.py": SLEEPS.format(seconds=1.2)},
                             "--jobs", "2", "--record", str(record))
        self.assertEqual(done.returncode, 0, done.stdout)
        times = json.loads(record.read_text())
        self.assertEqual(sorted(times), ["test_fast.py", "test_slow.py"])
        self.assertGreaterEqual(times["test_slow.py"], 1.2)
        (self.tests / "module_times.json").write_text(record.read_text())
        self.assertEqual(self.listed(),
                         sorted(times, key=times.get, reverse=True))

    def listed(self, *args):
        done = self.run_over({}, "--list", *args)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done.stdout.splitlines()

    def fixture(self, names, times):
        for name in names:
            (self.tests / name).write_text(PASSING)
        (self.tests / "module_times.json").write_text(json.dumps(times))

    def test_shards_of_the_real_directory_split_every_module_once(self):
        every = {path.name for path in (ROOT / "tests").glob("test_*.py")}
        for count in (2, 3):
            with self.subTest(count=count):
                shards = []
                for index in range(1, count + 1):
                    done = subprocess.run(
                        [sys.executable, str(RUNNER), "--list",
                         "--shard", f"{index}/{count}"],
                        capture_output=True, text=True, timeout=60)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    shards.append(done.stdout.splitlines())
                names = [name for shard in shards for name in shard]
                self.assertEqual(len(names), len(set(names)), shards)
                self.assertEqual(set(names), every)

    def test_two_shards_balance_recorded_times_and_run_longest_first(self):
        times = {"test_a.py": 5.0, "test_b.py": 4.0, "test_c.py": 3.0,
                 "test_d.py": 3.0, "test_e.py": 2.0}
        self.fixture(times, times)
        shards = [self.listed("--shard", f"{index}/2") for index in (1, 2)]
        self.assertEqual(sorted(shards[0] + shards[1]), sorted(times))
        totals = [sum(times[name] for name in shard) for shard in shards]
        self.assertLessEqual(abs(totals[0] - totals[1]), max(times.values()))
        for shard in shards:
            self.assertEqual([times[name] for name in shard],
                             sorted((times[name] for name in shard),
                                    reverse=True))

    def test_recorded_time_not_file_size_sets_the_order(self):
        (self.tests / "test_small.py").write_text(PASSING)
        (self.tests / "test_large.py").write_text(PASSING + "\n# pad" * 500)
        (self.tests / "module_times.json").write_text(json.dumps(
            {"test_small.py": 3.0, "test_large.py": 0.1}))
        done = self.run_over({}, "--jobs", "1")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(list(self.timed_lines(done)),
                         ["test_small.py", "test_large.py"])

    def test_unrecorded_module_counts_as_the_median_and_ghosts_are_ignored(self):
        times = {"test_a.py": 4.0, "test_b.py": 3.0, "test_c.py": 1.0,
                 "test_d.py": 0.5}
        self.fixture([*times, "test_new.py"],
                     dict(times, **{"test_gone.py": 9.0}))
        self.assertEqual(self.listed(), ["test_a.py", "test_b.py",
                                         "test_new.py", "test_c.py",
                                         "test_d.py"])
        self.assertEqual(self.listed("test_d.py", "test_new.py"),
                         ["test_new.py", "test_d.py"])
        shards = [self.listed("--shard", f"{index}/2") for index in (1, 2)]
        self.assertEqual((shards[0] + shards[1]).count("test_new.py"), 1)

    def test_malformed_or_out_of_range_shard_is_a_usage_error(self):
        for value in ("0/2", "3/2", "1/0", "x"):
            with self.subTest(value=value):
                done = self.run_over({"test_good.py": PASSING}, "--shard", value)
                self.assertEqual(done.returncode, 2, done.stdout)
                self.assertIn("--shard", done.stderr)
                self.assertEqual(self.module_lines(done), {}, done.stdout)

    @staticmethod
    def kill_survivors(pgid):
        for pid in live_members(pgid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def job_blocks(text):
    """Each job id under `jobs:` in workflow `text`, mapped to its lines."""
    jobs, current = {}, None
    for line in text[text.index("\njobs:"):].splitlines()[2:]:
        match = re.fullmatch(r"  ([\w-]+):", line)
        if match:
            current = jobs.setdefault(match[1], [])
        elif current is not None:
            current.append(line)
    return jobs


def run_script(lines):
    """The body of the block-scalar `run: |` in a job's `lines`, dedented."""
    start = next(i for i, line in enumerate(lines)
                 if line.strip() == "run: |")
    indent = len(lines[start]) - len(lines[start].lstrip())
    body = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


class UnitWorkflowTests(unittest.TestCase):
    def test_triggers_stay_pull_requests_merge_groups_and_main(self):
        text = WORKFLOW.read_text()
        on = text[text.index("\non:"):text.index("\njobs:")]
        self.assertIn("pull_request", on)
        # A merge queue waits for its required checks on the merge group.
        self.assertRegex(on, r"\n  merge_group:\n")
        self.assertRegex(on, r"push:\s*\n\s+branches: \[main\]")

    def test_two_python_shards_a_console_job_and_a_unit_job_over_both(self):
        text = WORKFLOW.read_text()
        jobs = job_blocks(text)
        self.assertEqual(len(jobs), 3, sorted(jobs))
        unit = block_scalars(text, "jobs", "unit")
        needs = re.findall(r"[\w-]+", unit["needs"])
        self.assertEqual(len(needs), 2, unit)
        self.assertEqual(set(needs) | {"unit"}, set(jobs))
        self.assertEqual(unit["if"], "always()")
        python = next(job for job in needs
                      if "tests/run_modules.py" in "\n".join(jobs[job]))
        console = next(job for job in needs if job != python)
        strategy = block_scalars(text, "jobs", python, "strategy")
        self.assertEqual(strategy.get("fail-fast"), "false")
        matrix = block_scalars(text, "jobs", python, "strategy", "matrix")
        self.assertEqual(len(matrix), 1, matrix)
        (axis, entries), = matrix.items()
        self.assertEqual(re.findall(r"\d+", entries), ["1", "2"])
        step = next(line for line in jobs[python]
                    if "tests/run_modules.py" in line)
        self.assertIn("--jobs 4", step)
        self.assertIn(f"--shard ${{{{ matrix.{axis} }}}}/2", step)
        self.assertIn("--max-module-sec 90", step)
        console_steps = "\n".join(jobs[console])
        for command in ("install --frozen-lockfile", "run typecheck",
                        "run lint", "run test"):
            self.assertIn(f"bun --cwd=console {command}", console_steps)

    def test_only_the_aggregating_job_is_named_unit_and_jobs_are_capped(self):
        text = WORKFLOW.read_text()
        for job in job_blocks(text):
            with self.subTest(job=job):
                fields = block_scalars(text, "jobs", job)
                if job != "unit":
                    self.assertNotEqual(fields.get("name", job), "unit")
                self.assertEqual(fields.get("runs-on"), "ubuntu-latest")
                self.assertRegex(fields["timeout-minutes"], r"^[0-9]+$")
                self.assertLessEqual(int(fields["timeout-minutes"]), 30)

    def test_unit_step_passes_only_when_both_needed_jobs_succeed(self):
        text = WORKFLOW.read_text()
        script = run_script(job_blocks(text)["unit"])
        env = block_scalars(text, "jobs", "unit", "steps", "env")
        variables = {re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.result \}\}",
                                  value)[1]: name for name, value in env.items()}
        first, second = re.findall(
            r"[\w-]+", block_scalars(text, "jobs", "unit")["needs"])
        for results, passes in ((("success", "success"), True),
                                (("failure", "success"), False),
                                (("success", "cancelled"), False),
                                (("skipped", "success"), False)):
            with self.subTest(results=results):
                given = dict(zip((first, second), results))
                done = subprocess.run(
                    ["sh", "-c", script], capture_output=True, text=True,
                    env=dict(os.environ, **{variables[job]: result
                                            for job, result in given.items()}))
                self.assertEqual(done.returncode == 0, passes, done.stdout)
                for job, result in given.items():
                    if result != "success":
                        self.assertIn(f"{job} did not pass: {result}",
                                      done.stdout)
                    else:
                        self.assertNotIn(job, done.stdout)

if __name__ == "__main__":
    unittest.main()

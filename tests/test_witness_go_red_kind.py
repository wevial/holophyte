"""A red Go witness is an assertion red only when every failed package is."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from holophyte.story.witness import red_kind

GO = shutil.which("go")
GO_MOD = "module example.test/red\n\ngo 1.21\n"
SUBTEST_ERRORF = """package red

import "testing"

func TestA(t *testing.T) {
	t.Run("leaf", func(t *testing.T) { t.Errorf("want 1, got 2") })
}

func TestB(t *testing.T) {}
"""
CALLS_UNDEFINED = """package red

import "testing"

func TestA(t *testing.T) { undefinedFunction() }
"""
WRITES_NIL_MAP = """package red

import "testing"

func TestA(t *testing.T) {
	var m map[string]int
	m["a"] = 1
}
"""
ASSERTION_RED = (
    "=== RUN   TestA\n"
    "=== RUN   TestA/leaf\n"
    "    red_test.go:6: want 1, got 2\n"
    "--- FAIL: TestA (0.00s)\n"
    "    --- FAIL: TestA/leaf (0.00s)\n"
    "=== RUN   TestB\n"
    "--- PASS: TestB (0.00s)\n"
    "FAIL\n"
    "FAIL\texample.test/red\t0.002s\n"
    "FAIL\n")
BUILD_FAILED = (
    "# example.test/red [example.test/red.test]\n"
    "./red_test.go:5:28: undefined: undefinedFunction\n"
    "FAIL\texample.test/red [build failed]\n"
    "FAIL\n")
SETUP_FAILED = (
    "# example.test/red\n"
    "red_test.go:6:2: package nosuchstd is not in std"
    " (/usr/local/go/src/nosuchstd)\n"
    "FAIL\texample.test/red [setup failed]\n"
    "FAIL\n")
TIMED_OUT = (
    "=== RUN   TestA\n"
    "panic: test timed out after 5s\n"
    "\trunning tests:\n"
    "\t\tTestA (5s)\n"
    "\n"
    "goroutine 17 [running]:\n"
    "testing.(*M).startAlarm.func1()\n"
    "\t/usr/local/go/src/testing/testing.go:2802 +0x354\n"
    "created by time.goFunc\n"
    "\t/usr/local/go/src/time/sleep.go:215 +0x2d\n"
    "\n"
    "goroutine 1 [chan receive]:\n"
    "testing.(*T).Run(0x2f8cc17d0008, {0x584fb5?, 0x0?}, 0x592e18)\n"
    "\t/usr/local/go/src/testing/testing.go:2109 +0x4e5\n"
    "testing.runTests.func1(0x2f8cc17d0008)\n"
    "\t/usr/local/go/src/testing/testing.go:2585 +0x3e\n"
    "testing.tRunner(0x2f8cc17d0008, 0x2f8cc1786c58)\n"
    "\t/usr/local/go/src/testing/testing.go:2036 +0xea\n"
    "testing.runTests({0x587ba3, 0x10}, {0x587ba3, 0x10}, 0x2f8cc166e330,"
    " {0x6cb4d0, 0x1, 0x1}, {0xc2a8191319053020, 0x12a073c43, ...})\n"
    "\t/usr/local/go/src/testing/testing.go:2583 +0x505\n"
    "testing.(*M).Run(0x2f8cc178a6e0)\n"
    "\t/usr/local/go/src/testing/testing.go:2443 +0x6ac\n"
    "main.main()\n"
    "\t_testmain.go:46 +0x9b\n"
    "\n"
    "goroutine 6 [sleep]:\n"
    "time.Sleep(0xdf8475800)\n"
    "\t/usr/local/go/src/runtime/time.go:363 +0x165\n"
    "example.test/red.TestA(0x2f8cc17d0248?)\n"
    "\t/tmp/red/red_test.go:5 +0x1d\n"
    "testing.tRunner(0x2f8cc17d0248, 0x592e18)\n"
    "\t/usr/local/go/src/testing/testing.go:2036 +0xea\n"
    "created by testing.(*T).Run in goroutine 1\n"
    "\t/usr/local/go/src/testing/testing.go:2101 +0x4c5\n"
    "FAIL\texample.test/red\t5.009s\n"
    "FAIL\n")
DATA_RACE = (
    "=== RUN   TestA\n"
    "==================\n"
    "WARNING: DATA RACE\n"
    "Write at 0x00c000018278 by goroutine 8:\n"
    "  example.test/red.TestA.func1()\n"
    "      /tmp/red/red_test.go:8 +0x33\n"
    "\n"
    "Previous write at 0x00c000018278 by goroutine 7:\n"
    "  example.test/red.TestA()\n"
    "      /tmp/red/red_test.go:9 +0x104\n"
    "  testing.tRunner()\n"
    "      /usr/local/go/src/testing/testing.go:2036 +0x21c\n"
    "  testing.(*T).Run.gowrap1()\n"
    "      /usr/local/go/src/testing/testing.go:2101 +0x38\n"
    "\n"
    "Goroutine 8 (running) created at:\n"
    "  example.test/red.TestA()\n"
    "      /tmp/red/red_test.go:8 +0xf9\n"
    "  testing.tRunner()\n"
    "      /usr/local/go/src/testing/testing.go:2036 +0x21c\n"
    "  testing.(*T).Run.gowrap1()\n"
    "      /usr/local/go/src/testing/testing.go:2101 +0x38\n"
    "\n"
    "Goroutine 7 (running) created at:\n"
    "  testing.(*T).Run()\n"
    "      /usr/local/go/src/testing/testing.go:2101 +0xb12\n"
    "  testing.runTests.func1()\n"
    "      /usr/local/go/src/testing/testing.go:2585 +0x84\n"
    "  testing.tRunner()\n"
    "      /usr/local/go/src/testing/testing.go:2036 +0x21c\n"
    "  testing.runTests()\n"
    "      /usr/local/go/src/testing/testing.go:2583 +0x9e9\n"
    "  testing.(*M).Run()\n"
    "      /usr/local/go/src/testing/testing.go:2443 +0xf4b\n"
    "  main.main()\n"
    "      _testmain.go:46 +0x164\n"
    "==================\n"
    "    testing.go:1712: race detected during execution of test\n"
    "--- FAIL: TestA (0.00s)\n"
    "FAIL\n"
    "FAIL\texample.test/red\t0.008s\n"
    "FAIL\n")
TEST_MAIN_EXITS_1 = (
    "=== RUN   TestA\n"
    "--- PASS: TestA (0.00s)\n"
    "PASS\n"
    "FAIL\texample.test/red\t0.002s\n"
    "FAIL\n")
ASSERTION_RED_BESIDE_BUILD_FAILED = (
    "# example.test/red/b [example.test/red/b.test]\n"
    "b/red_test.go:5:28: undefined: undefinedFunction\n"
    "=== RUN   TestA\n"
    "=== RUN   TestA/leaf\n"
    "    red_test.go:6: want 1, got 2\n"
    "--- FAIL: TestA (0.00s)\n"
    "    --- FAIL: TestA/leaf (0.00s)\n"
    "=== RUN   TestB\n"
    "--- PASS: TestB (0.00s)\n"
    "FAIL\n"
    "FAIL\texample.test/red/a\t0.002s\n"
    "FAIL\texample.test/red/b [build failed]\n"
    "=== RUN   TestC\n"
    "--- PASS: TestC (0.00s)\n"
    "PASS\n"
    "ok  \texample.test/red/c\t0.002s\n"
    "FAIL\n")
UNITTEST_ASSERTION_RED = (
    "F\n"
    "======================================================================\n"
    "FAIL: test_sorted (test_w1.W1Tests.test_sorted)\n"
    "----------------------------------------------------------------------\n"
    "Traceback (most recent call last):\n"
    '  File "/tmp/w/test_w1.py", line 6, in test_sorted\n'
    "    self.assertEqual(sorted([2, 1]), [2, 1])\n"
    "AssertionError: Lists differ: [1, 2] != [2, 1]\n"
    "\n"
    "First differing element 0:\n"
    "1\n"
    "2\n"
    "\n"
    "- [1, 2]\n"
    "+ [2, 1]\n"
    "\n"
    "----------------------------------------------------------------------\n"
    "Ran 1 test in 0.000s\n"
    "\n"
    "FAILED (failures=1)\n")


def go_test_output(test_source):
    with tempfile.TemporaryDirectory() as tmp:
        module = Path(tmp) / "red"
        module.mkdir()
        (module / "go.mod").write_text(GO_MOD)
        (module / "red_test.go").write_text(test_source)
        env = dict(os.environ, GOTOOLCHAIN="local", GOCACHE=str(Path(tmp) / "cache"),
                   GOFLAGS="", GOWORK="off")
        done = subprocess.run([GO, "test", "-v", "-count=1", "./..."], cwd=module,
                              env=env, capture_output=True, text=True, timeout=300)
    return done.returncode, done.stdout + done.stderr


@unittest.skipUnless(GO, "go is not installed")
class RealGoTestTests(unittest.TestCase):
    def test_a_failing_subtest_beside_a_passing_test_is_an_assertion_red(self):
        code, output = go_test_output(SUBTEST_ERRORF)
        self.assertNotEqual(code, 0, output)
        self.assertEqual(red_kind(output), "assert", output)

    def test_a_build_failure_and_a_nil_map_panic_are_exception_reds(self):
        for name, source in {"undefined function": CALLS_UNDEFINED,
                             "nil map write": WRITES_NIL_MAP}.items():
            with self.subTest(name):
                code, output = go_test_output(source)
                self.assertNotEqual(code, 0, output)
                self.assertEqual(red_kind(output), "exception", output)


class CapturedGoOutputTests(unittest.TestCase):
    def test_a_failed_subtest_with_no_breakage_is_an_assertion_red(self):
        self.assertEqual(red_kind(ASSERTION_RED), "assert")

    def test_a_broken_package_is_an_exception_red(self):
        cases = {"build failed": BUILD_FAILED, "setup failed": SETUP_FAILED,
                 "test timeout": TIMED_OUT, "data race": DATA_RACE,
                 "TestMain exits 1 with every test passing": TEST_MAIN_EXITS_1}
        for name, output in cases.items():
            with self.subTest(name):
                self.assertEqual(red_kind(output), "exception")

    def test_a_mixed_output_is_an_assertion_red_only_when_every_summary_is(self):
        self.assertEqual(red_kind(ASSERTION_RED_BESIDE_BUILD_FAILED), "exception")
        self.assertEqual(red_kind(UNITTEST_ASSERTION_RED + ASSERTION_RED), "assert")


if __name__ == "__main__":
    unittest.main()

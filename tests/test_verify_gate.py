"""Fail-loud contracts for the factory's mechanical verify gate.

Run: python3 -m unittest discover -s tests -p 'test_verify_gate*' -v
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("holophyte_factory", ROOT / "factory.py")
factory = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(factory)


class VerifyClauseDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)
        (self.cwd / "sub").mkdir()
        (self.cwd / "sub" / "value.txt").write_text("payload\n")

    def test_failing_clause_of_a_chain_is_named_with_index_and_status(self):
        ok, out = factory.run_verify(
            "echo first && python3 -c 'import sys; print(\"boom\"); sys.exit(3)' "
            "&& echo never", self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3 exited 3", out)
        self.assertIn("failing clause: python3 -c", out)
        self.assertIn("--- clause 2 (exit 3): python3 -c", out)
        self.assertIn("boom", out)

    def test_executed_clauses_are_shown_and_short_circuited_ones_are_not(self):
        # KO-109 acceptance criteria, verbatim command.
        ok, out = factory.run_verify(
            "printf first && sh -c 'exit 7' && printf never", self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3 exited 7", out)
        self.assertIn("--- clause 1 (ok): printf first", out)
        self.assertIn("first", out)
        self.assertIn("not executed: clause 3", out)
        self.assertNotIn("clause 3 (", out)

    def test_a_clause_that_exits_the_shell_is_still_attributed(self):
        ok, out = factory.run_verify(
            "echo before && exit 7 && echo never", self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3 exited 7", out)
        self.assertIn("failing clause: exit 7", out)
        self.assertIn("not executed: clause 3", out)

    def test_unterminated_clause_output_is_attributed_to_its_own_clause(self):
        # Round-2 review blocker: `printf first` leaves no trailing newline,
        # so the next clause marker glues onto "first" and `boom` used to be
        # credited to clause 1 while failing clause 2 read as silent.
        ok, out = factory.run_verify(
            "printf first && sh -c 'echo boom; exit 7' && printf never",
            self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3 exited 7", out)
        self.assertIn("--- clause 1 (ok): printf first", out)
        self.assertIn("boom", out)
        clause1 = out.split("--- clause 1")[1].split("--- clause 2")[0]
        clause2 = out.split("--- clause 2")[1]
        self.assertNotIn("boom", clause1)
        self.assertIn("boom", clause2)
        self.assertIn("first", clause1)

    def test_silent_clause_failure_is_reported_as_silence(self):
        ok, out = factory.run_verify(
            "echo starting && grep -q absent sub/value.txt", self.cwd)

        self.assertFalse(ok)
        self.assertIn("failing clause: grep -q absent sub/value.txt", out)
        self.assertIn("failed silently", out)

    def test_clauses_run_in_one_shell_so_state_carries_across_them(self):
        ok, out = factory.run_verify("cd sub && cat value.txt", self.cwd)

        self.assertTrue(ok, out)
        self.assertEqual(out, "payload")

    def test_chain_with_a_top_level_or_keeps_its_original_semantics(self):
        ok, out = factory.run_verify(
            "test -f missing.txt && echo found || echo recovered", self.cwd)

        self.assertTrue(ok, out)
        self.assertIn("recovered", out)

    def test_and_inside_a_trailing_comment_is_not_an_operator(self):
        ok, out = factory.run_verify("true # && false", self.cwd)

        self.assertTrue(ok, out)

    def test_a_trailing_comment_after_a_real_chain_stays_with_its_clause(self):
        ok, out = factory.run_verify(
            "echo one && grep -q absent sub/value.txt # why", self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 2 exited 1", out)
        self.assertIn("failing clause: grep -q absent sub/value.txt # why", out)


class VacuousGreenTests(unittest.TestCase):
    """An exit-0 test command that collected no tests verified nothing (KO-107:
    a discovery pattern matching no file reported a green gate)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)
        (self.cwd / "suite").mkdir()
        (self.cwd / "suite" / "test_real.py").write_text(
            "import unittest\n\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n")

    def test_unittest_discovery_that_ran_no_tests_is_red(self):
        # Real discovery against a pattern no file matches: exits 0, "OK".
        ok, out = factory.run_verify(
            "python3 -m unittest discover -s suite -p 'test_absent*'", self.cwd)

        self.assertFalse(ok)
        self.assertIn("vacuous-green", out)
        self.assertIn("Ran 0 tests", out)

    def test_pytest_collecting_no_items_is_red(self):
        ok, out = factory.run_verify(
            "printf '%s\\n' 'collected 0 items' 'no tests ran in 0.01s'",
            self.cwd)

        self.assertFalse(ok)
        self.assertIn("vacuous-green", out)
        self.assertIn("zero-test summary: collected 0 items", out)

    def test_unittest_run_that_executed_tests_stays_green(self):
        ok, out = factory.run_verify(
            "python3 -m unittest discover -s suite -p 'test_real*'", self.cwd)

        self.assertTrue(ok, out)
        self.assertIn("Ran 1 test", out)

    def test_pytest_collecting_items_stays_green(self):
        ok, out = factory.run_verify(
            "printf '%s\\n' 'collected 10 items' '10 passed in 0.4s'",
            self.cwd)

        self.assertTrue(ok, out)


if __name__ == "__main__":
    unittest.main()

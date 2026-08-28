"""Fail-loud contracts for the factory's mechanical verify gate.

Run: python3 -m unittest discover -s tests -p 'test_factory_verify*' -v
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
        self.assertEqual(out.split("clause output:\n")[1], "boom")

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


if __name__ == "__main__":
    unittest.main()

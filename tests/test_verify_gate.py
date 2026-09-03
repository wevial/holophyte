"""Fail-loud contracts for the factory's mechanical verify gate.

Run: python3 -m unittest discover -s tests -p 'test_verify_gate*' -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports ticket_template by name
# `waiting` is a helper, not a test module: discovery never imports it, and
# how this file is imported decides whether `tests/` is on the path at all.
sys.path.insert(0, str(HERE))

from waiting import wait_for  # noqa: E402 - after the sys.path insert above

import holophyte.gates  # noqa: E402 - after the sys.path insert above


class VerifyClauseDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)
        (self.cwd / "sub").mkdir()
        (self.cwd / "sub" / "value.txt").write_text("payload\n")

    def test_failing_clause_of_a_chain_is_named_with_index_and_status(self):
        ok, out = holophyte.gates.run_verify(
            "echo first && python3 -c 'import sys; print(\"boom\"); sys.exit(3)' "
            "&& echo never", self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3 exited 3", out)
        self.assertIn("failing clause: python3 -c", out)
        self.assertIn("--- clause 2 (exit 3): python3 -c", out)
        self.assertIn("boom", out)

    def test_executed_clauses_are_shown_and_short_circuited_ones_are_not(self):
        # KO-109 acceptance criteria, verbatim command.
        ok, out = holophyte.gates.run_verify(
            "printf first && sh -c 'exit 7' && printf never", self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3 exited 7", out)
        self.assertIn("--- clause 1 (ok): printf first", out)
        self.assertIn("first", out)
        self.assertIn("not executed: clause 3", out)
        self.assertNotIn("clause 3 (", out)

    def test_a_clause_that_exits_the_shell_is_still_attributed(self):
        ok, out = holophyte.gates.run_verify(
            "echo before && exit 7 && echo never", self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 3 exited 7", out)
        self.assertIn("failing clause: exit 7", out)
        self.assertIn("not executed: clause 3", out)

    def test_unterminated_clause_output_is_attributed_to_its_own_clause(self):
        # Round-2 review blocker: `printf first` leaves no trailing newline,
        # so the next clause marker glues onto "first" and `boom` used to be
        # credited to clause 1 while failing clause 2 read as silent.
        ok, out = holophyte.gates.run_verify(
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
        ok, out = holophyte.gates.run_verify(
            "echo starting && grep -q absent sub/value.txt", self.cwd)

        self.assertFalse(ok)
        self.assertIn("failing clause: grep -q absent sub/value.txt", out)
        self.assertIn("failed silently", out)

    def test_clauses_run_in_one_shell_so_state_carries_across_them(self):
        ok, out = holophyte.gates.run_verify("cd sub && cat value.txt", self.cwd)

        self.assertTrue(ok, out)
        self.assertEqual(out, "payload")

    def test_chain_with_a_top_level_or_keeps_its_original_semantics(self):
        ok, out = holophyte.gates.run_verify(
            "test -f missing.txt && echo found || echo recovered", self.cwd)

        self.assertTrue(ok, out)
        self.assertIn("recovered", out)

    def test_and_inside_a_trailing_comment_is_not_an_operator(self):
        ok, out = holophyte.gates.run_verify("true # && false", self.cwd)

        self.assertTrue(ok, out)

    def test_a_trailing_comment_after_a_real_chain_stays_with_its_clause(self):
        ok, out = holophyte.gates.run_verify(
            "echo one && grep -q absent sub/value.txt # why", self.cwd)

        self.assertFalse(ok)
        self.assertIn("clause 2 of 2 exited 1", out)
        self.assertIn("failing clause: grep -q absent sub/value.txt # why", out)


class VerifyTimeoutTests(unittest.TestCase):
    """Real caps against real process trees; nothing here is mocked.

    Margins: a 0.3 s or 0.5 s cap lost to the shell's own startup whenever
    another suite or a review ran alongside, so the pre-cap output the tests
    look for was never printed and green code went red. Caps are 1 s; a child
    that must outlive the cap sleeps 3 s; a wait that proves a child is gone
    runs past that delay; where a test reads pre-cap output out of the report,
    the shell also touches a `started` marker, so a machine too loaded to get
    the shell to its first line is named as such rather than as lost output.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)

    def test_a_chain_that_hits_the_cap_is_red_and_names_the_running_clause(self):
        # KO acceptance criterion, verbatim command: the cap is a failed
        # verify, not a `TimeoutExpired` escaping into the loop.
        # The command is kept verbatim (a `touch` clause would change the
        # clause count the assertions name), so the 1 s cap is the margin.
        with patch.object(holophyte.gates, "VERIFY_TIMEOUT", 1.0):
            ok, out = holophyte.gates.run_verify("echo a && sleep 5", self.cwd)

        self.assertFalse(ok)
        self.assertIn("timed out after 1s", out)
        self.assertIn("clause 2 of 2", out)
        self.assertIn("running clause: sleep 5", out)
        self.assertIn("--- clause 1 (ok): echo a", out)  # what got that far

    def test_the_cap_still_reaps_the_command_s_process_group(self):
        started = self.cwd / "started.txt"
        escaped = self.cwd / "escaped.txt"
        cmd = ("echo resolving; touch %s; (sleep 3; touch %s) & sleep 5"
               % (started, escaped))

        with patch.object(holophyte.gates, "VERIFY_TIMEOUT", 1.0):
            ok, out = holophyte.gates.run_verify(cmd, self.cwd)

        self.assertFalse(ok)
        self.assertIn("timed out after 1s", out)
        self.assertTrue(wait_for(started.exists, 5.0),
                        "the shell did not reach its first line inside the 1s "
                        "cap: this machine is too loaded to time this run")
        self.assertIn("resolving", out)  # what it said before the cap
        self.assertFalse(wait_for(escaped.exists, 3.5),
                         "a child of the timed-out command outlived the cap")

    def test_a_detached_descendant_holding_the_pipe_still_yields_a_report(self):
        # Review finding on the cap: a grandchild in its own session survives
        # the group kill and holds the output pipe, so `reap_group` falls back
        # to the partial output CPython attached to `TimeoutExpired` -- which
        # is `bytes` even under `text=True`. That must not turn the timeout
        # report into a `TypeError`.
        # The grandchild sleeps 3 s so that it is still holding the pipe when
        # the cap (1 s) plus the grace (0.1 s) runs out: a `sleep 1` could
        # exit first and let `communicate` return normally, and the fallback
        # this test exists for would never run.
        started = self.cwd / "started.txt"
        cmd = ("echo before; touch %s; setsid sh -c 'sleep 3' & sleep 5"
               % started)

        with patch.object(holophyte.gates, "VERIFY_TIMEOUT", 1.0), \
                patch.object(holophyte.gates, "REAP_GRACE", 0.1):
            ok, out = holophyte.gates.run_verify(cmd, self.cwd)

        self.assertFalse(ok)
        self.assertIn("timed out after 1s", out)
        self.assertTrue(wait_for(started.exists, 5.0),
                        "the shell did not reach its first line inside the 1s "
                        "cap: this machine is too loaded to time this run")
        self.assertIn("before", out)


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
        # Real discovery against a pattern no file matches. On Python < 3.12
        # unittest exits 0 with "OK" and the vacuous-green detector must catch
        # it; from 3.12 unittest itself exits 5, a plain failure. Either way
        # the gate is RED with the zero-test evidence visible — that is the
        # KO-107 property; the detector's report shape is pinned by the
        # exit-0 test below.
        ok, out = holophyte.gates.run_verify(
            "python3 -m unittest discover -s suite -p 'test_absent*'", self.cwd)

        self.assertFalse(ok)
        self.assertIn("Ran 0 tests", out)

    def test_pytest_collecting_no_items_is_red(self):
        ok, out = holophyte.gates.run_verify(
            "printf '%s\\n' 'collected 0 items' 'no tests ran in 0.01s'",
            self.cwd)

        self.assertFalse(ok)
        self.assertIn("vacuous-green", out)
        self.assertIn("zero-test summary: collected 0 items", out)

    def test_unittest_run_that_executed_tests_stays_green(self):
        ok, out = holophyte.gates.run_verify(
            "python3 -m unittest discover -s suite -p 'test_real*'", self.cwd)

        self.assertTrue(ok, out)
        self.assertIn("Ran 1 test", out)

    def test_pytest_collecting_items_stays_green(self):
        ok, out = holophyte.gates.run_verify(
            "printf '%s\\n' 'collected 10 items' '10 passed in 0.4s'",
            self.cwd)

        self.assertTrue(ok, out)


class ContractCheckTests(unittest.TestCase):
    """Literal contract assertions declared on the ticket (KO-106 drifted from
    its required port 8622 while every command still exited 0)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = Path(tmp.name)
        (self.cwd / "config").mkdir()
        self.conf = self.cwd / "config" / "tunnel.yml"
        self.conf.write_text("service: http://localhost:8622\n")
        self.contracts = [("config/tunnel.yml", "8622")]

    def test_declared_literal_present_keeps_the_gate_green(self):
        ok, out = holophyte.gates.run_verify("printf 'suite ok\n'", self.cwd,
                                     self.contracts)

        self.assertTrue(ok, out)
        self.assertIn("contract checks passed: 1", out)
        self.assertIn("suite ok", out)

    def test_drifted_literal_is_red_and_names_path_and_literal(self):
        self.conf.write_text("service: http://localhost:8000\n")

        # The command itself still exits 0 — only the contract has drifted.
        ok, out = holophyte.gates.run_verify("printf 'suite ok\n'", self.cwd,
                                     self.contracts)

        self.assertFalse(ok)
        self.assertIn("contract check", out)
        self.assertIn("config/tunnel.yml", out)
        self.assertIn("expected literal: 8622", out)

    def test_drift_report_does_not_echo_the_checked_file(self):
        # A declared file may hold credentials and this report reaches the
        # reviewer, so the path and the missing literal are all it may carry.
        self.conf.write_text("token: hunter2-do-not-log\nport: 8000\n")

        ok, out = holophyte.gates.run_verify("printf 'suite ok\n'", self.cwd,
                                     self.contracts)

        self.assertFalse(ok)
        self.assertNotIn("hunter2-do-not-log", out)
        self.assertNotIn("token", out)

    def test_ticket_without_contract_checks_is_unaffected(self):
        self.conf.write_text("service: http://localhost:8000\n")

        ok, out = holophyte.gates.run_verify("printf 'suite ok\n'", self.cwd)

        self.assertTrue(ok, out)
        self.assertNotIn("contract check", out)

    def test_declared_file_that_does_not_exist_is_red(self):
        ok, out = holophyte.gates.run_verify("printf 'suite ok\n'", self.cwd,
                                     [("config/gone.yml", "8622")])

        self.assertFalse(ok)
        self.assertIn("does not exist", out)
        self.assertIn("config/gone.yml", out)

    def test_absolute_declared_path_is_refused_rather_than_read(self):
        ok, out = holophyte.gates.run_verify("printf 'suite ok\n'", self.cwd,
                                     [("/etc/hostname", "8622")])

        self.assertFalse(ok)
        self.assertIn("must be relative", out)


class TaskExtractionTests(unittest.TestCase):
    """linear_provider hands the parsed declarations to the gate."""

    @classmethod
    def setUpClass(cls):
        # linear_provider refuses to import without a configured project.
        os.environ.setdefault("HOLO2_PROJECT_ID", "test-project")
        import linear_provider
        cls.provider = linear_provider

    def issue(self, description):
        return {"identifier": "KO-1", "id": "uuid-of-ko-1", "title": "t",
                "estimate": 30, "description": description}

    def test_contract_checks_section_reaches_the_task(self):
        task = self.provider.parse_task(self.issue(
            "# T\n\n## Verify command(s)\n\n```\npytest -q\n```\n\n"
            "## Contract checks\n\n```\nconfig/tunnel.yml: 8622\n```\n"))

        self.assertEqual(task["verify"], "pytest -q")
        self.assertEqual(task["contracts"], [("config/tunnel.yml", "8622")])

    def test_the_canonical_issue_uuid_reaches_the_task(self):
        """The store keys its mirror on the UUID, so parsing may not drop it."""
        task = self.provider.parse_task(self.issue("# T\n"))

        self.assertEqual((task["id"], task["issue_id"]),
                         ("KO-1", "uuid-of-ko-1"))

    def test_the_full_description_reaches_the_task_as_its_body(self):
        """The implementer's contract is the body, so parsing may not drop it."""
        description = ("# T\n\n## Acceptance criteria\n\n"
                       "- [ ] The word used is `unparseable`.\n\n"
                       "## Verify command(s)\n\n```\npytest -q\n```\n")

        task = self.provider.parse_task(self.issue(description))

        self.assertEqual(task["body"], description)

    def test_ticket_without_the_section_declares_no_contracts(self):
        task = self.provider.parse_task(self.issue(
            "# T\n\n## Verify command(s)\n\n```\npytest -q\n```\n"))

        self.assertEqual(task["contracts"], [])


if __name__ == "__main__":
    unittest.main()

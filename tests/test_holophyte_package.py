"""The `holophyte` package: each moved name is defined where the split says.

Phase 2 moves `factory.py` into the package one section at a time, and
`factory.py` imports the moved names back for the call sites still in it. The
re-export is what keeps the tests and the loop running between slices; this
test is what keeps it a re-export: a name listed here that is defined in
`factory.py` again, or in the wrong module, fails naming the name. The
companion check -- that `factory.py` no longer holds the `def`/`class` lines
-- is the slice's verify grep.

Run: python3 -m unittest discover -s tests -p 'test_holophyte_package*' -v
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # the package and its `review_runner` import

import holophyte.agents  # noqa: E402 - after the sys.path insert above
import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.config  # noqa: E402 - after the sys.path insert above
import holophyte.findings  # noqa: E402 - after the sys.path insert above
import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.report  # noqa: E402 - after the sys.path insert above
import holophyte.review  # noqa: E402 - after the sys.path insert above
import holophyte.runs  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above

# The functions and classes each module owns after the slices so far; constants
# carry no `__module__`, so they are not listed. Edit these lists in the same
# change that moves a name, and say why in the commit.
DEFINED = {
    holophyte.target: [
        "Target",
        "adopt_legacy_state",
        "legacy_state_layouts",
        "state_dir",
    ],
    holophyte.config: [
        "LoopConfig",
        "SweepConfig",
        "agent_command",
        "check_agent_commands",
        "check_config_keys",
        "check_default_implementer",
        "check_default_reviewer",
        "docker_probe",
        "load_config",
        "loop_config",
        "setup_commands",
        "setup_timeout",
        "sweep_config",
    ],
    holophyte.gates: [
        "InfraFailure",
        "RunFailure",
        "contract_report",
        "failure_report",
        "instrumented_script",
        "outcome_class_of",
        "parse_clause_output",
        "parse_task",
        "reap_group",
        "run_capped",
        "run_verify",
        "sh",
        "split_and_clauses",
        "timeout_failure_report",
        "vacuous_green_report",
    ],
    holophyte.agents: [
        "agent",
        "agent_route",
        "publish_review_refs",
    ],
    holophyte.findings: [
        "_document",
        "_entry",
        "_gist",
        "_ms",
        "_stamp",
        "commit_findings",
        "finding_line",
        "findings_entries",
        "frozen_preamble",
        "refresh_findings",
        "render_findings",
        "round_at",
        "round_entry",
        "run_entry",
        "write_findings",
    ],
    holophyte.report: [
        "format_age",
        "host_name",
        "report_lines",
        "report_rows",
        "report_summary",
    ],
    holophyte.review: [
        "_trailing_verdict",
        "criteria_block",
        "criteria_brief",
        "criteria_findings",
        "finding_blocks",
        "finding_message",
        "finding_severity",
        "parse_findings",
        "raw_finding",
        "round_verdict",
        "sanitize_findings",
        "unparsed_path",
    ],
    holophyte.runs: [
        "open_store",
        "record_round",
        "set_phase",
        "warn_on_run",
    ],
    holophyte.board: [
        "body_problem",
        "close_out_failure",
        "escalate",
        "escalation_comment",
        "failure_history",
        "ledger",
        "merge_drift",
        "mirror_key",
        "mirror_push",
        "mirror_status",
        "mirror_task",
        "release_run",
        "store_status",
        "task_contract",
        "warn",
    ],
}


class MovedNamesTests(unittest.TestCase):

    def test_each_moved_name_is_defined_in_its_new_module(self):
        for module, names in DEFINED.items():
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertEqual(getattr(module, name).__module__,
                                     module.__name__)


if __name__ == "__main__":
    unittest.main()

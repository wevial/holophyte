"""The `holophyte` package: each moved name is defined where the split says.

Phase 2 moved `factory.py` into the package one section at a time; with the
last slice `factory.py` is the entry point and nothing else. A name listed
here that is defined in the wrong module fails naming the name, and a
function or class defined in `factory.py` again fails naming it too. The
companion check -- that `factory.py` holds no `def`/`class` lines -- is the
verify grep.

Run: python3 -m unittest discover -s tests -p 'test_holophyte_package*' -v
"""
import importlib.util
import inspect
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # the package and its `review_runner` import


def _module(name):
    """`holophyte.<name>`, imported where the registry names it."""
    return importlib.import_module(f"holophyte.{name}")


FACTORY = ROOT / "factory.py"

# The functions and classes each module owns after the slices so far; constants
# carry no `__module__`, so they are not listed. Edit these lists in the same
# change that moves a name, and say why in the commit.
DEFINED = {
    _module("run"): ["Run", "land"],
    _module("project"): [
        "Project",
        "adopt_legacy_state",
        "legacy_state_layouts",
        "state_dir",
    ],
    _module("config"): [
        "agent_command",
        "carry_directories",
        "check_agent_commands",
        "check_config_keys",
        "check_default_implementer",
        "check_default_reviewer",
        "check_worktree_setup",
        "docker_probe",
        "load_config",
        "setup_commands",
        "setup_timeout",
    ],
    # KO-397: the per-table readers, out of `holophyte.config`.
    _module("config_tables"): [
        "LoopConfig", "ReportConfig", "SweepConfig", "loop_config",
        "report_config", "sweep_config",
    ],
    _module("gates"): [
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
    _module("agents"): [
        "ProbeResult",
        "agent",
        "agent_route",
        "probe_implementer",
        "publish_review_refs",
    ],
    _module("findings"): [
        "_entry",
        "_gist",
        "_ms",
        "_stamp",
        "commit_findings",
        "decode_findings",
        "finding_line",
        "findings_entries",
        "findings_off",
        "frozen_preamble",
        "refresh_findings",
        "render_findings",
        "round_at",
        "round_entry",
        "run_entry",
        "write_findings",
    ],
    _module("report"): [
        "format_age",
        "host_label",
        "host_name",
        "report_lines",
        "report_rows",
        "report_summary",
    ],
    _module("review"): [
        "_trailing_verdict",
        "criteria_block",
        "criteria_brief",
        "criteria_findings",
        "finding_blocks",
        "finding_message",
        "finding_severity",
        "missing_witnesses",
        "parse_findings",
        "raw_finding",
        "round_verdict",
        "sanitize_findings",
        "test_references",
        "unparsed_path",
    ],
    _module("runs"): [
        "heartbeat_while",
        "open_store",
        "record_round",
        "review_round_cap",
        "set_phase",
        "warn_on_run",
    ],
    _module("board"): [
        "body_problem",
        "close_out_failure",
        "escalate",
        "escalation_comment",
        "failure_history",
        "file_ticket",
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
    # The three namedtuples are classes made in the module, so they carry a
    # `__module__` like any `class` statement and are listed with the rest.
    _module("supervisor"): [
        "Outcome",
        "Sweep",
        "Trip",
        "act_on_trip",
        "review_overlap",
        "still_tripped",
        "supervise",
        "supervise_pass",
        "supervisor_liveness_line",
        "sweep",
    ],
    # KO-396: the sweep's report lines and the supervisor lock, out of
    # `holophyte.supervisor`.
    _module("sweep_report"): [
        "_runs",
        "merge_lock_lines",
        "restart_lines",
        "review_container_lines",
        "run_lines",
        "sweep_lines",
        "sweep_report",
    ],
    _module("supervisor_lock"): [
        "SupervisorHeld",
        "acquire_supervisor_lock",
        "pid_alive",
        "read_supervisor_lock",
        "reclaim_turn",
        "release_supervisor_lock",
        "supervisor_lock_path",
        "supervisor_running",
    ],
    _module("loop"): ["run_task"],
    # KO-390: the operator commands and the entry point, out of `holophyte.loop`.
    _module("operator"): ["main", "report", "self_hosted"],
    # KO-389: the claim and the worktree cut, out of `holophyte.loop`.
    _module("claim"): [
        "_Held", "_admit_ticket", "_claim_next", "_claim_run",
        "_cut_worktree", "_lease_on_board", "_park_unlisted", "_refresh_main",
        "_refuse_claim", "_resolve_merge_conflict", "_setup_worktree",
        "_skip_held", "conflict_brief", "merge_conflicts", "reuse_leftover",
        "run_worktree_setup", "skip_line", "timeout_report",
    ],
    _module("cli"): ["cli"],
    # KO-385: the pull-request stage, split out of `holophyte.loop`.
    _module("pullrequest"): [
        "_landed_pr",
        "_merge_pr",
        "_park_human",
        "_park_on_pr",
        "_pr_template",
        "_prepare_pr",
        "_push_and_open",
        "_resume_on_pr",
        "_written_pr_text",
    ],
    # KO-386: the babysit pass joins the texts it drives.
    _module("pr_head"): ["_pr_terminal"],
    _module("babysitter"): [
        "_answer_threads", "_decline_threads",
        "_babysit",
        "_fix_threads",
        "_merge_origin_main",
        "_moved",
        "_next_round",
        "_post",
        "_review_fix",
        "_settled_state",
        "_verdicts_by_kind",
        "addressed_reply",
        "adjudication_brief",
        "conversation",
        "conventions",
        "conventions_paragraph",
        "declined_reply",
        "fix_brief",
        "gist",
        "open_threads_question",
        "parse_summaries",
        "parse_verdicts",
        "people_paragraph",
        "quoted",
        "round_reply",
        "route_of",
        "thread_line",
        "where",
    ],
    # KO-259: the one GitHub surface, `[merge] mode = "pr"`'s push and PR.
    _module("pr"): [
        "check_pr_route",
        "create_pull_request",
        "origin_url",
        "pr_body_stub",
        "pr_title",
        "push_branch",
        "repo_of",
        "token_from_env",
    ],
    # KO-426: reading a pull request's state, out of `holophyte.pr`.
    _module("pr_status"): [
        "PullStatus",
        "_check_reads",
        "_check_runs_of",
        "_comment_nodes",
        "_comments_of",
        "_head_checks",
        "_pull_request_page",
        "_required_contexts",
        "_state_of",
        "fold_checks",
        "parse_pr_url",
        "pr_state",
        "pull_status",
    ],
    # KO-388: the worker pool and its scheduler, out of `holophyte.loop`.
    _module("pool"): [
        "_PoolState",
        "_PrefixedOut",
        "_claimable",
        "_render_findings_locked",
        "_spawn_worker",
        "_wait_any",
        "scheduler",
        "worker",
    ],
    # KO-387: the startup reconciles and the GitHub budget, split out of
    # `holophyte.loop` (`_pr_seen` out of `holophyte.pullrequest`).
    _module("reconcile"): [
        "GitHubBudget",
        "_budget_low",
        "_iso_epoch",
        "_land_github_merge",
        "_note_closed_pr",
        "_parked_phase",
        "_parked_pull_request",
        "_pr_seen",
        "_rebabysit",
        "_reconcile_at_startup",
        "_reconcile_mirror",
        "_reconcile_pull_requests",
        "_seen",
    ],
}


class MediaStartupTests(unittest.TestCase):
    def test_missing_bucket_credentials_warn_once_before_probes(self):
        from holophyte import operator
        from tests.test_media_store import CREDS

        bucket = {"endpoint": "https://objects.example.invalid", "bucket": "evidence",
                  "public_base": "https://media.example.invalid"}
        target = SimpleNamespace(config=lambda: {"merge": {"media_bucket": bucket}},
                                 config_path=Path("config.toml"))
        for missing in (tuple(CREDS), (tuple(CREDS)[1],), ()):
            with self.subTest(missing=missing), patch.dict(os.environ, CREDS):
                for name in missing:
                    os.environ.pop(name, None)
                out = io.StringIO()

                def probe(*args, **kwargs):
                    print("probes reached")
                    return False

                with patch("sys.stdout", out), patch.object(
                        operator, "startup_routes", side_effect=probe) as probes:
                    self.assertEqual(operator.main(target, None), 1)
                probes.assert_called_once()
                warnings = [line for line in out.getvalue().splitlines()
                            if "warning:" in line]
                expected = (["[holo2] warning: media_bucket is configured but "
                             f"{missing[0]} is not set; evidence will not be published"]
                            if missing else [])
                self.assertEqual(warnings, expected)
                self.assertTrue(out.getvalue().endswith("probes reached\n"))
                for value in CREDS.values():
                    self.assertNotIn(value, out.getvalue())


class MovedNamesTests(unittest.TestCase):

    def test_landing_is_owned_by_run(self):
        self.assertFalse(hasattr(_module("loop"), "_land"))


    def test_each_moved_name_is_defined_in_its_new_module(self):
        for module, names in DEFINED.items():
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertEqual(getattr(module, name).__module__,
                                     module.__name__)

    def test_factory_defines_no_function_or_class_of_its_own(self):
        # The entry point, loaded by path the way `python3 factory.py` runs
        # it: every function and class reachable from it was defined
        # somewhere in the package. `cli` itself is the one imported name.
        spec = importlib.util.spec_from_file_location("holophyte_factory",
                                                      FACTORY)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        own = sorted(
            name for name, value in vars(entry).items()
            if not name.startswith("_")
            and (inspect.isfunction(value) or inspect.isclass(value))
            and value.__module__ == entry.__name__)
        self.assertEqual(own, [])
        self.assertIs(entry.cli, _module("cli").cli)


class ProjectTypeTests(unittest.TestCase):
    """KO-619: the project's type is `Project` in `holophyte.project`, with
    no shim left at the old module path."""

    # Spelled in pieces so the grep below does not find this file.
    OLD_TYPE = "Tar" "get"
    OLD_MODULE = OLD_TYPE.lower()

    def test_the_old_module_path_no_longer_imports(self):
        with self.assertRaises(ModuleNotFoundError):
            _module(self.OLD_MODULE)

    def test_the_old_names_are_gone_from_the_code_and_the_manual(self):
        # The module is named as Python names it -- imported, or with an
        # attribute after it -- so the systemd unit `holophyte.target`
        # (deploy/, consolidation stage 4) is not taken for it.
        module = rf"holophyte\.{self.OLD_MODULE}"
        found = subprocess.run(
            ["git", "grep", "-nwE",
             "-e", self.OLD_TYPE,
             "-e", rf"(from|import) {module}",
             "-e", rf"{module}\.[A-Za-z_]+",
             "-e", f"holophyte/{self.OLD_MODULE}\\.py",
             "--", "holophyte", "tests", "store", "factory.py", "README.md",
             "AGENTS.md", "docs", ":!docs/design"],
            cwd=ROOT, capture_output=True, text=True)
        # Exit 1 is "no match"; anything else is git failing, not a pass.
        self.assertIn(found.returncode, (0, 1), found.stderr)
        self.assertEqual(found.stdout.splitlines(), [])


if __name__ == "__main__":
    unittest.main()

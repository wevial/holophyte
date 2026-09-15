"""KO-384: the module-size ratchet — a ceiling table that only moves down.

Nothing else stops a module from growing; this file is the rule that runs
with the suite. `CEILING` sets the caps — 1000 lines a source module,
1500 a test module — and `OVER` holds every tracked Python file over its
cap. The walk fails the suite when a listed file grows past its entry,
when an unlisted file passes its ceiling, and when a listed file is back
under the ceiling — a stale entry. `PINNED` holds a file a slice brought
back under its ceiling at the tighter size the slice left it. A second
check holds the table to exactly what `wc -l` measures — membership and
counts — so an entry is the file's current count and the table can only
shrink.

Run: python3 -m unittest discover -s tests -p 'test_file_sizes*' -v
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CEILING = {"source": 1000, "test": 1500}

# Repo-relative path to the file's `wc -l` count. A slice that changes
# a listed file's size rewrites its entry in the same commit; a file
# back under its ceiling leaves the table.
OVER = {}

# Repo-relative path to the file's `wc -l` count, for a file a slice
# brought back under its ceiling: the pin caps it at the size the slice
# left it, so the table only moves down for that file too.
PINNED = {
    "holophyte/babysitter.py": 836, "holophyte/board.py": 870,
    "holophyte/claim.py": 815, "holophyte/cli.py": 460,
    "holophyte/config.py": 752, "holophyte/dispatch.py": 262,
    "holophyte/config_tables.py": 571, "holophyte/findings.py": 331,
    "holophyte/gates.py": 664, "holophyte/loop.py": 893,
    "holophyte/merge_gate.py": 493,
    "holophyte/operator.py": 447, "holophyte/pool.py": 387,
    "holophyte/pr.py": 620, "holophyte/pr_status.py": 449,
    "holophyte/pullrequest.py": 312,
    "holophyte/reconcile.py": 468, "holophyte/reexec.py": 94,
    "holophyte/report.py": 160, "provider.py": 302,
    "holophyte/serve.py": 889,
    "holophyte/serve_actions.py": 169, "holophyte/serve_config.py": 336,
    "holophyte/serve_runs.py": 507, "holophyte/supervisor.py": 928,
    "holophyte/supervisor_lock.py": 227,
    "holophyte/sweep_report.py": 236, "store/__init__.py": 975,
    "store/operate.py": 923, "store/read.py": 930,
    "store/schema.py": 753, "store/tickets.py": 460,
    "tests/config_fixture.py": 76, "tests/loop_fixture.py": 644,
    "tests/serve_fixture.py": 170, "tests/test_babysit_pass.py": 856,
    "tests/test_babysitter.py": 466, "tests/test_config_tables.py": 487,
    "tests/test_claim.py": 1468, "tests/test_claim_mirror.py": 178,
    "tests/test_cli.py": 114,
    "tests/test_cli_approve.py": 269, "tests/test_cli_requeue.py": 214,
    "tests/test_file_sizes.py": 204, "tests/test_holophyte_package.py": 322,
    "tests/test_factory_config.py": 1073,
    "tests/test_factory_loop.py": 1230, "tests/test_merge_gate.py": 831,
    "tests/test_pool.py": 865, "tests/test_provider.py": 658, "tests/test_runs.py": 55,
    "tests/test_pullrequest.py": 1077, "tests/test_reconcile.py": 414,
    "tests/test_serve.py": 1130,
    "tests/test_serve_actions.py": 313, "tests/test_serve_config.py": 677,
    "tests/test_serve_ledger.py": 449, "tests/test_serve_runs.py": 552,
    "tests/test_serve_shipped.py": 181,
    "tests/sweep_fixture.py": 201, "tests/test_startup_checks.py": 648,
    "tests/test_store.py": 249,
    "tests/test_store_claim.py": 261, "tests/test_store_heartbeat.py": 64,
    "tests/test_store_interventions.py": 403,
    "tests/test_store_lease.py": 80, "tests/test_store_pickable.py": 121,
    "tests/test_store_read.py": 337, "tests/test_store_resume.py": 203,
    "tests/test_store_schema.py": 1017, "tests/test_store_status.py": 312,
    "tests/test_store_status_graph.py": 111,
    "tests/test_store_surface.py": 267, "tests/test_store_tickets.py": 118,
    "tests/test_supervise.py": 1299, "tests/test_supervisor_sweep.py": 981,
    "tests/test_wiring_claim.py": 636, "tests/test_wiring_findings.py": 489,
    "tests/test_wiring_mirror.py": 289, "tests/test_wiring_phases.py": 441,
    "tests/test_wiring_rounds.py": 597,
    "tests/test_wiring_telemetry.py": 449,
    "tests/test_worktree_reuse.py": 239,
}


def line_count(path):
    """Newline count; a file with no trailing newline counts its last line."""
    text = path.read_text()
    return text.count("\n") + bool(text and not text.endswith("\n"))


def tracked_counts(root=ROOT):
    """Repo-relative path to line count for every tracked Python file."""
    names = subprocess.check_output(
        ["git", "ls-files", "*.py"], cwd=root, text=True).splitlines()
    return {name: line_count(root / name) for name in sorted(names)}


def wc_counts(root=ROOT):
    """Every tracked Python file's line count the way the table was
    measured: `wc -l`, one subprocess for the lot."""
    names = subprocess.check_output(
        ["git", "ls-files", "*.py"], cwd=root, text=True).splitlines()
    out = subprocess.check_output(
        ["wc", "-l", *sorted(names)], cwd=root, text=True)
    counts = {}
    for line in out.splitlines():
        number, name = line.split(None, 1)
        if name != "total":
            counts[name] = int(number)
    return counts


def expected_over(counts, ceiling=CEILING):
    """The table `wc -l` says this tree needs: each tracked file over
    its ceiling at exactly its measured count."""
    return {
        name: lines for name, lines in counts.items()
        if lines > ceiling["test" if name.startswith("tests/") else "source"]
    }


def violations(counts, over=OVER, ceiling=CEILING, pinned=PINNED):
    """The ratchet's four rules as failure lines: a listed file over its
    entry, an unlisted file over its ceiling, a pinned file over its pin,
    an entry whose file is not over the ceiling — stale."""
    bad = []
    for name, lines in sorted(counts.items()):
        cap = ceiling["test" if name.startswith("tests/") else "source"]
        if name in over:
            if lines > over[name]:
                bad.append(f"{name}: {lines} lines is over its ratchet "
                           f"entry of {over[name]}")
            elif lines <= cap:
                bad.append(f"{name}: {lines} lines is under the {cap}-line "
                           f"ceiling; the table entry is stale — delete it")
        elif lines > cap:
            bad.append(f"{name}: {lines} lines is over the {cap}-line "
                       f"ceiling with no table entry")
        pin = pinned.get(name)
        if pin is not None and lines > pin:
            bad.append(f"{name}: {lines} lines is over its pinned entry "
                       f"of {pin}")
    for name in sorted((set(over) | set(pinned)) - set(counts)):
        bad.append(f"{name}: no longer a tracked file; the table entry "
                   f"is stale — delete it")
    return bad


class FileSizeRatchet(unittest.TestCase):

    def test_every_tracked_python_file_holds_its_ceiling_or_entry(self):
        self.assertEqual(violations(tracked_counts()), [])

    def test_the_table_is_exactly_what_wc_l_measures(self):
        """The acceptance witness: the table's membership and counts are
        `wc -l`'s, so an inflated or stale entry fails like a missing
        one."""
        counts = wc_counts()
        self.assertEqual(OVER, expected_over(counts))
        self.assertEqual(PINNED,
                         {name: counts[name] for name in PINNED
                          if name in counts})


class RatchetSelfTests(unittest.TestCase):
    """The failure paths, witnessed with a temporary table and temporary
    files so the messages are seen, not assumed."""

    def test_a_listed_file_over_its_entry_fails_naming_both_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mod.py"
            path.write_text("\n" * 1002)
            bad = violations({"pkg/mod.py": line_count(path)},
                             over={"pkg/mod.py": 1001}, pinned={})
        (msg,) = bad
        for needle in ("pkg/mod.py", "1002", "1001"):
            self.assertIn(needle, msg)

    def test_an_unlisted_file_over_the_ceiling_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_new.py"
            path.write_text("\n" * 1501)
            bad = violations({"tests/test_new.py": line_count(path)},
                             over={}, pinned={})
        (msg,) = bad
        for needle in ("tests/test_new.py", "1501", "1500"):
            self.assertIn(needle, msg)

    def test_a_listed_file_under_the_ceiling_is_a_stale_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mod.py"
            path.write_text("\n" * 10)
            bad = violations({"pkg/mod.py": line_count(path)},
                             over={"pkg/mod.py": 1001}, pinned={})
        (msg,) = bad
        self.assertIn("pkg/mod.py", msg)
        self.assertIn("stale", msg)


if __name__ == "__main__":
    unittest.main()

"""KO-384: the module-size ratchet — a ceiling table that only moves down.

Nothing else stops a module from growing; this file is the rule that runs
with the suite. `CEILING` sets the caps — 1000 lines a source module,
1500 a test module — and `OVER` holds every unpinned tracked Python file over its
cap. The walk fails the suite when a listed file grows past its entry,
when an unlisted file passes its ceiling, and when a listed file is back
under the ceiling — a stale entry. `PINNED` bounds a file a slice brought
back under its ceiling. Pins allow growth up to their bound; more than
150 lines of slack fails so the bounds keep following the code down.

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

# Pins are upper bounds, initialized with 60 lines of headroom in KO-576.
# Lower a pin when it sits more than 150 lines above its file's count.
PINNED = {
    "holophyte/babysitter.py": 1047, "holophyte/board.py": 961,
    "holophyte/claim.py": 970, "holophyte/cli.py": 552,
    "holophyte/config.py": 894, "holophyte/dispatch.py": 327,
    "holophyte/config_tables.py": 695, "holophyte/findings.py": 391,
    "holophyte/gates.py": 870, "holophyte/loop.py": 872,
    "holophyte/merge_gate.py": 562,
    "holophyte/operator.py": 539, "holophyte/pool.py": 434,
    "holophyte/pr.py": 739, "holophyte/pr_status.py": 497,
    "holophyte/pullrequest.py": 465,
    "holophyte/reconcile.py": 540, "holophyte/reexec.py": 154,
    "holophyte/report.py": 256, "provider.py": 362,
    "holophyte/serve.py": 989,
    "holophyte/serve_actions.py": 251, "holophyte/serve_config.py": 402,
    "holophyte/serve_runs.py": 651, "holophyte/supervisor.py": 963,
    "holophyte/supervisor_lock.py": 287,
    "holophyte/sweep_report.py": 322, "store/__init__.py": 1048,
    "store/operate.py": 1018, "store/read.py": 1018,
    "store/schema.py": 902, "store/tickets.py": 528,
    "tests/config_fixture.py": 136, "tests/loop_fixture.py": 713,
    "tests/serve_fixture.py": 230, "tests/test_babysit_pass.py": 494,
    "tests/test_babysit_threads.py": 963, "tests/test_babysit_checks.py": 317,
    "tests/test_babysitter.py": 674, "tests/test_config_tables.py": 558,
    "tests/test_claim.py": 1648, "tests/test_claim_mirror.py": 238,
    "tests/test_cli.py": 299,
    "tests/test_cli_approve.py": 390, "tests/test_cli_requeue.py": 333,
    "tests/test_file_sizes.py": 280, "tests/test_holophyte_package.py": 421,
    "tests/test_factory_config.py": 1199,
    "tests/test_factory_loop.py": 1378, "tests/test_merge_gate.py": 920,
    "tests/test_pool.py": 913, "tests/test_provider.py": 721, "tests/test_runs.py": 115,
    "tests/test_pullrequest.py": 1420, "tests/test_reconcile.py": 769,
    "tests/test_serve.py": 1461,
    "tests/test_serve_actions.py": 377, "tests/test_serve_config.py": 769,
    "tests/test_serve_ledger.py": 509, "tests/test_serve_runs.py": 906,
    "tests/test_serve_shipped.py": 241,
    "tests/sweep_fixture.py": 257, "tests/test_startup_checks.py": 708,
    "tests/test_store.py": 342,
    "tests/test_store_claim.py": 345, "tests/test_store_heartbeat.py": 124,
    "tests/test_store_interventions.py": 442,
    "tests/test_store_lease.py": 140, "tests/test_store_pickable.py": 181,
    "tests/test_store_read.py": 414, "tests/test_store_resume.py": 263,
    "tests/test_store_schema.py": 1441, "tests/test_store_status.py": 372,
    "tests/test_store_status_graph.py": 171,
    "tests/test_store_surface.py": 340, "tests/test_store_tickets.py": 178,
    "tests/test_supervise.py": 1360, "tests/test_supervisor_sweep.py": 1100,
    "tests/test_wiring_claim.py": 700, "tests/test_wiring_findings.py": 549,
    "tests/test_wiring_mirror.py": 349, "tests/test_wiring_phases.py": 501,
    "tests/test_wiring_rounds.py": 657,
    "tests/test_wiring_telemetry.py": 509,
    "tests/test_worktree_reuse.py": 299,
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


def expected_over(counts, ceiling=CEILING, pinned=PINNED):
    """The table `wc -l` says this tree needs: each unpinned tracked file over
    its ceiling at exactly its measured count."""
    return {
        name: lines for name, lines in counts.items()
        if name not in pinned
        and lines > ceiling["test" if name.startswith("tests/") else "source"]
    }


def violations(counts, over=OVER, ceiling=CEILING, pinned=PINNED):
    """The ratchet's rules as failure lines: a listed file over its
    entry, an unlisted file over its ceiling, a pinned file over its pin,
    excessive pin slack, an entry whose file is not over the ceiling — stale."""
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
        elif name not in pinned and lines > cap:
            bad.append(f"{name}: {lines} lines is over the {cap}-line "
                       f"ceiling with no table entry")
        pin = pinned.get(name)
        if pin is not None and lines > pin:
            bad.append(f"{name}: {lines} lines is over its pinned entry "
                       f"of {pin}")
        elif pin is not None and pin - lines > 150:
            bad.append(f"{name}: {lines} lines leaves more than 150 lines "
                       f"of slack under its pinned entry of {pin}; lower the pin")
    for name in sorted((set(over) | set(pinned)) - set(counts)):
        bad.append(f"{name}: no longer a tracked file; the table entry "
                   f"is stale — delete it")
    return bad


class FileSizeRatchet(unittest.TestCase):

    def test_every_tracked_python_file_holds_its_ceiling_or_entry(self):
        self.assertEqual(violations(tracked_counts()), [])

    def test_the_table_is_exactly_what_wc_l_measures(self):
        """Over-ceiling entries stay exact; pins allow bounded growth."""
        counts = wc_counts()
        self.assertEqual(OVER, expected_over(counts))
        self.assertEqual(violations(counts), [])


class RatchetSelfTests(unittest.TestCase):
    """The failure paths, witnessed with a temporary table and temporary
    files so the messages are seen, not assumed."""

    def test_pins_allow_growth_above_the_ordinary_ceiling(self):
        for name, pin in (("pkg/mod.py", 1060), ("tests/test_mod.py", 1560)):
            for count in (pin - 59, pin):
                with self.subTest(name=name, count=count):
                    counts = {name: count}
                    pins = {name: pin}
                    self.assertEqual(violations(counts, over={}, pinned=pins), [])
                    self.assertEqual(expected_over(counts, pinned=pins), {})
            with self.subTest(name=name, count=pin + 1):
                self.assertEqual(
                    violations({name: pin + 1}, over={}, pinned={name: pin}),
                    [f"{name}: {pin + 1} lines is over its pinned entry of {pin}"])

    def test_unpinned_files_still_need_entries_above_the_ceiling(self):
        counts = {"pkg/mod.py": 1001, "tests/test_mod.py": 1501}
        self.assertEqual(violations(counts, over={}, pinned={}), [
            "pkg/mod.py: 1001 lines is over the 1000-line ceiling with no table entry",
            "tests/test_mod.py: 1501 lines is over the 1500-line ceiling "
            "with no table entry",
        ])
        self.assertEqual(expected_over(counts, pinned={}), counts)

    def test_a_pinned_file_can_grow_or_shrink_within_its_bound(self):
        for count in (900, 899, 840, 751, 750):
            with self.subTest(count=count):
                self.assertEqual(violations({"pkg/mod.py": count}, over={},
                                            pinned={"pkg/mod.py": 900}), [])

    def test_a_pinned_file_over_its_pin_keeps_the_failure_message(self):
        self.assertEqual(
            violations({"pkg/mod.py": 901}, over={},
                       pinned={"pkg/mod.py": 900}),
            ["pkg/mod.py: 901 lines is over its pinned entry of 900"])

    def test_a_pinned_file_more_than_150_lines_under_its_pin_is_slack(self):
        self.assertEqual(
            violations({"pkg/mod.py": 749}, over={},
                       pinned={"pkg/mod.py": 900}),
            ["pkg/mod.py: 749 lines leaves more than 150 lines of slack "
             "under its pinned entry of 900; lower the pin"])

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

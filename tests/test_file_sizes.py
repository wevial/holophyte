"""The module-size ratchet: a ceiling table that only moves down.

Nothing else stops a module from growing; this file is the rule that runs
with the suite. `CEILING` sets the caps: 1000 lines a source module, 1500 a
test module. `PINNED` lists exactly the tracked Python files over their
ceiling, one entry to a line, each at its `wc -l` count. A pinned file may
not grow past its entry, and the entry never rises: new code goes in a new
module. The walk fails the suite when a file grows past its pin, when an
unpinned file passes its ceiling, when a pinned file is back at or under its
ceiling (a stale entry), and when a pin sits more than 150 lines above its
file. `OVER` holds any unpinned file over its cap at its exact count.

Run: python3 -m unittest discover -s tests -p 'test_file_sizes*' -v
"""

from __future__ import annotations

import re
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

# Only files over their ceiling, one to a line, sorted by path. An entry
# only goes down; a file brought to or under its ceiling leaves the table.
# Lower a pin when it sits more than 150 lines above its file's count.
PINNED = {
    "holophyte/babysit/babysitter.py": 1107,

    "holophyte/loop/claim.py": 1001,

    "holophyte/serve/server.py": 1166,

    "tests/test_claim.py": 1692,

    "tests/test_isolation.py": 1627,

    "tests/test_pullrequest.py": 1615,

    "tests/test_store_schema.py": 1621,
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
    excessive pin slack, an entry or pin whose file is not over the
    ceiling — stale."""
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
        elif pin is not None and lines <= cap:
            bad.append(f"{name}: {lines} lines is at or under the {cap}-line "
                       f"ceiling; the pinned entry is stale — delete it")
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

    def test_the_pinned_paths_are_exactly_the_files_over_their_ceiling(self):
        counts = wc_counts()
        over = {name for name, lines in counts.items()
                if lines > CEILING["test" if name.startswith("tests/")
                                   else "source"]}
        self.assertEqual(set(PINNED), over)

    def test_the_pinned_table_holds_one_entry_to_a_line(self):
        lines = Path(__file__).read_text().splitlines()
        start = lines.index("PINNED = {")
        end = lines.index("}", start)
        crowded = [line for line in lines[start + 1:end]
                   if len(re.findall(r'"[^"]+":\s*\d+', line)) > 1]
        self.assertEqual(crowded, [])


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

    def test_a_pinned_file_can_shrink_within_its_bound(self):
        for count in (1200, 1199, 1140, 1051, 1050):
            with self.subTest(count=count):
                self.assertEqual(violations({"pkg/mod.py": count}, over={},
                                            pinned={"pkg/mod.py": 1200}), [])

    def test_a_pinned_file_over_its_pin_keeps_the_failure_message(self):
        self.assertEqual(
            violations({"pkg/mod.py": 1201}, over={},
                       pinned={"pkg/mod.py": 1200}),
            ["pkg/mod.py: 1201 lines is over its pinned entry of 1200"])

    def test_a_pinned_file_more_than_150_lines_under_its_pin_is_slack(self):
        self.assertEqual(
            violations({"pkg/mod.py": 1049}, over={},
                       pinned={"pkg/mod.py": 1200}),
            ["pkg/mod.py: 1049 lines leaves more than 150 lines of slack "
             "under its pinned entry of 1200; lower the pin"])

    def test_a_pinned_file_at_or_under_the_ceiling_is_a_stale_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, cap in (("pkg/mod.py", 1000), ("tests/test_mod.py", 1500)):
                for count in (cap, cap - 40):
                    with self.subTest(name=name, count=count):
                        path = Path(tmp) / "mod.py"
                        path.write_text("\n" * count)
                        bad = violations({name: line_count(path)}, over={},
                                         pinned={name: cap + 10})
                        (msg,) = bad
                        for needle in (name, str(count), "stale",
                                       "delete it"):
                            self.assertIn(needle, msg)

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

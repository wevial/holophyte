"""KO-384: the module-size ratchet — a ceiling table that only moves down.

Nothing else stops a module from growing; this file is the rule that runs
with the suite. `CEILING` sets the caps — 1000 lines a source module,
1500 a test module — and `OVER` holds every tracked Python file over its
cap at the count this ticket measured. The walk fails the suite when a
listed file grows past its entry, when an unlisted file passes its
ceiling, and when a listed file is back under the ceiling — a stale
entry, so the table can only shrink.

Run: python3 -m unittest discover -s tests -p 'test_file_sizes*' -v
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CEILING = {"source": 1000, "test": 1500}

# Repo-relative path to the line count this ticket measured. Each slice
# that shrinks a file lowers its entry to the new count; a file back
# under its ceiling leaves the table.
OVER = {
    "holophyte/config.py": 1227,
    "holophyte/loop.py": 4236,
    "holophyte/pr.py": 1033,
    "holophyte/serve.py": 1779,
    "holophyte/supervisor.py": 1203,
    "store/__init__.py": 2995,
    "tests/test_factory_config.py": 2074,
    "tests/test_factory_loop.py": 6647,
    "tests/test_serve.py": 3134,
    "tests/test_supervisor_sweep.py": 2197,
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


def violations(counts, over=OVER, ceiling=CEILING):
    """The ratchet's three rules as failure lines: a listed file over its
    entry, an unlisted file over its ceiling, an entry whose file is not
    over the ceiling — stale."""
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
    for name in sorted(set(over) - set(counts)):
        bad.append(f"{name}: no longer a tracked file; the table entry "
                   f"is stale — delete it")
    return bad


class FileSizeRatchet(unittest.TestCase):

    def test_every_tracked_python_file_holds_its_ceiling_or_entry(self):
        self.assertEqual(violations(tracked_counts()), [])


class RatchetSelfTests(unittest.TestCase):
    """The failure paths, witnessed with a temporary table and temporary
    files so the messages are seen, not assumed."""

    def test_a_listed_file_over_its_entry_fails_naming_both_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mod.py"
            path.write_text("\n" * 1002)
            bad = violations({"pkg/mod.py": line_count(path)},
                             over={"pkg/mod.py": 1001})
        (msg,) = bad
        for needle in ("pkg/mod.py", "1002", "1001"):
            self.assertIn(needle, msg)

    def test_an_unlisted_file_over_the_ceiling_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test_new.py"
            path.write_text("\n" * 1501)
            bad = violations({"tests/test_new.py": line_count(path)},
                             over={})
        (msg,) = bad
        for needle in ("tests/test_new.py", "1501", "1500"):
            self.assertIn(needle, msg)

    def test_a_listed_file_under_the_ceiling_is_a_stale_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mod.py"
            path.write_text("\n" * 10)
            bad = violations({"pkg/mod.py": line_count(path)},
                             over={"pkg/mod.py": 1001})
        (msg,) = bad
        self.assertIn("pkg/mod.py", msg)
        self.assertIn("stale", msg)


if __name__ == "__main__":
    unittest.main()

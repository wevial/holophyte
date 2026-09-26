"""The runbook's native move backs the store up on any factory host.

KO-770: the writer host has no `sqlite3` command-line tool, so the backup
line before the native import is a `python3 -c` one-liner over the
`sqlite3` module. These checks run that exact line from the runbook against
a temporary store and keep the command-line tool out of the block.

Run: python3 -m unittest discover -s tests -p 'test_docs_runbook.py' -v
"""
from __future__ import annotations

import re
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs" / "operating" / "runbook.md"
SECTION = "### Move a project off Linear to the native board"
STORE_ARG = re.compile(r"^~/\.holophyte/[^/]+/(store\.db(?:\.pre-native)?)$")


def native_move_block() -> list[str]:
    """The lines of the first code block under the native move heading."""
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index(SECTION)
    match = re.search(r"^```[^\n]*\n(.*?)^```", text[start:], re.M | re.S)
    if match is None:
        raise AssertionError("the native move section has no code block")
    return match.group(1).splitlines()


def backup_line() -> str:
    lines = [ln for ln in native_move_block()
             if ln.startswith('python3 -c "import sqlite3')]
    if len(lines) != 1:
        raise AssertionError(f"expected one backup line, found {lines!r}")
    return lines[0]


class NativeMoveBackupTest(unittest.TestCase):

    def test_backup_line_copies_the_store_and_prints_ok(self):
        argv = shlex.split(backup_line())
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "store.db", Path(tmp) / "store.db.pre-native"
            paths = {"store.db": src, "store.db.pre-native": dst}
            with sqlite3.connect(src) as conn:
                conn.execute("create table tickets (id text, title text)")
                conn.executemany("insert into tickets values (?, ?)",
                                 [("KO-1", "first"), ("KO-2", "second")])
            conn.close()
            swapped = []
            for arg in argv:
                found = STORE_ARG.match(arg)
                swapped.append(str(paths[found.group(1)]) if found else arg)
            self.assertEqual(swapped.count(str(src)), 1, argv)
            self.assertEqual(swapped.count(str(dst)), 1, argv)
            proc = subprocess.run(swapped, capture_output=True, text=True,
                                  timeout=60, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "ok")
            copy = sqlite3.connect(dst)
            try:
                rows = copy.execute(
                    "select id, title from tickets order by id").fetchall()
            finally:
                copy.close()
            self.assertEqual(rows, [("KO-1", "first"), ("KO-2", "second")])

    def test_no_line_invokes_the_sqlite3_command(self):
        for line in native_move_block():
            words = shlex.split(line, comments=True)
            self.assertNotIn("sqlite3", words[:1], line)


if __name__ == "__main__":
    unittest.main()

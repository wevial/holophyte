"""A witness command's processes do not outlive the pass that ran it.

Run: python3 -m unittest discover -s tests -p 'test_witness_reap.py' -v
"""
import os
import random
import shlex
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from holophyte.story.witness import main_tip, run_witnesses
from tests.test_witness_runner import PASSES, PYTHON, WitnessRunnerFixture, git

PROC = Path("/proc")


def carrying(token):
    pids = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if token.encode() in argv:
            pids.append(int(entry.name))
    return pids


def kill_carrying(token):
    for pid in carrying(token):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def alive_carrying(token, settle=2.0):
    until = time.monotonic() + settle
    while carrying(token) and time.monotonic() < until:
        time.sleep(0.1)
    return carrying(token)


@unittest.skipUnless(PROC.is_dir(), "reading survivors needs /proc")
class WitnessCommandTreeTests(WitnessRunnerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.token = f"300.{random.randrange(10**9):09d}"
        self.addCleanup(kill_carrying, self.token)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        tempdir = patch.object(tempfile, "tempdir", str(self.scratch))
        tempdir.start()
        self.addCleanup(tempdir.stop)

    def test_a_green_command_leaves_no_descendant_running(self):
        token = shlex.quote(self.token)
        detached = shlex.quote(
            "import os, time; os.setsid(); time.sleep(300)")
        command = (f"sleep {token} >/dev/null 2>&1 &"
                   f" {PYTHON} -c {detached} {token} >/dev/null 2>&1 &"
                   " exit 0")
        target = self.project()
        self.approve([("W1", "tests/test_w1.py", command, PASSES)])

        rows = run_witnesses(target, self.conn, self.story_id,
                             main_tip(target), "operator", copy_files=True)

        self.assertEqual([row.verdict for row in rows], ["green"])
        self.assertEqual(list(self.scratch.glob("witness-*")), [])
        self.assertEqual(
            len(git(self.repo, "worktree", "list").splitlines()), 1)
        self.assertEqual(alive_carrying(self.token), [])


if __name__ == "__main__":
    unittest.main()

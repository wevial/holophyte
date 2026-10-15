"""A streaming turn launched in the implementer image: its lines reach the
quiet watchdog as they are printed, and a stall removes its container."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from holophyte.agents.claude_stream import TurnStalled
from holophyte.isolation import launcher

LINE = '{"type":"system","subtype":"status","session_id":"S"}'
DONE = '{"type":"result","subtype":"success","is_error":false,"result":"done"}'


@unittest.skipUnless(
    os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
    "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
)
class ContainerStreamTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.worktree = Path(tmp.name) / "task"
        self.worktree.mkdir()
        self.route = launcher.Route("container", writable=False)
        launcher.image_ready(self.route)
        self.names = []

    def launch(self, script):
        real = uuid.uuid4

        def mint():
            made = real()
            self.names.append("holophyte-implement-" + made.hex)
            return made

        with patch.object(launcher.uuid, "uuid4", side_effect=mint):
            return launcher.launch(self.route, self.worktree, {},
                                   ["sh", "-c", script], timeout=120, quiet=2)

    def test_lines_printed_each_second_arrive_unbuffered(self):
        code, output = self.launch(
            f"for i in 1 2 3 4; do echo '{LINE}'; sleep 1; done; echo '{DONE}'")
        self.assertEqual((code, output), (0, DONE))

    def test_a_silent_turn_stalls_and_leaves_no_container(self):
        started = time.monotonic()
        with self.assertRaises(TurnStalled):
            self.launch(f"echo '{LINE}'; sleep 10")
        self.assertLess(time.monotonic() - started, 6)
        self.assertTrue(self.names)
        for name in self.names:
            listed = subprocess.run(
                ["docker", "ps", "-aq", "--filter", f"name=^{name}$"],
                capture_output=True, text=True, check=True).stdout
            self.assertEqual(listed.strip(), "", name)


if __name__ == "__main__":
    unittest.main()

"""The escaped-child failure message explains itself (KO-210).

Run: python3 -m unittest discover -s tests -p 'test_procs*' -v
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import procs  # noqa: E402 - after the sys.path insert above


class EscapedChildReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.marker = Path(tmp.name) / "escaped.txt"

    def test_a_present_marker_names_the_surviving_process_and_the_shell(self):
        # A live process whose command line carries the marker path stands
        # in for the child that outlived the cap; the message must name it
        # by pid with its group and session, and say which shell ran it.
        self.marker.write_text("")
        proc = subprocess.Popen(["sh", "-c", "sleep 30; : %s" % self.marker])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)

        with self.assertRaises(AssertionError) as caught:
            procs.assert_no_escaped_child(self.marker, 0.2, elapsed=1.75, cap=1.0)

        message = str(caught.exception)
        self.assertIn("outlived the cap", message)
        self.assertRegex(message, r"(?m)^\s*%d\s+\d+\s+\d+\s+\S+\s+sh -c" % proc.pid)
        self.assertIn("0.750s went to reaping", message)
        self.assertIn("/bin/sh resolves to /", message)

    def test_a_failing_ps_still_fails_the_test_with_a_note(self):
        self.marker.write_text("")

        with patch.object(procs.subprocess, "run",
                          side_effect=OSError("no ps here")), \
                self.assertRaises(AssertionError) as caught:
            procs.assert_no_escaped_child(self.marker, 0.2)

        message = str(caught.exception)
        self.assertIn("a child of the timed-out command outlived the cap", message)
        self.assertIn("process snapshot could not be taken", message)
        self.assertIn("no ps here", message)

    def test_an_absent_marker_raises_nothing(self):
        procs.assert_no_escaped_child(self.marker, 0.1)


if __name__ == "__main__":
    unittest.main()

"""Standalone imports must not depend on the factory's import order."""
import subprocess
import unittest
from pathlib import Path


class ThreadAnswersImportTests(unittest.TestCase):
    def test_standalone_import_in_fresh_interpreter(self):
        result = subprocess.run(
            ["python3", "-c", "import holophyte.thread_answers"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

import subprocess
import tempfile
import unittest
from pathlib import Path

from holophyte.isolation.isolation_git import git


class IsolationGitErrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def test_failed_clone_message_names_subcommand_status_and_git_stderr(self):
        missing = self.root / "missing"
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            git(self.repo, "clone", str(missing), str(self.root / "out"))
        message = str(caught.exception)
        self.assertEqual(caught.exception.returncode, 128)
        self.assertIn("clone", message)
        self.assertIn("128", message)
        self.assertIn("does not exist", message)
        self.assertIn(str(missing), message)

    def test_long_stderr_keeps_last_1000_characters_without_control_chars(self):
        script = self.root / "noisy.sh"
        script.write_text(
            "printf 'HEAD-MARKER' >&2\n"
            "head -c 9965 /dev/zero | tr '\\000' x >&2\n"
            "printf 'esc\\033tab\\there\\nTAIL-MARKER' >&2\n"
            "exit 3\n"
        )
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            git(self.repo, "-c", f"alias.noisy=!sh {script}", "noisy")
        self.assertEqual(len(caught.exception.stderr), 10000)
        message = str(caught.exception)
        prefix = "git noisy exited with status 3: "
        self.assertTrue(message.startswith(prefix), message)
        tail = message[len(prefix):]
        self.assertLessEqual(len(tail), 1000)
        self.assertTrue(tail.endswith("xxesc tab here TAIL-MARKER"), tail[-40:])
        self.assertNotIn("HEAD-MARKER", message)
        self.assertFalse(
            [char for char in message if ord(char) < 32 or ord(char) == 127]
        )

    def test_successful_command_returns_stripped_stdout(self):
        self.assertEqual(git(self.repo, "rev-parse", "--git-dir"), ".git")


if __name__ == "__main__":
    unittest.main()

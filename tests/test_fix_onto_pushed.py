import subprocess
import tempfile
import unittest
from pathlib import Path

from holophyte.loop import branch_sync


class FixOntoPushedTests(unittest.TestCase):
    def test_descendant_head_and_branch_ref_stay_unchanged(self):
        with tempfile.TemporaryDirectory() as wt:
            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=wt, capture_output=True,
                    text=True, check=True).stdout.strip()

            git("init", "-b", "fix")
            git("config", "user.name", "Test Writer")
            git("config", "user.email", "writer@example.test")
            git("config", "commit.gpgsign", "false")
            git("commit", "--allow-empty", "-m", "Pushed fix")
            pushed = git("rev-parse", "HEAD")
            git("commit", "--allow-empty", "-m", "Follow-up fix")
            head = git("rev-parse", "HEAD")

            self.assertEqual(branch_sync.onto_pushed(wt, "fix", pushed, head), head)
            self.assertEqual(git("rev-parse", "refs/heads/fix"), head)

    def test_amended_pushed_fix_fast_forwards_preserving_tree_and_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "worktree"
            remote = root / "remote.git"

            def git(*args, cwd=wt, check=True):
                result = subprocess.run(
                    ["git", *args], cwd=cwd, capture_output=True, text=True)
                if check:
                    self.assertEqual(result.returncode, 0, result.stderr)
                return result

            git("init", "--bare", str(remote), cwd=root)
            git("init", "-b", "fix", str(wt), cwd=root)
            git("config", "user.name", "Test Writer")
            git("config", "user.email", "writer@example.test")
            git("config", "commit.gpgsign", "false")
            git("remote", "add", "origin", str(remote))
            git("commit", "--allow-empty", "-m", "Base")
            (wt / "fix.txt").write_text("The requested fix\n")
            git("add", "fix.txt")
            git("commit", "-m", "Apply fix")
            git("push", "origin", "fix")
            pushed = git("rev-parse", "HEAD").stdout.strip()

            message = "Apply fix\n\nCite operator_note event 42"
            git("commit", "--amend", "-m", message)
            head = git("rev-parse", "HEAD").stdout.strip()
            tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
            self.assertNotEqual(head, pushed)
            self.assertIsNone(branch_sync._candidate_drift(wt, "fix", head))
            self.assertEqual(
                git("merge-base", "--is-ancestor", pushed, head,
                    check=False).returncode, 1)

            branch_sync.onto_pushed(wt, "fix", pushed, head)

            push = git("push", "origin", "fix", check=False)
            self.assertEqual(push.returncode, 0, push.stderr)
            tip = git("rev-parse", "refs/heads/fix", cwd=remote).stdout.strip()
            self.assertEqual(
                git("rev-parse", f"{tip}^{{tree}}").stdout.strip(), tree)
            git("merge-base", "--is-ancestor", pushed, tip)
            self.assertEqual(
                git("log", "-1", "--format=%B", tip).stdout.strip(), message)

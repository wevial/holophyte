"""The merge lock is reached through `Target.locks` (KO-594)."""
import ast
import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # the package imports store/ticket_template by name
import holophyte.claim  # noqa: E402 - after the sys.path insert above
from holophyte.gates import merge_lock_path, read_merge_lock  # noqa: E402
from holophyte.target import Target  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class RecordingLocks:
    """A `Locks` that takes nothing and remembers each `merge()` asked of it."""

    def __init__(self):
        self.calls = []

    @contextlib.contextmanager
    def merge(self, conn, run_id, beat_s, operation="gate",
              wait_phase="merge_gate"):
        self.calls.append((run_id, operation, wait_phase))
        yield


class TargetLocksTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(self.root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def test_claim_time_fetch_takes_the_lock_the_target_supplies(self):
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "base")
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "origin", "main")
        fake = RecordingLocks()
        located = Target.locate(self.repo, adopt=False)
        target = Target(path=located.path, holo_dir=located.holo_dir,
                        store_path=located.store_path,
                        config_path=located.config_path,
                        worktrees=located.worktrees, locks=fake)
        holophyte.claim._refresh_main(target, run_id=42)
        self.assertEqual(fake.calls, [(42, "fetch before the cut", "working")])
        self.assertFalse(merge_lock_path(target).exists())
        self.assertFalse(target.holo_dir.exists())

    def test_default_locks_hold_the_merge_lock_file(self):
        target = Target.locate(self.repo, adopt=False)
        path = merge_lock_path(target)
        with target.locks.merge(None, 7, 1.0):
            self.assertEqual(read_merge_lock(path)[0], 7)
        self.assertFalse(path.exists())

    def test_callers_no_longer_import_live_merge_lock(self):
        for module in ("merge_gate", "claim"):
            with self.subTest(module=module):
                tree = ast.parse((ROOT / "holophyte" / f"{module}.py").read_text())
                imported = {alias.name for node in ast.walk(tree)
                            if isinstance(node, ast.ImportFrom)
                            and node.module == "holophyte.merge_lock"
                            for alias in node.names}
                self.assertNotIn("live_merge_lock", imported)


if __name__ == "__main__":
    unittest.main()

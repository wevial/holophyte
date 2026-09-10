"""The merge lock: two gates on one target take turns (KO-342)."""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above


class MergeLockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "repo").mkdir()
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        holophyte.target.state_dir(root / "repo").mkdir(parents=True)
        self.tgt = holophyte.target.Target.locate(root / "repo")

    def test_the_second_gate_waits_for_the_first_to_release(self):
        """Two runs reach the gate together: one holds the lock while the
        other polls, and the second enters only after the first has left."""
        first_in = threading.Event()
        spans = {}

        def gate(run_id, hold):
            with holophyte.gates.merge_lock(self.tgt, run_id, wait=10,
                                            poll=0.01):
                entered = time.monotonic()
                if run_id == 1:
                    first_in.set()
                time.sleep(hold)
                spans[run_id] = (entered, time.monotonic())

        one = threading.Thread(target=gate, args=(1, 0.3))
        two = threading.Thread(target=gate, args=(2, 0.0))
        one.start()
        self.assertTrue(first_in.wait(5))
        two.start()
        one.join(5)
        two.join(5)

        self.assertEqual(sorted(spans), [1, 2])
        self.assertGreaterEqual(spans[2][0], spans[1][1])
        self.assertFalse(holophyte.gates.merge_lock_path(self.tgt).exists())

    def test_a_lock_held_past_the_bound_names_its_holder(self):
        path = holophyte.gates.merge_lock_path(self.tgt)
        path.write_text(f"7 {time.time():.3f}\n")

        with self.assertRaises(holophyte.gates.MergeLockHeld) as caught:
            with holophyte.gates.merge_lock(self.tgt, 8, wait=0.05, poll=0.01):
                self.fail("the gate entered under another run's lock")

        self.assertIn("run 7", str(caught.exception))
        self.assertIsInstance(caught.exception, holophyte.gates.InfraFailure)
        self.assertEqual(holophyte.gates.read_merge_lock(path)[0], 7)


if __name__ == "__main__":
    unittest.main()

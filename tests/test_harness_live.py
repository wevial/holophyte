"""Live: a harness adapter's turn and resume argv against the real CLI.

Opt in with `HOLOPHYTE_LIVE_HARNESS=claude` on a host with a signed-in
`claude` on PATH; without the variable the test skips, and with it set but
no binary on PATH the test fails. Kept out of the ticket's verify block:
the reviewer's container carries no agent credentials.

Run: HOLOPHYTE_LIVE_HARNESS=claude python3 -m unittest tests.test_harness_live
"""
import os
import secrets
import shutil
import subprocess
import tempfile
import unittest

from holophyte import harness

LIVE = os.environ.get("HOLOPHYTE_LIVE_HARNESS")
TURN_TIMEOUT = 300


@unittest.skipUnless(LIVE, "set HOLOPHYTE_LIVE_HARNESS=claude for a live turn")
class LiveHarnessTests(unittest.TestCase):
    def run_turn(self, argv, cwd):
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                timeout=TURN_TIMEOUT, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0,
                         f"{argv[0]} exited {result.returncode}:\n"
                         f"{result.stdout}\n{result.stderr}")
        return result.stdout

    def test_resumed_session_remembers_the_word(self):
        self.assertIn(LIVE, harness.ADAPTERS,
                      f"HOLOPHYTE_LIVE_HARNESS={LIVE!r} names no adapter")
        adapter = harness.ADAPTERS[LIVE]
        binary = shutil.which(adapter.name)
        self.assertIsNotNone(binary, f"HOLOPHYTE_LIVE_HARNESS={LIVE} but no "
                             f"{adapter.name!r} on PATH")
        seat = harness.Seat(adapter, binary, {})
        word = "holo" + secrets.token_hex(3)
        with tempfile.TemporaryDirectory(prefix="holophyte-live-") as scratch:
            turn = seat.turn(f"Remember this word: {word}. Reply with: noted")
            print(f"turn: {[adapter.name, *turn[1:-1]]}")
            self.run_turn(turn, scratch)
            session = seat.session(turn)
            resume = seat.resume(session) + [
                "What word did I ask you to remember? Reply with the word only."]
            print(f"resume: {[adapter.name, *resume[1:-1]]}")
            answer = self.run_turn(resume, scratch)
        print(f"answer: {answer.strip()}")
        self.assertIn(word, answer.lower())


if __name__ == "__main__":
    unittest.main()

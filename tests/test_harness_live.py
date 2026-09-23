"""Live: a harness adapter's turn and resume argv against the real CLI.

Opt in with `HOLOPHYTE_LIVE_HARNESS=claude` (an implementer turn and its
resume) or `HOLOPHYTE_LIVE_HARNESS=codex` or `devin` (two review rounds
through `holophyte.agents.agent()`) on a host with that CLI signed in on PATH;
without the variable the tests skip, and with it set but no binary on PATH
the test fails. Kept out of the ticket's verify block: the reviewer's
container carries no agent credentials.

Run: HOLOPHYTE_LIVE_HARNESS=claude python3 -m unittest tests.test_harness_live
     HOLOPHYTE_LIVE_HARNESS=codex python3 -m unittest tests.test_harness_live
     HOLOPHYTE_LIVE_HARNESS=devin python3 -m unittest tests.test_harness_live
"""
import os
import secrets
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import holophyte.agents
import holophyte.target
import store
from holophyte import harness

LIVE = os.environ.get("HOLOPHYTE_LIVE_HARNESS")
TURN_TIMEOUT = 300
# The `[agents.reviewer]` table each review harness's live rounds run under.
REVIEW_TABLES = {
    "codex": 'harness = "codex"\neffort = "low"\n',
    "devin": 'harness = "devin"\nmodel = "opus"\n',
}


@unittest.skipUnless(LIVE and LIVE not in REVIEW_TABLES,
                     "set HOLOPHYTE_LIVE_HARNESS=claude for a live turn")
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


@unittest.skipUnless(LIVE in REVIEW_TABLES,
                     "set HOLOPHYTE_LIVE_HARNESS=codex or devin for live review "
                     "rounds")
class LiveReviewTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             *args], cwd=repo, text=True).strip()

    def test_rounds_see_the_candidate_and_resume_the_session(self):
        self.assertIsNotNone(shutil.which(LIVE), f"HOLOPHYTE_LIVE_HARNESS={LIVE} "
                             f"but no {LIVE!r} on PATH")
        word = "holo" + secrets.token_hex(3)
        with tempfile.TemporaryDirectory(prefix="holophyte-live-") as scratch:
            root = Path(scratch)
            repo = root / "repo"
            repo.mkdir()
            self.git(repo, "init", "-q")
            (repo / "notes.txt").write_text("base\n")
            self.git(repo, "add", "notes.txt")
            self.git(repo, "commit", "-qm", "base")
            base = self.git(repo, "rev-parse", "HEAD")
            (repo / "notes.txt").write_text("candidate\n")
            self.git(repo, "commit", "-qam", "candidate")
            candidate = self.git(repo, "rev-parse", "HEAD")
            self.git(repo, "checkout", "-q", base)
            holo = root / "holo"
            holo.mkdir()
            (holo / "config.toml").write_text(
                '[agents.reviewer]\n' + REVIEW_TABLES[LIVE]
                + '[loop]\nreview_session = "resume"\n')
            target = holophyte.target.Target(
                path=repo, holo_dir=holo, store_path=holo / "store.db",
                config_path=holo / "config.toml", worktrees=root / "repo.worktrees")
            conn = store.open(target.store_path)
            self.addCleanup(conn.close)
            store.init(conn)
            project = store.ensure_project(conn, "live", repo)
            ticket = store.mirror_ticket(conn, project, "KO-614", "KO-614", "live",
                                         acceptance_criteria=["review"],
                                         verification_commands=["true"])
            run = store.claim(conn, project, ticket)

            def review(goal, review_round):
                output = holophyte.agents.agent(
                    target, "review", goal, repo, base_sha=base,
                    candidate_sha=candidate, timeout=TURN_TIMEOUT, conn=conn,
                    run_id=run, review_round=review_round)
                print(f"round {review_round} ({output.command}):\n{output}")
                self.assertEqual(output.exit_code, 0, output)
                return output

            first = review(
                "Run `git rev-parse HEAD` in the current checkout and reply with "
                f"the full commit id it prints. Remember this word: {word}.", 1)
            second = review("What word did I ask you to remember? Reply with "
                            "the word only.", 2)
            status = self.git(repo, "status", "--porcelain")
        self.assertIn(candidate, first)
        self.assertIn(word, second.lower())
        self.assertEqual(status, "")


if __name__ == "__main__":
    unittest.main()

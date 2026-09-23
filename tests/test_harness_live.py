"""Live: a harness adapter's turn and resume argv against the real CLI.

Opt in with `HOLOPHYTE_LIVE_HARNESS=claude` (an implementer turn and its
resume), `HOLOPHYTE_LIVE_HARNESS=codex` (two review rounds through
`holophyte.agents.agent()`, and an implementer turn through the loop's
`_timed()` and its resume) or `HOLOPHYTE_LIVE_HARNESS=cursor` (one review
round; `HOLOPHYTE_LIVE_MODEL` picks its model, `auto` by default) on a host
with that CLI signed in on PATH; without the variable the tests skip, and
with it set but no binary on PATH the test fails. Kept out of the ticket's
verify block: the reviewer's container carries no agent credentials.

Run: HOLOPHYTE_LIVE_HARNESS=claude python3 -m unittest tests.test_harness_live
     HOLOPHYTE_LIVE_HARNESS=codex python3 -m unittest tests.test_harness_live
     HOLOPHYTE_LIVE_HARNESS=cursor python3 -m unittest tests.test_harness_live
"""
import os
import secrets
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

import holophyte.agents
import holophyte.fix_session
import holophyte.loop
import holophyte.project
import store
from holophyte import harness

LIVE = os.environ.get("HOLOPHYTE_LIVE_HARNESS")
TURN_TIMEOUT = 300
# The review harnesses, which run through `agent()` rather than the
# implementer's turn-and-resume case.
REVIEW_HARNESSES = ("codex", "cursor")


@unittest.skipUnless(LIVE and LIVE not in REVIEW_HARNESSES,
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
        seat = harness.Seat(adapter, binary, {}, "implementer")
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


class LiveReviewCase(unittest.TestCase):
    """A temporary repository whose candidate differs from its base, checked
    out at the base, and a run to review it under `CONFIG`."""
    CONFIG = None

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             *args], cwd=self.repo, text=True).strip()

    def setUp(self):
        binary = harness.ADAPTERS[LIVE].binary
        self.assertIsNotNone(shutil.which(binary), f"HOLOPHYTE_LIVE_HARNESS={LIVE} "
                             f"but no {binary!r} on PATH")
        scratch = tempfile.TemporaryDirectory(prefix="holophyte-live-")
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "notes.txt").write_text("base\n")
        self.git("add", "notes.txt")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        (self.repo / "notes.txt").write_text("candidate\n")
        self.git("commit", "-qam", "candidate")
        self.candidate = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", self.base)
        holo = root / "holo"
        holo.mkdir()
        (holo / "config.toml").write_text(self.CONFIG)
        self.target = holophyte.project.Project(
            path=self.repo, holo_dir=holo, store_path=holo / "store.db",
            config_path=holo / "config.toml", worktrees=root / "repo.worktrees")
        self.conn = store.open(self.target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "live", self.repo)
        ticket = store.mirror_ticket(self.conn, project, "KO-614", "KO-614", "live",
                                     acceptance_criteria=["review"],
                                     verification_commands=["true"])
        self.run_id = store.claim(self.conn, project, ticket)

    def review(self, goal, review_round):
        output = holophyte.agents.agent(
            self.target, "review", goal, self.repo, base_sha=self.base,
            candidate_sha=self.candidate, timeout=TURN_TIMEOUT, conn=self.conn,
            run_id=self.run_id, review_round=review_round)
        print(f"round {review_round} ({output.command}):\n{output}")
        self.assertEqual(output.exit_code, 0, output)
        return output


HEAD_GOAL = ("Run `git rev-parse HEAD` in the current checkout and reply with "
             "the full commit id it prints.")


@unittest.skipUnless(LIVE == "codex",
                     "set HOLOPHYTE_LIVE_HARNESS=codex for live review rounds")
class LiveCodexReviewTests(LiveReviewCase):
    CONFIG = ('[agents.reviewer]\nharness = "codex"\neffort = "low"\n'
              '[loop]\nreview_session = "resume"\n')

    def test_rounds_see_the_candidate_and_resume_the_session(self):
        word = "holo" + secrets.token_hex(3)
        first = self.review(f"{HEAD_GOAL} Remember this word: {word}.", 1)
        second = self.review("What word did I ask you to remember? Reply with "
                             "the word only.", 2)
        self.assertIn(self.candidate, first)
        self.assertIn(word, second.lower())
        self.assertEqual(self.git("status", "--porcelain"), "")


@unittest.skipUnless(LIVE == "cursor",
                     "set HOLOPHYTE_LIVE_HARNESS=cursor for a live review round")
class LiveCursorReviewTests(LiveReviewCase):
    CONFIG = ('[agents.reviewer]\nharness = "cursor"\nmodel = "'
              + os.environ.get("HOLOPHYTE_LIVE_MODEL", "auto") + '"\n')

    def test_a_round_sees_the_candidate_and_leaves_the_repository_clean(self):
        output = self.review(HEAD_GOAL, 1)
        self.assertIn(self.candidate, output)
        self.assertEqual(self.git("status", "--porcelain"), "")


@unittest.skipUnless(LIVE == "codex",
                     "set HOLOPHYTE_LIVE_HARNESS=codex for a live implementer turn")
class LiveCodexImplementerTests(unittest.TestCase):
    def test_turn_writes_the_file_and_the_resume_remembers_the_word(self):
        self.assertIsNotNone(shutil.which("codex"), "HOLOPHYTE_LIVE_HARNESS=codex "
                             "but no 'codex' on PATH")
        word = "holo" + secrets.token_hex(3)
        with tempfile.TemporaryDirectory(prefix="holophyte-live-") as scratch:
            root = Path(scratch)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            holo = root / "holo"
            holo.mkdir()
            (holo / "config.toml").write_text(
                '[agents.implementer]\nharness = "codex"\neffort = "low"\n')
            target = holophyte.project.Project(
                path=repo, holo_dir=holo, store_path=holo / "store.db",
                config_path=holo / "config.toml", worktrees=root / "repo.worktrees")
            conn = store.open(target.store_path)
            self.addCleanup(conn.close)
            store.init(conn)
            project = store.ensure_project(conn, "live", repo)
            ticket = store.mirror_ticket(conn, project, "KO-624", "KO-624", "live",
                                         acceptance_criteria=["implement"],
                                         verification_commands=["true"])
            run = store.claim(conn, project, ticket)
            output, timed_out = holophyte.loop._timed(
                target, conn, run, 60, repo, TURN_TIMEOUT / 60,
                "Write the text ok to a new file note.txt in the current "
                f"directory. Also remember this word: {word}, but do not write "
                "it to any file. Reply with: done")
            print(f"turn ({output.command}):\n{output}")
            self.assertFalse(timed_out)
            self.assertEqual(output.exit_code, 0, output)
            note = (repo / "note.txt").read_text().strip()
            (session,) = conn.execute("SELECT providerSessionId FROM runs "
                                      "WHERE id = ?", (run,)).fetchone()
            argv, reason = holophyte.fix_session.resume_argv(target, conn, run)
            self.assertIsNone(reason)
            print(f"resume: {['codex', *argv[1:]]}")
            result = subprocess.run(
                argv + ["What word did I ask you to remember? Reply with the "
                        "word only."], cwd=repo, capture_output=True, text=True,
                timeout=TURN_TIMEOUT, stdin=subprocess.DEVNULL)
            self.assertEqual(result.returncode, 0,
                             f"{result.stdout}\n{result.stderr}")
        print(f"answer: {result.stdout.strip()}")
        self.assertEqual(note, "ok")
        self.assertEqual(str(uuid.UUID(session)), session)
        self.assertIn(word, result.stdout.lower())


if __name__ == "__main__":
    unittest.main()

"""Table-form roles: the adapter's argv, its session and its resume -- a
claude implementer, and a codex reviewer in a throwaway candidate checkout.

Run: python3 -m unittest tests.test_harness -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import holophyte.agents
import holophyte.fix_session
import holophyte.loop
import holophyte.target
import store

# The fake harness: records its argv, then sleeps past the cap when the
# prompt asks it to.
FAKE_CLAUDE = """
import json, os, sys, time
with open(os.environ["FAKE_HARNESS_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
print("fake claude ran")
sys.stdout.flush()
if "stall" in sys.argv[-1]:
    time.sleep(30)
"""

CONFIG = ('[agents.implementer]\nharness = "claude"\nmodel = "sonnet"\n'
          'effort = "low"\n')


class ClaudeTableTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c",
                        "user.email=test@example.invalid", "commit",
                        "--allow-empty", "-qm", "base"], cwd=self.repo, check=True)
        holo = root / "holo"
        holo.mkdir()
        (holo / "config.toml").write_text(CONFIG + '[loop]\nfix_session = "resume"\n')
        self.target = holophyte.target.Target(
            path=self.repo, holo_dir=holo, store_path=holo / "store.db",
            config_path=holo / "config.toml", worktrees=root / "repo.worktrees")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
        fake.chmod(0o755)
        self.calls = root / "calls.jsonl"
        env = patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_HARNESS_CALLS": str(self.calls)})
        env.start()
        self.addCleanup(env.stop)
        self.conn = store.open(self.target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "test", self.repo)
        ticket = store.mirror_ticket(self.conn, project, "KO-613", "KO-613",
                                     "harness", acceptance_criteria=["record"],
                                     verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket)

    def received(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def session(self):
        return self.conn.execute("SELECT providerSessionId FROM runs WHERE id = ?",
                                 (self.run,)).fetchone()[0]

    def implement(self, goal, timeout=60):
        return holophyte.agents.agent(self.target, "implement", goal, self.repo,
                                      timeout=timeout, conn=self.conn,
                                      run_id=self.run)

    def assert_turn(self, argv, goal):
        self.assertEqual(argv[:2], ["-p", "--session-id"])
        self.assertEqual(argv[3:], ["--model", "sonnet", "--effort", "low", goal])
        self.assertEqual(str(uuid.UUID(argv[2], version=4)), argv[2])
        self.assertEqual(self.session(), argv[2])

    def test_implement_turn_runs_the_adapter_argv_and_records_its_session(self):
        self.implement("implement the thing")
        [argv] = self.received()
        self.assert_turn(argv, "implement the thing")

    def test_a_turn_the_timeout_ends_still_leaves_its_session(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.implement("stall until the cap", timeout=1)
        [argv] = self.received()
        self.assert_turn(argv, "stall until the cap")

    def test_fix_turn_resumes_the_recorded_session(self):
        self.implement("implement the thing")
        [first] = self.received()
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                      text=True).strip()
        holophyte.fix_session.fix_turn(
            self.target, self.conn, self.run, 60, self.repo, 1, "the ticket",
            "REQUEST_CHANGES: a finding", sha, timed=holophyte.loop._timed,
            check_cap=lambda *args: None)
        _, resumed = self.received()
        self.assertEqual(resumed[:-1], ["-p", "--resume", first[2], "--model",
                                        "sonnet", "--effort", "low"])
        self.assertTrue(resumed[-1].startswith("Reviewer findings:"))
        self.assertIn("REQUEST_CHANGES: a finding", resumed[-1])
        [(payload,)] = self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'fix_session'").fetchall()
        self.assertEqual(json.loads(payload), {"arm": "resume", "resumed": True})


# The fake codex: records its argv and its cwd's HEAD, prints the banner and,
# when told to, answers a resume with Codex's missing-rollout error.
FAKE_CODEX = """
import json, os, subprocess, sys
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                      text=True).stdout.strip()
with open(os.environ["FAKE_HARNESS_CALLS"], "a") as calls:
    calls.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                            "head": head}) + "\\n")
if sys.argv[2] == "resume" and os.environ.get("FAKE_NO_ROLLOUT"):
    print("Error: thread/resume failed: no rollout found for thread id x")
    sys.exit(1)
print("workdir: " + os.getcwd())
print("session id: codex-" + str(len(open(os.environ["FAKE_HARNESS_CALLS"])
                                      .readlines())))
print("APPROVE")
"""

CODEX_CONFIG = "".join(
    f'[agents.{seat}]\nharness = "codex"\nmodel = "gpt-5.6-luna"\n'
    'effort = "low"\n' for seat in ("reviewer", "adjudicator")
) + '[loop]\nreview_session = "resume"\n'
OPTIONS = ["-m", "gpt-5.6-luna", "-c", "model_reasoning_effort=low",
           "--dangerously-bypass-approvals-and-sandbox"]


class CodexTableTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("commit", "--allow-empty", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("commit", "--allow-empty", "-qm", "candidate")
        self.candidate = self.git("rev-parse", "HEAD")
        self.holo = root / "holo"
        self.holo.mkdir()
        (self.holo / "config.toml").write_text(CODEX_CONFIG)
        self.target = holophyte.target.Target(
            path=self.repo, holo_dir=self.holo, store_path=self.holo / "store.db",
            config_path=self.holo / "config.toml",
            worktrees=root / "repo.worktrees")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "codex"
        fake.write_text(f"#!{sys.executable}\n{FAKE_CODEX}")
        fake.chmod(0o755)
        self.calls = root / "calls.jsonl"
        env = patch.dict(os.environ, {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_HARNESS_CALLS": str(self.calls)})
        env.start()
        self.addCleanup(env.stop)
        self.conn = store.open(self.target.store_path)
        self.addCleanup(self.conn.close)
        store.init(self.conn)
        project = store.ensure_project(self.conn, "test", self.repo)
        ticket = store.mirror_ticket(self.conn, project, "KO-614", "KO-614",
                                     "codex", acceptance_criteria=["review"],
                                     verification_commands=["true"])
        self.run = store.claim(self.conn, project, ticket)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             *args], cwd=self.repo, text=True).strip()

    def received(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def events(self, kind):
        return [json.loads(payload) for (payload,) in self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = ? ORDER BY seq", (kind,))]

    def dispatch(self, role, goal, review_round=1):
        return holophyte.agents.agent(
            self.target, role, goal, self.repo, base_sha=self.base,
            candidate_sha=self.candidate, timeout=60, conn=self.conn,
            run_id=self.run, review_round=review_round)

    def assert_fresh_turn_in_a_candidate_checkout(self, call, goal):
        self.assertEqual(call["argv"], ["exec", *OPTIONS, goal])
        self.assertEqual(call["head"], self.candidate)
        self.assertNotEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        worktrees = self.git("worktree", "list", "--porcelain").splitlines()
        self.assertEqual([line for line in worktrees if line.startswith("worktree ")],
                         [f"worktree {self.repo.resolve()}"])

    def test_review_and_adjudicator_tables_run_codex_in_a_candidate_checkout(self):
        for role in ("review", "adjudicate"):
            with self.subTest(role=role):
                self.calls.unlink(missing_ok=True)
                self.dispatch(role, f"{role} the candidate")
                [call] = self.received()
                self.assert_fresh_turn_in_a_candidate_checkout(
                    call, f"{role} the candidate")
        [recorded] = self.events("agent_session")
        self.assertEqual((recorded["session_id"], recorded["role"],
                          recorded["round"]), ("codex-1", "review", 1))

    def test_round_two_resumes_the_recorded_id_or_runs_fresh_without_a_rollout(self):
        self.dispatch("review", "first look")
        self.dispatch("review", "second look", review_round=2)
        _, resumed = self.received()
        self.assertEqual(resumed["argv"],
                         ["exec", "resume", *OPTIONS, "codex-1", "second look"])
        self.assertEqual(resumed["head"], self.candidate)
        with patch.dict(os.environ, {"FAKE_NO_ROLLOUT": "1"}):
            self.dispatch("review", "third look", review_round=2)
        _, _, refused, fresh = self.received()
        self.assertEqual(refused["argv"][:2], ["exec", "resume"])
        self.assert_fresh_turn_in_a_candidate_checkout(fresh, "third look")
        self.assertEqual([event.get("reason") for event in
                          self.events("review_session")],
                         [None, None, "no rollout found"])


if __name__ == "__main__":
    unittest.main()

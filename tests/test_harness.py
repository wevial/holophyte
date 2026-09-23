"""Table-form roles: the adapter's argv, its session and its resume -- a
claude or codex implementer, and codex and devin reviewers in a throwaway
candidate checkout.

Run: python3 -m unittest tests.test_harness -v
"""
import json
import os
import shutil
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
import holophyte.project
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
        self.target = holophyte.project.Project(
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
    calls.write(json.dumps({"binary": sys.argv[0], "argv": sys.argv[1:],
                            "cwd": os.getcwd(), "head": head}) + "\\n")
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


class CandidateCheckoutCase(unittest.TestCase):
    """A run with a base and a candidate commit and a fake `HARNESS` on PATH
    running `FAKE`, under `CONFIG`."""
    HARNESS = FAKE = CONFIG = None

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
        (self.holo / "config.toml").write_text(self.CONFIG)
        self.target = holophyte.project.Project(
            path=self.repo, holo_dir=self.holo, store_path=self.holo / "store.db",
            config_path=self.holo / "config.toml",
            worktrees=root / "repo.worktrees")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / self.HARNESS
        fake.write_text(f"#!{sys.executable}\n{self.FAKE}")
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
                                     self.HARNESS, acceptance_criteria=["review"],
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

    def assert_in_a_candidate_checkout(self, call):
        self.assertEqual(call["head"], self.candidate)
        self.assertNotEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        worktrees = self.git("worktree", "list", "--porcelain").splitlines()
        self.assertEqual([line for line in worktrees if line.startswith("worktree ")],
                         [f"worktree {self.repo.resolve()}"])


class CodexTableTests(CandidateCheckoutCase):
    HARNESS, FAKE, CONFIG = "codex", FAKE_CODEX, CODEX_CONFIG

    def assert_fresh_turn_in_a_candidate_checkout(self, call, goal):
        self.assertEqual(call["argv"], ["exec", *OPTIONS, goal])
        self.assert_in_a_candidate_checkout(call)

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

    def test_a_container_implementer_leaves_the_reviewer_its_harness_path(self):
        pinned = self.holo / "pinned" / "codex"
        pinned.parent.mkdir()
        shutil.copy2(shutil.which("codex"), pinned)
        (self.holo / "config.toml").write_text(
            '[agents]\nimplementer_isolation = "container"\n' + CODEX_CONFIG
            + f'[harnesses]\ncodex = "{pinned}"\n')
        self.target = holophyte.project.Project(
            path=self.repo, holo_dir=self.holo, store_path=self.target.store_path,
            config_path=self.target.config_path, worktrees=self.target.worktrees)
        self.dispatch("review", "review the candidate")
        [call] = self.received()
        self.assertEqual(call["binary"], str(pinned))
        self.assert_fresh_turn_in_a_candidate_checkout(call, "review the candidate")

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


# The fake devin: records every call with its cwd's HEAD, and answers
# `list --format json` with the one session Devin keeps per directory.
FAKE_DEVIN = """
import json, os, subprocess, sys
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                      text=True).stdout.strip()
with open(os.environ["FAKE_HARNESS_CALLS"], "a") as calls:
    calls.write(json.dumps({"binary": sys.argv[0], "argv": sys.argv[1:],
                            "cwd": os.getcwd(), "head": head}) + "\\n")
if sys.argv[1:] == ["list", "--format", "json"]:
    print(json.dumps([{"id": "amenable-astrodon", "short_id": "amenable-astrodon",
                       "working_directory": os.getcwd(), "title": "review"}]))
else:
    print("APPROVE")
"""

DEVIN_CONFIG = ('[agents.reviewer]\nharness = "devin"\nmodel = "opus"\n'
                '[loop]\nreview_session = "resume"\n')
DEVIN_OPTIONS = ["--model", "opus", "--permission-mode", "dangerous",
                 "--respect-workspace-trust", "false"]


class DevinTableTests(CandidateCheckoutCase):
    HARNESS, FAKE, CONFIG = "devin", FAKE_DEVIN, DEVIN_CONFIG

    def test_a_first_round_runs_in_a_candidate_checkout_and_lists_its_session(self):
        self.dispatch("review", "review the candidate")
        turn, listed = self.received()
        self.assertEqual(turn["argv"], [*DEVIN_OPTIONS, "-p", "review the candidate"])
        self.assert_in_a_candidate_checkout(turn)
        self.assertEqual((listed["argv"], listed["cwd"]),
                         (["list", "--format", "json"], turn["cwd"]))
        [recorded] = self.events("agent_session")
        self.assertEqual((recorded["session_id"], recorded["round"]),
                         ("amenable-astrodon", 1))

    def test_round_two_resumes_the_listed_session(self):
        self.dispatch("review", "first look")
        self.dispatch("review", "second look", review_round=2)
        _, _, resumed, _ = self.received()
        self.assertEqual(resumed["argv"], [*DEVIN_OPTIONS, "-r", "amenable-astrodon",
                                           "-p", "second look"])
        self.assert_in_a_candidate_checkout(resumed)


# The fake codex implementer: records its argv, prints the banner to stderr
# as Codex does, and sleeps past the budget when the prompt asks it to.
FAKE_CODEX_IMPLEMENTER = """
import json, os, sys, time
with open(os.environ["FAKE_HARNESS_CALLS"], "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
print("session id: " + os.environ["FAKE_SESSION"], file=sys.stderr)
sys.stderr.flush()
if "stall" in sys.argv[-1]:
    time.sleep(30)
"""
SESSION = "0199a0b1-7c2d-7e3f-8a4b-5c6d7e8f9a0b"
IMPLEMENTER_OPTIONS = ["--dangerously-bypass-approvals-and-sandbox",
                       "--skip-git-repo-check", "-m", "gpt-5.6-luna",
                       "-c", "model_reasoning_effort=low"]


class CodexImplementerTests(ClaudeTableTests):
    def setUp(self):
        super().setUp()
        self.target.config_path.write_text(
            '[agents.implementer]\nharness = "codex"\nmodel = "gpt-5.6-luna"\n'
            'effort = "low"\n[loop]\nfix_session = "resume"\n')
        fake = Path(os.environ["PATH"].split(os.pathsep)[0]) / "codex"
        fake.write_text(f"#!{sys.executable}\n{FAKE_CODEX_IMPLEMENTER}")
        fake.chmod(0o755)
        env = patch.dict(os.environ, {"FAKE_SESSION": SESSION})
        env.start()
        self.addCleanup(env.stop)

    def implement(self, goal, budget_min=1):
        return holophyte.loop._timed(self.target, self.conn, self.run, 60,
                                     self.repo, budget_min, goal)

    def test_implement_turn_runs_the_adapter_argv_and_records_its_session(self):
        _, timed_out = self.implement("implement the thing")
        self.assertFalse(timed_out)
        self.assertEqual(self.received(),
                         [["exec", *IMPLEMENTER_OPTIONS, "implement the thing"]])
        self.assertEqual(self.session(), SESSION)

    def test_a_turn_the_timeout_ends_still_leaves_its_session(self):
        _, timed_out = self.implement("stall until the cap", budget_min=1 / 60)
        self.assertTrue(timed_out)
        self.assertEqual(self.received(),
                         [["exec", *IMPLEMENTER_OPTIONS, "stall until the cap"]])
        self.assertEqual(self.session(), SESSION)

    def test_fix_turn_resumes_the_recorded_session(self):
        self.implement("implement the thing")
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                      text=True).strip()
        holophyte.fix_session.fix_turn(
            self.target, self.conn, self.run, 60, self.repo, 1, "the ticket",
            "REQUEST_CHANGES: a finding", sha, timed=holophyte.loop._timed,
            check_cap=lambda *args: None)
        _, resumed = self.received()
        self.assertEqual(resumed[:-1], ["exec", "resume", SESSION,
                                        *IMPLEMENTER_OPTIONS])
        self.assertTrue(resumed[-1].startswith("Reviewer findings:"))
        self.assertIn("REQUEST_CHANGES: a finding", resumed[-1])
        [(payload,)] = self.conn.execute(
            "SELECT payload FROM runEvents WHERE kind = 'fix_session'").fetchall()
        self.assertEqual(json.loads(payload), {"arm": "resume", "resumed": True})


if __name__ == "__main__":
    unittest.main()

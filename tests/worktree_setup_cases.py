"""Existing setup cases shared by test_claim's worktree test class.

Kept as a mixin so the required test_claim command still runs every case
while its new environment acceptance tests stay within the module ceiling.
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from fake_agent import APPROVE, Commit, _git
from loop_fixture import BRANCH, StubProvider, a_task

import holophyte.claim
import holophyte.environment_git
import holophyte.merge_gate
import holophyte.redact
import store


class WorktreeSetupCases:
    def assert_environment_merge_refused(self, remove):
        source = self.target.parent / "source.env"
        source.write_text("PUBLIC=sentinel-local-merge-value\n")
        self.configure(f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n')

        class UnsafeCommit(Commit):
            def play(self, cwd, turn):
                _git(cwd, "add", "-f", ".env")
                super().play(cwd, turn)
                if remove:
                    _git(cwd, "rm", ".env")
                    _git(cwd, "commit", "-m", "remove environment")
                return "candidate ready"

        self.main_output(UnsafeCommit("unsafe candidate"), APPROVE)
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.git("log", "main", "--format=%H", "--", ".env"), "")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        reason = self.read("SELECT outcomeReason FROM runs")[0][0]
        self.assertIn(".env", reason)
        self.assertIn("refusing to merge", reason)
        self.assertIn(BRANCH, self.branches())

    def test_local_merge_refuses_environment_in_candidate_tree(self):
        self.assert_environment_merge_refused(remove=False)

    def test_local_merge_refuses_environment_removed_from_candidate_tree(self):
        self.assert_environment_merge_refused(remove=True)

    def test_interrupted_environment_write_cannot_be_staged(self):
        source = self.target.parent / "source.env"
        source.write_text("PUBLIC=sentinel-interrupted-value\n")
        self.configure(f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n')
        wt = self.target.parent / "interrupted"
        self.git("worktree", "add", "-b", BRANCH, str(wt), "main")
        # Exit after the temporary file is written, without running finally,
        # as a SIGKILL between the write and the atomic rename would do.
        result = subprocess.run([sys.executable, "-c", "\n".join([
            "import os, sys",
            "from pathlib import Path",
            "from unittest.mock import patch",
            "from holophyte.claim import write_worktree_environment",
            "from holophyte.target import Target",
            "target = Target.locate(Path(sys.argv[1]))",
            "with patch('holophyte.claim.os.replace',",
            "           side_effect=lambda *a: os._exit(37)):",
            "    write_worktree_environment(target, Path(sys.argv[2]))",
        ]), str(self.target), str(wt)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 37, result.stderr)
        git_dir = Path(_git(wt, "rev-parse", "--absolute-git-dir"))
        leftovers = list(wt.glob(".env-*")) + list(git_dir.glob(".env-*"))
        self.assertEqual(len(leftovers), 1)
        self.assertEqual(leftovers[0].read_text(),
                         "PUBLIC=sentinel-interrupted-value\n")
        shared = self.target / ".git" / ".env-other-writer"
        shared.write_text("another writer")
        sibling = self.target.parent / "sibling"
        self.git("worktree", "add", "--detach", str(sibling), "main")
        sibling_temp = (Path(_git(sibling, "rev-parse", "--absolute-git-dir"))
                        / ".env-active")
        sibling_temp.write_text("another worktree")
        directory = git_dir / ".env-directory"
        directory.mkdir()
        symlink = git_dir / ".env-symlink"
        symlink.symlink_to(shared)
        (wt / "work.txt").write_text("candidate work\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "candidate after interrupted setup")
        self.assertNotIn("sentinel-interrupted-value", _git(wt, "log", "-p"))
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
        self.assertEqual((wt / ".env").read_text(),
                         "PUBLIC=sentinel-interrupted-value\n")

        self.assertFalse(leftovers[0].exists())
        self.assertEqual(shared.read_text(), "another writer")
        self.assertEqual(sibling_temp.read_text(), "another worktree")
        self.assertTrue(directory.is_dir())
        self.assertTrue(symlink.is_symlink())
        # The primary checkout must never sweep the shared Git directory.
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, self.target)[0])
        stale = self.target / ".git" / "holophyte-env" / ".env-interrupted"
        stale.write_text("interrupted primary checkout write")
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, self.target)[0])
        self.assertFalse(stale.exists())
        self.assertEqual(shared.read_text(), "another writer")
        self.assertEqual(sibling_temp.read_text(), "another worktree")

    def test_environment_check_pins_push_and_merge_to_checked_commit(self):
        source = self.target.parent / "source.env"
        source.write_text("PUBLIC=sentinel-racing-value\n")
        self.configure(f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n')
        remote = self.target.parent / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        check = holophyte.environment_git.refuse_environment_history
        for operation in ("push", "merge"):
            with self.subTest(operation=operation):
                branch = f"task/{operation}"
                wt = self.target.parent / operation
                self.git("worktree", "add", "-b", branch, str(wt), "main")
                (wt / "work.txt").write_text(operation)
                _git(wt, "add", "-A")
                _git(wt, "commit", "-m", "safe candidate")
                safe = _git(wt, "rev-parse", "HEAD")
                (wt / ".env").write_text("PUBLIC=sentinel-racing-value\n")
                _git(wt, "add", "-f", ".env")
                _git(wt, "commit", "-m", "unsafe concurrent candidate")
                unsafe = _git(wt, "rev-parse", "HEAD")
                _git(wt, "reset", "--hard", safe)

                def move_after_check(target, name, *, action):
                    checked = check(target, name, action=action)
                    self.git("update-ref", f"refs/heads/{name}", unsafe)
                    return checked

                module = (holophyte.environment_git if operation == "push"
                          else holophyte.merge_gate)
                with patch.object(module, "refuse_environment_history",
                                  move_after_check):
                    if operation == "push":
                        holophyte.pr.push_branch(self.tgt, branch)
                        landed = _git(remote, "rev-parse", f"refs/heads/{branch}")
                    else:
                        with patch.object(module, "set_phase"), patch.object(
                                module, "commit_findings"):
                            module._merge(self.tgt, None, None, None, "KO-131",
                                          "task", branch, wt, safe)
                        landed = self.git("rev-parse", "main^2").strip()
                self.assertEqual(landed, safe)

    def test_baseline_evidence_survives_source_redaction(self):
        self.main_output(Commit("candidate"), APPROVE)
        conn = store.open(str(self.db), migrate="owner")
        self.addCleanup(conn.close)
        with patch.object(holophyte.redact, "_environment_values", frozenset()):
            holophyte.redact.register_values(["base"])
            output = holophyte.gates.VerificationOutput(
                "base", [{"source": "baseline", "output": "base"}])
            holophyte.gates.record_unreviewed_verification(conn, 1, output)
        rows = json.loads(self.read(
            "SELECT verificationResults FROM reviewRounds ORDER BY round DESC"
        )[0][0])
        self.assertEqual(rows[-1],
                         {"source": "[redacted]line", "output": "[redacted]"})

    def test_event_payload_redacts_escaped_json_and_plain_text(self):
        self.main_output(Commit("candidate"), APPROVE)
        conn = store.open(str(self.db), migrate="owner")
        self.addCleanup(conn.close)
        values = ['sentinel"quote', "sentinel\nnewline", "sentinel\\slash"]
        with patch.object(holophyte.redact, "_environment_values", frozenset()):
            holophyte.redact.register_values(values)
            for value in values:
                for encoded in (False, True):
                    with self.subTest(value=value, encoded=encoded):
                        payload = json.dumps({"output": [value]}) if encoded else value
                        store.record_event(conn, 1, "escaped", "diagnostic",
                                           level="detail", payload=payload)
                        saved = self.read("SELECT payload FROM runEvents "
                                          "ORDER BY seq DESC LIMIT 1")[0][0]
                        self.assertEqual(
                            json.loads(saved) if encoded else saved,
                            {"output": ["[redacted]"]} if encoded else "[redacted]")

    def test_recovery_unstages_environment_when_it_is_the_only_change(self):
        source = self.target.parent / "source.env"
        source.write_text("PUBLIC=sentinel-recovery-value\n")
        self.configure(f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n')
        for recovery in ("leftover", "timeout"):
            with self.subTest(recovery=recovery):
                wt = self.target.parent / recovery
                branch = f"task/{recovery}"
                self.git("worktree", "add", "-b", branch, str(wt), "main")
                self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
                _git(wt, "add", "-f", ".env")
                if recovery == "leftover":
                    ok, reason = holophyte.claim.reuse_leftover(
                        self.tgt, wt, branch, sync_origin=False)
                    self.assertTrue(ok, reason)
                else:
                    # A killed staging process can leave its lock behind too.
                    lock = Path(_git(
                        wt, "rev-parse", "--git-path", "index.lock").strip())
                    lock.touch()
                    with patch.object(holophyte.loop, "_check_run_cap"), patch.object(
                            holophyte.loop, "_transport_timed",
                            return_value=("", True)):
                        with self.assertRaises(holophyte.gates.RunFailure):
                            holophyte.loop._implement(
                                self.tgt, None, None, "KO-131", "task", branch,
                                wt, False, 1, self.base, "ticket", "", 5)
                self.assertEqual(_git(wt, "ls-files", "--", ".env"), "")
                self.assertEqual(_git(wt, "rev-parse", "HEAD").strip(), self.base)
                self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])

    def test_environment_stays_out_of_reclaim_and_candidate_commits(self):
        source = self.target.parent / "source.env"
        source.write_text("PUBLIC=sentinel-git-value\n")
        self.configure(f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n')
        wt = self.target.parent / "reused"
        self.git("worktree", "add", "-b", BRANCH, str(wt), "main")
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
        self.assertEqual(_git(wt, "check-ignore", ".env").strip(), ".env")
        # Reclaim must protect it even if the local exclusion is lost.
        exclude = Path(_git(wt, "rev-parse", "--git-path", "info/exclude").strip())
        exclude.write_text("")
        _git(wt, "add", "-f", ".env")
        (wt / "work.txt").write_text("preserved work\n")
        ok, reason = holophyte.claim.reuse_leftover(
            self.tgt, wt, BRANCH, sync_origin=False)
        self.assertTrue(ok, reason)
        self.assertNotIn(".env",
                         _git(wt, "ls-tree", "--name-only", "HEAD").splitlines())
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
        (wt / "work.txt").write_text("candidate work\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "candidate")
        self.assertEqual(_git(wt, "log", "--format=%H", "--", ".env"), "")
        _git(wt, "add", "-f", ".env")
        _git(wt, "commit", "-m", "unsafe candidate")
        with self.assertRaisesRegex(holophyte.gates.InfraFailure, r"\.env"):
            holophyte.pr.push_branch(self.tgt, BRANCH)
        _git(wt, "rm", ".env")
        _git(wt, "commit", "-m", "remove unsafe file")
        with self.assertRaisesRegex(holophyte.gates.InfraFailure, r"\.env"):
            holophyte.pr.push_branch(self.tgt, BRANCH)

    def test_source_disappearing_after_startup_releases_run(self):
        source = self.target.parent / "source.env"
        source.write_text("PUBLIC=sentinel-vanishing-value\n")
        self.configure(f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n')
        setup = holophyte.claim.run_worktree_setup

        def remove_source(*args, **kwargs):
            source.unlink()
            return setup(*args, **kwargs)

        with patch.object(holophyte.claim, "run_worktree_setup", remove_source):
            output = self.main_output(provider=StubProvider(a_task()))
        self.assertIn("env_source could not be read", output)
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])

    def test_setup_enables_hooks_only_when_directory_exists(self):
        wt = self.target.parent / "hooks-worktree"
        self.git("worktree", "add", "--detach", str(wt))
        for present in (False, True):
            with self.subTest(present=present):
                self.configure('[worktree]\nsetup = ["mkdir .githooks"]\n'
                               if present else '')
                self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
                result = subprocess.run(
                    ["git", "config", "--get", "core.hooksPath"],
                    cwd=wt, capture_output=True, text=True)
                self.assertEqual(result.stdout.strip(), ".githooks" if present else "")
                self.assertEqual(result.returncode, 0 if present else 1)

    def test_setup_runs_in_the_fresh_worktree_before_the_implementer(self):
        """The commands run in the task worktree — not the main checkout —
        while the branch is cut and before any agent turn, and the run merges
        as it otherwise would."""
        marker = self.target.parent / "where.txt"
        self.configure(f'[worktree]\nsetup = ["pwd > {marker}"]\n')

        fake, guard = self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(marker.read_text().strip(),
                         str((self.worktrees / "ko-131-add-a-thing").resolve()))
        self.assertEqual(guard.spawned, [])
        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertIn("the scripted work", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_failed_setup_fails_the_run_before_any_agent_turn(self):
        """No agent is dispatched — the script is empty, so a turn would raise
        — main is untouched, and the branch is discarded rather than preserved:
        nothing was implemented on it."""
        provider = StubProvider(a_task(1), a_task(2))
        self.configure('[worktree]\nsetup = ["echo no toolchain here; exit 3"]\n')

        fake, guard = self.loop(provider=provider)

        self.assertEqual(fake.roles, [])
        self.assertEqual(guard.spawned, [])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        # A toolchain outage says nothing about the ticket: no agent ran, so
        # the failure must not spend one of its escalation strikes.
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(len(provider.queue), 1)  # the loop stopped
        # The ticket carries the reason, with the failing command and what it
        # printed — a run that ended before anything ran leaves no other trace.
        (_, body), = provider.comments
        self.assertIn("worktree setup", body)
        self.assertIn("no toolchain here", body)
        self.assertIn("exit 3", body)

    def test_a_failing_setup_leaves_a_reused_worktree_as_found(self):
        """A setup failure says nothing about the preserved work a reused
        worktree may hold; only a branch the run cut fresh is discarded."""
        wt = self.worktrees / "ko-131-add-a-thing"
        self.git("worktree", "add", "--detach", str(wt), "main")
        self.git("checkout", "-b", BRANCH, cwd=wt)
        (wt / "rescued.txt").write_text("rescued work\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)
        self.configure('[worktree]\nsetup = ["exit 3"]\n')

        self.loop()

        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertTrue((wt / "rescued.txt").exists())
        self.assertIn("rescued: preserved work", self.subjects(BRANCH))
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("left in place", reason)

    def test_the_setup_phase_is_recorded_between_cutting_and_working(self):
        self.configure('[worktree]\nsetup = ["true"]\n')

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.transitions()[:3],
                         ["claimed -> working", "working -> working",
                          "working -> verifying"])
        (note,) = [summary for (summary,) in
                   self.read("SELECT summary FROM runEvents ORDER BY seq")
                   if "worktree setup" in summary]
        self.assertIn("1 command(s)", note)

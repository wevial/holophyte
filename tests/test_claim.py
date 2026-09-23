from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Both discovery and named-module unittest commands need the harness on sys.path.
sys.path.insert(0, str(HERE))
from babysit_fixture import ResolveMerge  # noqa: E402
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    FAIL,
    REQUEST_CHANGES,
    REVIEW_ROLES,
    Commit,
    Idle,
    _git,
    block_until_killed,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    CommitThenTimeout,
    IdleThenTimeout,
    LoopFixture,
    MergeModeFixture,
    StubProvider,
    a_task,
)
from worktree_setup_cases import WorktreeSetupCases  # noqa: E402

import holophyte.board  # noqa: E402 - after the sys.path insert above
import holophyte.claim  # noqa: E402 - after the sys.path insert above
import holophyte.environment_git  # noqa: E402
import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


class BabysitClaimTests(MergeModeFixture):
    def test_send_back_claim_resumes_the_parked_candidate(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("candidate"), APPROVE, Idle(""), provider=self.provider())
        candidate = self.git("rev-parse", BRANCH).strip()
        self.assertIn("PR open:", self.question())
        holophyte.operator.babysit_ticket(
            self.tgt, "KO-131", "sent back to the babysitter", out=io.StringIO())
        self.assertEqual(self.read("SELECT blockedQuestion FROM tickets"), [(None,)])
        observed = []
        set_phase = holophyte.pullrequest.set_phase

        def observe_resume(conn, run_id, phase, note):
            observed.append(self.read(
                "SELECT prUrl, candidateSha, phase, endedAt FROM runs"
                f" WHERE id = {run_id}"))
            self.assertTrue(self.read(
                "SELECT summary FROM runEvents"
                f" WHERE runId = {run_id} AND summary LIKE 'resuming run %'"))
            return set_phase(conn, run_id, phase, note)

        with patch.object(holophyte.pullrequest, "set_phase", observe_resume):
            output = self.main_output(provider=self.provider())
        self.assertEqual(observed, [[(self.URL, candidate, "claimed", None)]])
        self.assertNotIn("parked on PR", output)
        self.assertEqual(self.read("SELECT id, candidateSha FROM runs ORDER BY id"),
                         [(1, candidate), (2, candidate)])
        self.assertEqual([k for k, _ in self.api_calls()], ["state", "state"])

    def test_fresh_claim_has_no_pull_request_or_candidate(self):
        observed = []
        cut = holophyte.loop._cut_worktree

        def observe_claim(target, conn, run_id, *args):
            observed.append(self.read(
                "SELECT prUrl, candidateSha, phase FROM runs"
                f" WHERE id = {run_id}"))
            return cut(target, conn, run_id, *args)

        with patch.object(holophyte.loop, "_cut_worktree", observe_claim):
            self.loop(Commit("fresh candidate"), APPROVE)
        self.assertEqual(observed, [[(None, None, "claimed")]])


class WorktreeSetupLoopTests(WorktreeSetupCases, LoopFixture):
    def test_filtered_environment_precedes_setup_and_never_reaches_records(self):
        source = self.target.parent / "source.env"
        source.write_text(
            "# ignored\n\nexport PUBLIC=sentinel-public-value\n"
            'QUOTED="sentinel quoted value"\n'
            "AUTH_KEY=sentinel-auth-value\nDB_KEY=sentinel-db-value\n"
            "OTHER=sentinel-other-value\n")
        capture = self.target.parent / "capture.env"
        capture.write_text("CAPTURE_KEY=sentinel-capture\n")
        seen = self.target.parent / "seen.env"
        mode = self.target.parent / "seen.mode"
        self.configure(
            f'[worktree]\nenv_source = "{source}"\n'
            'env_allow = ["PUBLIC", "QUOTED"]\n'
            f'setup = ["cp .env {seen}; '
            f'(stat -c %a .env 2>/dev/null || stat -f %Lp .env) > {mode}; '
            f'echo sentinel-public-value", "cat {source}; exit 3"]\n'
            f'[merge]\ncapture_env_source = "{capture}"\n'
            'capture_env_allow = ["CAPTURE_KEY"]\n')
        provider = StubProvider(a_task())
        out = self.main_output(provider=provider)
        self.assertEqual(seen.read_text(),
                         'PUBLIC=sentinel-public-value\n'
                         'QUOTED="sentinel quoted value"\n')
        self.assertNotIn("CAPTURE_KEY", seen.read_text())
        self.assertNotIn("sentinel-capture", seen.read_text())
        self.assertEqual(mode.read_text().strip(), "600")
        conn = store.open(str(self.db), migrate="owner")
        try:
            store.record_event(conn, 1, "diagnostic", "sentinel-public-value",
                               level="detail", payload="sentinel quoted value")
            store.record_ledger(conn, 1, "failure", "sentinel-db-value")
            store.record_review_round(
                conn, 1, 1, "pass", "reviewer",
                verification_results=[{"output": "sentinel-auth-value"}])
            output = holophyte.gates.VerificationOutput(
                "sentinel-other-value",
                [{"source": "baseline", "output": "sentinel-other-value"}])
            holophyte.gates.record_unreviewed_verification(conn, 1, output)
            store.resume(conn, 1)
            store.release(conn, 1, "failed", "sentinel-db-value")
            ticket_id = conn.execute("SELECT id FROM tickets").fetchone()[0]
            self.assertTrue(holophyte.board.block_ticket(
                conn, ticket_id, provider, "verify failed: sentinel-public-value"))
        finally:
            conn.close()
        records = repr(self.read("SELECT * FROM runEvents"))
        records += repr(self.read("SELECT * FROM ledger"))
        records += repr(self.read("SELECT outcomeReason FROM runs"))
        records += repr(self.read("SELECT blockedQuestion FROM tickets"))
        rounds = self.read("SELECT verificationResults FROM reviewRounds")
        self.assertEqual(len(json.loads(rounds[0][0])), 2)
        records += repr(rounds)
        records += repr(provider.comments) + out
        for value in ("sentinel-public-value", "sentinel quoted value",
                      "sentinel-auth-value", "sentinel-db-value",
                      "sentinel-other-value"):
            self.assertNotIn(value, records)
        self.assertIn("[redacted]", records)

    def test_environment_replaces_existing_symlink_without_changing_its_target(self):
        source = self.target.parent / "source.env"
        source.write_text("PUBLIC=sentinel-link-value\n")
        wt = self.target.parent / "reused"
        self.git("worktree", "add", "--detach", str(wt), "main")
        (wt / ".env").symlink_to(source)
        self.configure(f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n')
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
        self.assertFalse((wt / ".env").is_symlink())
        self.assertEqual((wt / ".env").stat().st_mode & 0o777, 0o600)
        (wt / ".env").write_text("checkout changes\n")
        self.assertEqual(source.read_text(), "PUBLIC=sentinel-link-value\n")

    def test_local_capture_spec_stays_on_disk_and_out_of_the_commit(self):
        wt = self.target.parent / "capture-local"
        self.git("worktree", "add", "-b", "capture-local", str(wt), "main")
        self.configure('[merge]\nui_capture_dir = ".holophyte-capture"\n'
                       "ui_capture_local = true\n")
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
        spec = wt / ".holophyte-capture" / "KO-7.capture.ts"
        spec.write_text("test('capture', () => {});\n")
        (wt / "work.txt").write_text("the ticket's change\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-qm", "candidate", cwd=wt)
        tree = self.git("ls-tree", "-r", "--name-only", "HEAD", cwd=wt)
        self.assertIn("work.txt", tree.split())
        self.assertNotIn(".holophyte-capture", tree)
        self.assertTrue(spec.is_file())

    def test_local_capture_setup_refuses_to_write_through_a_symlink(self):
        self.configure('[merge]\nui_capture_dir = "specs/capture"\n'
                       "ui_capture_local = true\n")
        outside = self.target.parent / "outside"
        outside.mkdir()
        (outside / ".gitignore").write_text("kept\n")
        for name, link, points_at in (
                ("ancestor", "specs", outside),
                ("file", "specs/capture/.gitignore", outside / ".gitignore")):
            with self.subTest(name):
                wt = self.target.parent / f"capture-{name}"
                self.git("worktree", "add", "--detach", str(wt), "main")
                (wt / link).parent.mkdir(parents=True, exist_ok=True)
                (wt / link).symlink_to(points_at)
                ok, report = holophyte.claim.run_worktree_setup(self.tgt, wt)
                self.assertFalse(ok)
                self.assertIn("symlink", report)
                self.assertEqual(sorted(p.name for p in outside.iterdir()),
                                 [".gitignore"])
                self.assertEqual((outside / ".gitignore").read_text(), "kept\n")

    def test_capture_directory_untouched_without_local_key(self):
        wt = self.target.parent / "capture-kept"
        self.git("worktree", "add", "--detach", str(wt), "main")
        self.configure('[merge]\nui_capture_dir = ".holophyte-capture"\n')
        self.assertTrue(holophyte.claim.run_worktree_setup(self.tgt, wt)[0])
        self.assertFalse((wt / ".holophyte-capture").exists())

    def test_without_environment_keys_setup_writes_no_environment(self):
        seen = self.target.parent / "env-absent"
        self.configure(f'[worktree]\nsetup = ["test ! -e .env && touch {seen}"]\n')
        self.loop(Commit("the scripted work"), APPROVE)
        self.assertTrue(seen.exists())


class SkipLineTests(unittest.TestCase):
    """The admit step's line for a parked ticket names why it is parked
    (KO-345): the strike-out, the pull request awaiting `--approve`, or the
    question -- so a ticket parked for the operator's merge is not reported
    as "repeated failures" that never happened."""

    def test_the_three_parks_read_as_what_they_are(self):
        struck = holophyte.claim.skip_line("KO-131", 2, None, None)
        self.assertIn("2 failures", struck)
        self.assertIn("a human owns it now", struck)

        url = "https://github.com/example/repo/pull/7"
        parked = holophyte.claim.skip_line("KO-131", 0, url,
                                          f"PR open: {url}\nready to merge")
        self.assertIn(url, parked)
        self.assertIn("--approve KO-131", parked)
        self.assertNotIn("fail", parked)

        asked = holophyte.claim.skip_line(
            "KO-131", 0, None, "merge?\nthe branch is at abc123")
        self.assertIn("a question: merge?;", asked)
        self.assertNotIn("abc123", asked)
        self.assertNotIn("fail", asked)

        closed = holophyte.claim.skip_line(
            "KO-131", 0, url, f"rejected: {url}", "pull_request_closed")
        self.assertIn(f"a question: rejected: {url};", closed)
        self.assertNotIn("--approve", closed)

    def test_a_module_question_outranks_the_strike_count(self):
        """The run that parked the ticket on a merge conflict may also be
        the failure that reached the threshold. The conflict is what the
        operator has to resolve, so it is the line -- and since KO-365 the
        line names the way back, `--requeue`; the escalation's own
        question is the one park the count speaks for."""
        conflicted = holophyte.claim.skip_line(
            "KO-131", 2, None,
            "merge conflict with main on: README.md; resolve it on the branch")
        self.assertIn("parked on a merge-gate conflict; resolve the branch"
                      " and --requeue KO-131", conflicted)
        self.assertNotIn("struck out", conflicted)
        self.assertNotIn("a question", conflicted)

        struck = holophyte.claim.skip_line(
            "KO-131", 2, None, holophyte.board.strike_question(2))
        self.assertIn("struck out after 2 failures", struck)
        self.assertNotIn("a question", struck)


class TicketNameTests(LoopFixture):
    """The branch and worktree a run cuts are named after the ticket, not the
    title alone: the identifier leads, so an operator can map any preserved
    `task/*` branch back to its ticket from `git branch`, and two titles that
    truncate to the same slug never land in the same worktree."""

    def merges(self):
        return [s for s in self.subjects() if s.startswith("Merge task/")]

    def test_the_branch_and_worktree_carry_the_lowercased_identifier(self):
        provider = StubProvider({**a_task(), "id": "KO-150",
                                 "title": "Supervisor 5/5: config"})

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.turns[0].cwd.name, "ko-150-supervisor-5-5-config")
        self.assertEqual(self.merges(),
                         ["Merge task/ko-150-supervisor-5-5-config: "
                          "Supervisor 5/5: config"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_titles_sharing_a_thirty_character_prefix_get_distinct_names(self):
        """Both titles truncate to `supervisor-worktree-reuse-on-a`; without
        the identifier the second run would land in the first's worktree."""
        shared = "Supervisor: worktree reuse on "
        self.assertEqual(len(shared), 30)
        provider = StubProvider(
            {**a_task(1), "title": shared + "a clean failure"},
            {**a_task(2), "title": shared + "a dirty failure"})

        fake, _ = self.loop(Commit("first ticket"), APPROVE,
                            Commit("second ticket"), APPROVE,
                            provider=provider)

        cut = [turn.cwd.name for turn in fake.turns if turn.role == "implement"]
        self.assertEqual(len(cut), 2)
        self.assertNotEqual(cut[0], cut[1])
        merged = self.merges()
        self.assertEqual(len(merged), 2)
        self.assertNotEqual(merged[0].split(":")[0], merged[1].split(":")[0])
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("merged",), ("merged",)])

    def test_the_prefix_comes_from_the_worktree_table(self):
        """`[worktree] branch_prefix = "factory"` puts `factory/` ahead of the
        identifier; the worktree directory does not carry it and is unchanged."""
        self.configure('[worktree]\nbranch_prefix = "factory"\n')
        provider = StubProvider({**a_task(), "id": "KO-7000"})

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.turns[0].cwd.name, "ko-7000-add-a-thing")
        self.assertEqual(
            [s for s in self.subjects() if s.startswith("Merge ")],
            ["Merge factory/ko-7000-add-a-thing: add a thing"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_worktree_table_without_the_key_keeps_the_task_prefix(self):
        self.configure('[worktree]\nsetup = ["true"]\n')
        provider = StubProvider({**a_task(), "id": "KO-7000"})

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.turns[0].cwd.name, "ko-7000-add-a-thing")
        self.assertEqual(self.merges(),
                         ["Merge task/ko-7000-add-a-thing: add a thing"])


class LeftoverWorktreeTests(LoopFixture):
    def test_pause_after_review_resumes_findings_without_repeating_review(self):
        from pause_fixture import PauseReply

        from holophyte.stop import command
        self.loop(Commit(), PauseReply(self.db, REQUEST_CHANGES))
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "addressing")])
        command(self.tgt, "KO-131", None, resume=True)
        fake, _ = self.loop(Commit("address recorded findings"), APPROVE)
        self.assertIn("scripted change is incomplete", fake.turns[0].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])

    def test_pause_after_approval_preserves_human_merge_gate(self):
        from pause_fixture import PauseReply

        from holophyte.stop import command
        self.configure('[merge]\napprove = "human"\n')
        self.loop(Commit(), PauseReply(self.db, APPROVE))
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "merge_gate")])
        command(self.tgt, "KO-131", None, resume=True)
        self.loop()  # No new agent turn, and no merge approval granted by resume.
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        self.assertEqual(self.read("SELECT phase FROM runs ORDER BY id"),
                         [("paused",), ("awaiting_merge_approval",)])

    def test_resumed_pause_reuses_worktree_without_implementing(self):
        from pause_fixture import PauseEdit

        from holophyte.stop import command
        self.loop(PauseEdit(self.db))
        wt = self.worktrees / "ko-131-add-a-thing"
        original = wt.stat().st_ino
        command(self.tgt, "KO-131", None, resume=True)
        # An APPROVE-only script fails if another implement turn is dispatched.
        cut = holophyte.claim._cut_worktree
        def reuse(*args, **kwargs):
            self.assertEqual(wt.stat().st_ino, original)
            return cut(*args, **kwargs)
        with patch.object(holophyte.loop, "_cut_worktree", side_effect=reuse):
            self.loop(APPROVE)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])
        self.assertEqual(self.git("show", "main:pause-work.txt"),
                         "preserve this uncommitted work\n")


    def leftover(self):
        """A registered leftover worktree on BRANCH, as a failed run leaves it."""
        wt = self.worktrees / "ko-131-add-a-thing"
        self.git("worktree", "add", "--detach", str(wt), "main")
        self.git("checkout", "-b", BRANCH, cwd=wt)
        return wt

    def environment(self):
        source = self.target.parent / "source.env"
        source.write_text("PUBLIC=checkout-only\n")
        self.configure(f'[worktree]\nenv_source = "{source}"\n'
                       'env_allow = ["PUBLIC"]\n')

    def test_staging_environment_keeps_only_source_in_index_and_commit(self):
        self.environment()
        for own_ignore, forced in ((False, False), (True, False), (False, True)):
            with self.subTest(own_ignore=own_ignore, forced=forced):
                wt = self.worktrees / f"stage-{own_ignore}-{forced}"
                self.git("worktree", "add", "--detach", str(wt), "main")
                if own_ignore:
                    (wt / ".gitignore").write_text("/.env\n")
                holophyte.claim.write_worktree_environment(self.tgt, wt)
                self.assertTrue((wt / ".env").is_file())
                rule = ".gitignore" if own_ignore else "info/exclude"
                self.assertIn(rule, self.git("check-ignore", "-v", ".env", cwd=wt))
                (wt / "source.py").write_text("print('work')\n")
                if forced:
                    self.git("add", "-f", ".env", cwd=wt)
                    self.assertIn(".env", self.git("ls-files", cwd=wt).splitlines())
                holophyte.environment_git.stage_work(self.tgt, wt)
                staged = self.git("diff", "--cached", "--name-only", cwd=wt)
                self.assertIn("source.py", staged.splitlines())
                self.assertNotIn(".env", staged.splitlines())
                self.git("commit", "-qm", "source work", cwd=wt)
                tree = self.git("ls-tree", "-r", "--name-only", "HEAD", cwd=wt)
                self.assertIn("source.py", tree.splitlines())
                self.assertNotIn(".env", tree.splitlines())

    def test_without_environment_staging_and_status_have_no_pathspec(self):
        wt = self.leftover()
        (wt / "source.py").write_text("print('work')\n")
        module = holophyte.environment_git
        with patch.object(module, "sh", wraps=module.sh) as stage, patch.object(
                holophyte.claim, "sh", wraps=holophyte.claim.sh) as status:
            self.loop(Idle(), APPROVE)
        self.assertIn(["git", "add", "-A"], [c.args[0] for c in stage.call_args_list])
        self.assertEqual({tuple(c.args[0]) for c in status.call_args_list
                          if c.args[0][:2] == ["git", "status"]},
                         {("git", "status", "--porcelain")})

    def test_an_idle_implementer_on_a_dirty_leftover_does_not_merge_debris(self):
        """A reclaimed WIP candidate still needs independent approval."""
        wt = self.leftover()
        self.environment()
        holophyte.claim.write_worktree_environment(self.tgt, wt)
        (wt / "debris.bin").write_text("build junk\n")

        fake, _ = self.loop(Idle(), REQUEST_CHANGES, Idle())

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())
        self.assertIn("WIP", self.subjects(BRANCH)[0])
        self.assertEqual(self.git("log", BRANCH, "--format=%H", "--", ".env"), "")
        self.assertIn("debris.bin", self.git("ls-tree", "--name-only", BRANCH))

    def test_an_empty_reused_leftover_is_discarded_like_a_fresh_cut(self):
        """An unignored environment alone is still an empty reclaimed checkout."""
        wt = self.leftover()
        self.environment()
        holophyte.claim.write_worktree_environment(self.tgt, wt)
        exclude = wt / _git(wt, "rev-parse", "--git-path", "info/exclude")
        exclude.write_text("")
        self.assertEqual(self.git("status", "--porcelain", cwd=wt).strip(), "?? .env")

        self.loop(Idle())

        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("discarded", reason)

    def test_a_timed_out_implementer_keeps_the_commits_it_made(self):
        """Commits landed before timeout survive, with their location recorded."""
        printed = self.main_output(CommitThenTimeout("late work"))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())
        self.assertIn("late work", self.subjects(BRANCH))
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("budget", reason)
        self.assertIn(BRANCH, reason)
        self.assertEqual(self.last_fake.turns[0].timeout, 5 * 60)
        self.assertIn("partial progress before cap", printed)

    def test_a_timed_out_dirty_tree_is_kept_as_a_wip_commit(self):
        """A timed-out edit survives as WIP and can be reclaimed and reviewed."""
        self.environment()
        self.loop(EditThenTimeout("the whole move done; mid-commit"))
        wt = self.worktrees / "ko-131-add-a-thing"
        self.assertTrue((wt / ".env").is_file())
        self.assertEqual(self.git("log", BRANCH, "--format=%H", "--", ".env"), "")
        self.assertIn("wip-one.txt", self.git("ls-tree", "--name-only", BRANCH))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").is_dir())
        commits = self.git("log", f"main..{BRANCH}", "--format=%s").splitlines()
        self.assertEqual(len(commits), 1)
        self.assertTrue(
            commits[0].startswith("WIP: implementer budget fired"), commits)
        sha = self.git("rev-parse", BRANCH).strip()
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("budget", reason)
        self.assertIn(sha[:12], reason)
        ((event,),) = self.read("SELECT summary FROM runEvents"
                               " WHERE kind = 'wip_committed'")
        self.assertIn(sha[:12], event)
        self.assertIn("2 changed file(s)", event)
        conn = store.open(str(self.db), migrate="owner")
        self.addCleanup(conn.close)
        store.requeue(conn, 1, "budget fired; the WIP commit is the work")
        fake, _ = self.loop(Idle(), APPROVE, provider=StubProvider(a_task()))

        self.assertEqual(fake.roles, ["implement", "review"])
        ((carried,),) = self.read("SELECT summary FROM runEvents"
                                 " WHERE kind = 'carried_candidate'")
        self.assertIn(sha[:12], carried)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("failed",), ("merged",)])
        self.assertIn(commits[0], self.subjects())

    def test_a_turn_killed_mid_staging_still_lands_the_wip_commit(self):
        """Rescue clears a killed git add lock and counts files, not directories."""
        step = StageThenTimeout()
        self.loop(step)

        self.assertTrue(step.flag.exists())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").is_dir())
        commits = self.git("log", f"main..{BRANCH}", "--format=%s").splitlines()
        self.assertEqual(len(commits), 1)
        self.assertTrue(
            commits[0].startswith("WIP: implementer budget fired"), commits)
        sha = self.git("rev-parse", BRANCH).strip()
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("budget", reason)
        self.assertIn(sha[:12], reason)
        ((event,),) = self.read("SELECT summary FROM runEvents"
                               " WHERE kind = 'wip_committed'")
        self.assertIn(sha[:12], event)
        self.assertIn("3 changed file(s)", event)

    def test_a_timed_out_clean_tree_is_still_discarded(self):
        """An environment whose ignore rule disappeared is not timed-out work."""
        self.environment()

        class LoseIgnoreThenTimeout(IdleThenTimeout):
            def play(self, wt, turn):
                exclude = Path(wt) / _git(wt, "rev-parse", "--git-path", "info/exclude")
                exclude.write_text("")
                if _git(wt, "status", "--porcelain") != "?? .env":
                    raise AssertionError("expected only the unignored environment")
                super().play(wt, turn)

        self.loop(LoseIgnoreThenTimeout())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("discarded", reason)

    def test_budget_scale_multiplies_the_cap_every_implementer_turn_gets(self):
        """Scale both implementer caps without changing the ticket estimate."""
        self.configure("[agents]\nbudget_scale = 1.5\n")
        task = dict(a_task(), budget_min=30)

        fake, _ = self.loop(Commit("work"), REQUEST_CHANGES,
                            Commit("the fix"), APPROVE,
                            provider=StubProvider(task))

        self.assertEqual(fake.roles,
                         ["implement", "review", "implement", "review"])
        self.assertEqual(fake.turns[0].timeout, 45 * 60)
        self.assertEqual(fake.turns[2].timeout, 45 * 60)
        # Reports retain the ticket's original 30-minute estimate.
        self.assertEqual(
            self.read("SELECT timeBoxMs FROM runs"), [(30 * 60 * 1000,)])

    def test_without_budget_scale_the_cap_is_the_estimate_as_today(self):
        """No key: the turn is armed with the ticket's estimate, unchanged."""
        task = dict(a_task(), budget_min=30)

        fake, _ = self.loop(Commit("work"), APPROVE,
                            provider=StubProvider(task))

        self.assertEqual(fake.turns[0].timeout, 30 * 60)

    def test_a_scaled_budget_timeout_names_both_figures(self):
        """Report both the estimate and the scaled cap that fired."""
        self.configure("[agents]\nbudget_scale = 1.5\n")
        task = dict(a_task(), budget_min=30)

        printed = self.main_output(CommitThenTimeout("late work"),
                                   provider=StubProvider(task))

        self.assertIn("task exceeded 30 min budget (45 min at scale 1.5)",
                      printed)
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("30 min budget (45 min at scale 1.5)", reason)

    def test_the_refusal_reason_reaches_the_run_row(self):
        """Persist the reuse refusal even if the board is unavailable."""
        wt = self.worktrees / "ko-131-add-a-thing"
        wt.mkdir(parents=True)
        (wt / "precious.txt").write_text("rescued work\n")

        self.loop()

        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("not a registered worktree", reason)
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])

    def test_a_no_commit_run_keeps_the_reused_worktree_and_its_commits(self):
        """KO-146/KO-172: preserve commits even when the tree matches main."""
        wt = self.leftover()
        (wt / "rescued.txt").write_text("rescued work\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)
        self.git("rm", "-q", "rescued.txt", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: and taken back out", cwd=wt)

        fake, _ = self.loop(Idle())

        self.assertEqual(fake.roles, ["implement"])  # no review turn
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())
        self.assertIn("rescued: preserved work", self.subjects(BRANCH))
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("preserved work kept on", reason)

    # --- the reuse merge stopping on conflicts (KO-355) ------------------

    TEST_FILE = "tests/test_thing.py"
    BOTH_TESTS = ("def test_it_works():\n    pass\n\n"
                  "def test_branch_side():\n    pass\n\n"
                  "def test_main_side():\n    pass\n")

    def conflicting_leftover(self):
        """A preserved branch and a main that both append a test at the same
        lines of the same file: the add/add overlap the operator resolved
        three times in one day."""
        wt = self.leftover()
        (wt / self.TEST_FILE).write_text(
            "def test_it_works():\n    pass\n\ndef test_branch_side():\n"
            "    pass\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: the branch's test", cwd=wt)
        (self.target / self.TEST_FILE).write_text(
            "def test_it_works():\n    pass\n\ndef test_main_side():\n"
            "    pass\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "main moved on with its own test")
        return wt

    def test_a_conflicting_reuse_is_handed_to_the_implementer_who_resolves_it(self):
        """The conflict is the implementer's first commit, not a person's
        park: the brief opens by naming the path, and the run reaches its
        first verify with the merge committed -- MERGE_HEAD gone."""
        self.conflicting_leftover()
        resolve = ResolveMerge(self.TEST_FILE, self.BOTH_TESTS)
        review = ApproveNotingMergeHead()

        fake, _ = self.loop(resolve, review)

        self.assertEqual(fake.roles, ["implement", "review"])
        brief = fake.turns[0].goal
        self.assertTrue(brief.startswith("FIRST, before the ticket's work"),
                        brief[:200])
        self.assertIn(self.TEST_FILE, brief)
        self.assertLess(brief.index(self.TEST_FILE),
                        brief.index("Implement this task"))
        self.assertIn(self.TEST_FILE, resolve.conflicted)
        self.assertFalse(review.mid_merge)
        # The resolution and the ticket's work both reached main.
        self.assertEqual((self.target / self.TEST_FILE).read_text(),
                         self.BOTH_TESTS)
        self.assertIn("Merge main into the preserved branch: both tests",
                      self.subjects())
        self.assertIn("rescued: the branch's test", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_an_unresolved_reuse_merge_fails_at_the_first_verify(self):
        """An implementer that commits nothing leaves the tree mid-merge;
        the first verify fails the run naming the unresolved merge, no
        reviewer is asked, and the branch keeps its preserved commit."""
        self.conflicting_leftover()
        moved_main = self.git("rev-parse", "main").strip()

        fake, _ = self.loop(Idle())

        self.assertEqual(fake.roles, ["implement"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("left the merge unresolved", reason)
        self.assertIn(self.TEST_FILE, reason)
        self.assertIn("a human resolves the merge", reason)
        self.assertIn(BRANCH, self.branches())
        self.assertIn("rescued: the branch's test", self.subjects(BRANCH))
        self.assertEqual(self.git("rev-parse", "main").strip(), moved_main)

    def test_a_reuse_that_merges_main_cleanly_carries_no_conflict_paragraph(self):
        """Main moved on in a different file: the merge lands on its own and
        the implementer is briefed on the ticket alone."""
        wt = self.leftover()
        (wt / "work.txt").write_text("preserved\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)
        (self.target / "new.txt").write_text("newer main\n")
        self.git("add", "new.txt")
        self.git("commit", "-q", "-m", "main moved on")

        fake, _ = self.loop(Commit("the scripted work"), APPROVE)

        brief = fake.turns[0].goal
        self.assertTrue(brief.startswith("Implement this task"), brief[:200])
        self.assertNotIn("mid-merge", brief)
        self.assertNotIn("conflict", brief)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_carried_candidate_reaches_review_without_a_new_commit(self):
        """A preserved branch ahead of main is a candidate, not a dead run:
        the implementer that correctly no-ops on finished work used to fail
        the no-commit gate forever, so the only exits were operator surgery
        or destroying the work (holophyte-bugs #3)."""
        wt = self.leftover()
        (wt / "carried.txt").write_text("a complete candidate\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "carried: a complete candidate", cwd=wt)
        carried = self.git("rev-parse", "HEAD", cwd=wt).strip()

        fake, _ = self.loop(Idle(), APPROVE)

        self.assertEqual(fake.roles, ["implement", "review"])
        review = next(t for t in fake.turns if t.role == "review")
        self.assertEqual((review.base_sha, review.candidate_sha),
                         (self.base, carried))
        self.assertIn("carried: a complete candidate", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertTrue(
            [summary for (summary,) in
             self.read("SELECT summary FROM runEvents ORDER BY seq")
             if "candidate carried from a prior run" in summary],
            "no event names the candidate as carried")

    def test_a_fresh_no_commit_run_cleans_up_and_says_discarded(self):
        """The fresh-cut behavior stays: nothing on the branch to keep, so
        it goes — and the reason says so instead of claiming preservation."""
        self.loop(Idle())

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        ((reason,),) = self.read("SELECT outcomeReason FROM runs")
        self.assertIn("discarded", reason)
        self.assertNotIn("preserved", reason)

    def test_preserved_commits_are_reviewed_and_carried_into_the_merge(self):
        """Preserved commits were never approved, so the review base must be
        main — putting them inside the reviewed diff — and the merge must
        carry them into main's history."""
        wt = self.leftover()
        (wt / "rescued.txt").write_text("rescued work\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)

        fake, _ = self.loop(Commit("the scripted work"), APPROVE)

        self.assertIn("rescued: preserved work", self.subjects())
        review = next(t for t in fake.turns if t.role == "review")
        self.assertEqual(review.base_sha, self.base)

    def test_an_unregistered_leftover_directory_fails_the_run_cleanly(self):
        """A leftover directory that is not a registered worktree can be
        neither reused nor safely deleted, so the run fails with nothing
        under the directory touched — before the fix `git worktree add`
        died on the non-empty directory and the RuntimeError escaped
        `main()` as a traceback (KO-146 incident, run 9's sibling)."""
        wt = self.worktrees / "ko-131-add-a-thing"
        wt.mkdir(parents=True)
        (wt / "precious.txt").write_text("rescued work\n")

        provider = StubProvider(a_task())
        self.loop(provider=provider)  # no agent turns: fails before dispatch

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual((wt / "precious.txt").read_text(), "rescued work\n")
        (body,) = [body for _task_id, body in provider.comments
                   if "not a registered worktree" in body]
        self.assertIn(str(wt), body)

    def test_a_leftover_branch_with_no_directory_fails_the_run_cleanly(self):
        """The mirror leftover: a preserved branch whose directory a human
        cleared away. `checkout -b` dies on the existing branch, so before
        the fix the RuntimeError escaped `main()`; deleting the branch
        instead could destroy preserved commits."""
        self.git("branch", BRANCH, "main")

        self.loop()  # no agent turns: the run fails before dispatch

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertIn(BRANCH, self.branches())

    # --- the reused branch is asked of origin first (KO-410) -------------

    def published_leftover(self):
        """A leftover worktree holding a preserved commit, published to a
        bare `origin` a person can then move -- the remote a pull-request
        target shares the branch with. Returns `(worktree, local sha, bare
        path, clone path)`; the person's commits are made in the clone and
        `publish()` moves the bare branch onto them."""
        wt = self.leftover()
        (wt / "work.txt").write_text("preserved\n")
        self.git("add", "-A", cwd=wt)
        self.git("commit", "-q", "-m", "rescued: preserved work", cwd=wt)
        local = self.git("rev-parse", "HEAD", cwd=wt).strip()
        bare = self.worktrees.parent / "origin.git"
        self.git("init", "-q", "--bare", str(bare))
        self.git("fetch", "-q", str(self.target), f"{BRANCH}:{BRANCH}",
                 cwd=bare)
        self.git("remote", "add", "origin", str(bare))
        clone = self.worktrees.parent / "person"
        self.git("clone", "-q", "-b", BRANCH, str(bare), str(clone))
        self.git("config", "user.email", "person@example.invalid", cwd=clone)
        self.git("config", "user.name", "A Person", cwd=clone)
        return wt, local, bare, clone

    def publish(self, clone, bare, force=False):
        """The person's `clone` branch moved onto the bare remote -- by a
        fetch into it, since the fixture's `git push` is the witnessed
        fake (`force` for a rewritten history)."""
        spec = f"{'+' if force else ''}{BRANCH}:{BRANCH}"
        self.git("fetch", "-q", str(clone), spec, cwd=bare)
        return self.git("rev-parse", BRANCH, cwd=bare).strip()

    def head_seen(self):
        """An implementer step that records the sha the turn stood on,
        alongside the list it records into: `(seen, step)`."""
        seen = []
        git = self.git

        class NoteHead(Idle):
            def play(self, cwd, turn):
                seen.append(git("rev-parse", "HEAD", cwd=cwd).strip())
                return "noted the head"

        return seen, NoteHead()

    def test_a_reclaim_fast_forwards_the_preserved_branch_to_origin(self):
        """The remote copy of the preserved branch is a commit ahead -- a
        person pushed on top of the parked work. The reclaim fast-forwards
        the worktree and the local branch to the remote's head before the
        implementer runs, and the ledger note names the commit count."""
        _wt, _local, bare, clone = self.published_leftover()
        (clone / "README.md").write_text("a person's touch\n")
        self.git("commit", "-q", "-am", "person: one commit on top",
                 cwd=clone)
        theirs = self.publish(clone, bare)

        seen, note_head = self.head_seen()
        fake, _ = self.loop(note_head, APPROVE)

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(seen, [theirs])
        review = next(t for t in fake.turns if t.role == "review")
        self.assertEqual(review.candidate_sha, theirs)
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE kind = 'note' AND text"
                      " LIKE 'Fast-forwarded%'"),
            [(f"Fast-forwarded {BRANCH} to {theirs} from origin (1 commit(s)"
              " pushed by someone else)",)])
        self.assertIn("person: one commit on top", self.subjects())
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_a_reclaim_with_an_equal_remote_changes_nothing(self):
        """The remote holds exactly the preserved tip: nothing is
        fast-forwarded, no note is written, and the branch the implementer
        stands on is the local one it left."""
        _wt, local, _bare, _clone = self.published_leftover()

        seen, note_head = self.head_seen()
        fake, _ = self.loop(note_head, APPROVE)

        self.assertEqual(seen, [local])
        review = next(t for t in fake.turns if t.role == "review")
        self.assertEqual(review.candidate_sha, local)
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE text LIKE"
                      " 'Fast-forwarded%'"), [])

    def test_a_reclaim_refuses_a_preserved_branch_diverged_from_origin(self):
        """The remote copy was rewritten rather than built on: neither side
        fast-forwards to the other. The run fails before any implementer
        turn, naming both shas, and the branch stands at its local tip."""
        wt, local, bare, clone = self.published_leftover()
        self.git("reset", "-q", "--hard", "HEAD~1", cwd=clone)
        (clone / "README.md").write_text("rewritten\n")
        self.git("commit", "-q", "-am", "person: a rewrite", cwd=clone)
        theirs = self.publish(clone, bare, force=True)

        fake, _ = self.loop()  # an empty script: any agent turn would raise

        self.assertEqual(fake.roles, [])
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn("diverged", reason)
        self.assertIn(local, reason)
        self.assertIn(theirs, reason)
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), local)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=wt).strip(), local)


class ApproveNotingMergeHead:
    """The approval, recording whether the worktree was still mid-merge when
    the review turn arrived -- the state the first verify must have seen."""

    role = APPROVE.role
    mid_merge = None

    def play(self, cwd, turn):
        self.mid_merge = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=cwd,
            capture_output=True).returncode == 0
        return APPROVE.play(cwd, turn)


class EditThenTimeout(Idle):
    """Write real files, then block until the implementer cap kills the turn."""

    paths = ("wip-one.txt", "wip-two.txt")

    def play(self, cwd, turn):
        for path in self.paths:
            (cwd / path).write_text(f"{path}: mid-edit work\n")
        block_until_killed(cwd, self.reply)


class StageThenTimeout(Idle):
    """An implementer the cap kills while its `git add` holds the index
    lock — the interruption the WIP rescue commits over.

    A clean filter that blocks on a once-only flag keeps a real `git add`
    inside its staging until `run_capped`'s SIGKILL lands, leaving the
    stale `index.lock` the rescue's own `git add -A` must clear; the flag
    then standing, the rescued staging runs the filter straight through.
    The files sit inside `new-dir/`, which default porcelain collapses into
    one `??` line — the count in the event is `-uall`'s.
    """

    def play(self, cwd, turn):
        # The flag and script live beside the worktree, not in it: in the
        # tree they would be staged into the WIP commit they exist to test.
        self.flag = cwd.parent / "filter-ran-once"
        script = cwd.parent / "block-once.sh"
        script.write_text(
            "#!/bin/sh\n"
            f'[ -f "{self.flag}" ] && exec cat\n'
            f'touch "{self.flag}"\n'
            "exec sleep 600\n")
        script.chmod(0o755)
        _git(cwd, "config", "filter.once.clean", str(script))
        (cwd / "new-dir").mkdir()
        (cwd / "new-dir" / ".gitattributes").write_text("*.txt filter=once\n")
        (cwd / "new-dir" / "one.txt").write_text("one: mid-edit work\n")
        (cwd / "new-dir" / "two.txt").write_text("two: mid-edit work\n")
        holophyte.gates.run_capped(
            ["sh", "-c", 'printf %s "$1"; git add -A; sleep 600',
             "sh", self.reply], cwd, timeout=2)


class MainDiverges:
    """A review turn that also lands a commit on main behind the branch.

    The one way a merge conflict happens for real: main moves while the run
    is under review, so the `--no-ff` merge at the end of the run meets a
    changed file. The step answers the review turn as usual after committing.
    """

    role = REVIEW_ROLES

    def __init__(self, commit, text=APPROVE.text):
        self.commit = commit
        self.text = text

    def play(self, cwd, turn):
        self.commit()
        return self.text


class MergeConflictTests(LoopFixture):
    """The merge gate meeting a conflict: a `main` that conflicts with the
    branch, on any path, goes to the implementer first (KO-404); a merge
    it leaves unresolved is aborted, main left clean and the run parked
    with the paths named. The `Idle()` step is that turn declining to
    resolve."""

    def commit_on_main(self, path, body):
        """Land `body` at `path` on main — the divergence the merge meets."""
        file = self.target / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"main moves {path}")

    def main_status(self):
        """Main's tracked state: empty means no half-applied merge left.

        Untracked files are excluded because a failed run deliberately leaves
        its regenerated FINDINGS.md window uncommitted for a human, which is
        not merge residue.
        """
        return self.git("status", "--porcelain", "-uno").strip()

    def mid_merge(self):
        """Whether main is still sitting in a merge git never finished."""
        return (self.target / ".git" / "MERGE_HEAD").exists()

    def test_a_conflict_outside_findings_aborts_and_leaves_main_clean(self):
        self.loop(Commit("branch edit", path="README.md", body="branch side\n"),
                  MainDiverges(lambda: self.commit_on_main("README.md",
                                                           "main side\n")),
                  Idle())

        self.assertEqual(self.main_status(), "")
        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / "README.md").read_text(), "main side\n")
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn("README.md", reason)
        self.assertIn(BRANCH, self.branches())  # preserved for a human

    def test_a_conflict_with_main_parks_the_ticket_and_moves_nothing(self):
        """KO-342: the gate merges main into the branch first, and a
        conflict the implementer leaves unresolved is a person's: the
        ticket is blocked with the path in its question, the branch sits
        at its pre-gate sha, main is where the divergence left it."""
        seen = {}

        def diverge():
            self.commit_on_main("README.md", "main side\n")
            seen["main"] = self.git("rev-parse", "main").strip()
            seen["branch"] = self.git("rev-parse", BRANCH).strip()

        self.loop(Commit("branch edit", path="README.md", body="branch side\n"),
                  MainDiverges(diverge), Idle())

        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        self.assertIn("README.md", question)
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), seen["branch"])
        self.assertEqual(self.git("rev-parse", "main").strip(), seen["main"])
        self.assertEqual(self.main_status(), "")

    def test_a_conflict_park_after_a_strike_is_reported_as_the_conflict(self):
        """KO-345 review: one failed run, then a run the gate parks on a
        merge conflict -- a second counted failure. The next pass names the
        conflict, which is what the operator must resolve, not a strike-out
        that would send them looking for a failure of the work."""
        self.loop(Commit("first cut"), REQUEST_CHANGES,
                  Commit("first fix round 1"), REQUEST_CHANGES,
                  Commit("first fix round 2"), FAIL)
        conn = store.open(str(self.db), migrate="owner")
        self.addCleanup(conn.close)
        tickets.walk_ticket(conn, 1, "ready")
        self.loop(Commit("branch edit", path="README.md", body="branch side\n"),
                  MainDiverges(lambda: self.commit_on_main("README.md",
                                                           "main side\n")),
                  Idle())
        self.assertEqual(
            self.read("SELECT outcome FROM runs ORDER BY id"),
            [("failed",), ("failed",)])
        parked, other = a_task(), dict(a_task(2), title="add another thing")

        out = self.main_output(Commit("the other work"), APPROVE,
                               provider=StubProvider(parked, other))

        self.assertIn("[holo2] KO-131 is parked on a merge-gate conflict;"
                      " resolve the branch and --requeue KO-131;", out)
        self.assertNotIn("struck out", out)
        self.assertIn("the other work", self.subjects())
        self.assertEqual(
            self.read("SELECT linearIdentifier, status FROM tickets"
                      " ORDER BY id"),
            [("KO-131", "blocked_on_operator"), ("KO-132", "merged")])

    def test_a_main_that_moved_without_conflict_is_merged_in_and_re_verified(self):
        """KO-342: main gains an unrelated commit under review; the gate
        merges it into the branch, runs the verify once more on the result,
        and the `--no-ff` merge lands with that commit behind it."""
        log = self.target.parent / "verify.log"
        task = a_task()
        task["verify"] = f"echo ran >> {log} && echo ok"
        moved = {}

        def diverge():
            self.commit_on_main("other.txt", "elsewhere\n")
            moved["sha"] = self.git("rev-parse", "main").strip()

        self.loop(Commit("branch edit", path="README.md", body="branch side\n"),
                  MainDiverges(diverge), provider=StubProvider(task))

        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        # one verify before review round 1, one more at the gate
        self.assertEqual(log.read_text().splitlines(), ["ran", "ran"])
        # The --no-ff merge commit, wherever the close-out's FINDINGS commit
        # has since put main's HEAD.
        merge = self.git("log", "main", "--merges", "-1", "--format=%H").strip()
        self.assertTrue(merge, self.subjects())
        branch_tip = self.git("rev-parse", f"{merge}^2").strip()
        # The branch side of the --no-ff merge already contains main's move:
        # the gate merged it in (`git` exits nonzero, and the fixture raises,
        # when it is not an ancestor).
        self.git("merge-base", "--is-ancestor", moved["sha"], branch_tip)
        self.assertNotEqual(branch_tip, moved["sha"])
        self.assertIn("main moves other.txt", self.subjects(merge))
        self.assertEqual((self.target / "README.md").read_text(), "branch side\n")
        self.assertEqual((self.target / "other.txt").read_text(), "elsewhere\n")
        self.assertNotIn(BRANCH, self.branches())

    def test_a_conflicting_path_that_merely_contains_findings_md_is_not_resolved(self):
        """The unmerged set decides, not a substring of the merge's output: a
        conflict in `docs/FINDINGS.md-notes.md` names FINDINGS.md in every
        line git prints about it, and is still a non-FINDINGS conflict."""
        path = "docs/FINDINGS.md-notes.md"
        self.commit_on_main(path, "base\n")
        self.loop(Commit("branch edit", path=path, body="branch side\n"),
                  MainDiverges(lambda: self.commit_on_main(path, "main side\n")),
                  Idle())

        self.assertEqual(self.main_status(), "")
        self.assertFalse(self.mid_merge())
        self.assertEqual((self.target / path).read_text(), "main side\n")
        ((outcome, reason),) = self.read(
            "SELECT outcome, outcomeReason FROM runs")
        self.assertEqual(outcome, "failed")
        self.assertIn(path, reason)

    def test_a_conflict_only_in_findings_md_parks_like_any_other(self):
        """KO-342: the `--no-ff` merge used to take the branch side of a
        FINDINGS.md-only conflict, but the gate's merge of main into the
        branch grants no such exception -- a conflict the implementer
        leaves unresolved parks, with the path named, and moves nothing."""
        self.configure('[report]\nfindings = "repo"\n')
        seen = {}

        def diverge():
            self.commit_on_main("FINDINGS.md", "main window\n")
            seen["main"] = self.git("rev-parse", "main").strip()
            seen["branch"] = self.git("rev-parse", BRANCH).strip()

        self.loop(Commit("branch window", path="FINDINGS.md",
                         body="branch window\n"),
                  MainDiverges(diverge), Idle())

        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        self.assertIn("FINDINGS.md", question)
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), seen["branch"])
        self.assertEqual(self.git("rev-parse", "main").strip(), seen["main"])
        self.assertFalse(self.mid_merge())
        # The only dirt on main is the close-out's regenerated FINDINGS.md
        # window, which every failed run leaves for a human -- here over a
        # tracked file, so it shows as modified rather than untracked.
        self.assertEqual(self.main_status(), "M FINDINGS.md")
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("failed",)])
        self.assertEqual(self.git("show", "main:FINDINGS.md"), "main window\n")


class BoardLeaseLabelTests(LoopFixture):
    """The claim's second lease (KO-351): a `holo:HOST` label on the
    issue, which a writer with a store of its own can see where it cannot
    see this store's `activeRunId`."""

    HOST = "writer-1"

    def setUp(self):
        super().setUp()
        self.configure('[report]\nhost_label = "writer-1"\n')

    @staticmethod
    def label(host="writer-1"):
        return f"holo:{host}"

    def labelled(self, provider, labels, *more):
        task = a_task()
        task["labels"] = labels
        return provider(task, *more)

    def seed_ended_run(self, requeue=True):
        """A run of this store on KO-131 that ended `failed` -- with the
        board down, so its label is still on the issue -- and, unless told
        otherwise, the requeue that put the ticket back. Returns the run
        id."""
        conn = store.open(str(self.db), migrate="owner")
        try:
            project = tickets.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            ticket = holophyte.board.mirror_task(conn, project, a_task())
            run_id = store.claim(conn, project, ticket)
            tickets.transition(conn, ticket, "in_flight")
            store.release(conn, run_id, "failed", "the board was down")
            if requeue:
                store.requeue(conn, ticket, "board back")
            conn.commit()
        finally:
            conn.close()
        return run_id

    def test_a_claim_labels_before_the_implementer_and_a_merge_unlabels(self):
        """The label `holo:writer-1` is on the issue when the implementer's
        turn begins -- not after it, when a second writer's listing could
        already have offered the ticket -- and the merge's close-out takes
        it off."""
        provider = StubProvider(a_task())
        at_implement = []

        @dataclass
        class Witness(Commit):
            def play(self, cwd, turn):
                at_implement.append(list(provider.labels["iss-131"]))
                return super().play(cwd, turn)

        self.loop(Witness("the scripted work"), APPROVE, provider=provider)

        self.assertEqual(self.read("SELECT id, outcome FROM runs"),
                         [(1, "merged")])
        self.assertEqual(at_implement, [["holo:writer-1"]])
        self.assertEqual(provider.labels["iss-131"], [])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        # One read-back, between the add and the implementer.
        self.assertEqual(provider.read_calls, ["iss-131"])

    def test_a_ticket_another_writer_labelled_is_skipped_and_nothing_is_leased(self):
        provider = self.labelled(StubProvider, [self.label("writer-2")])

        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("[holo2] KO-131 is leased by writer-2 on the board;"
                      " skipping it", out)
        self.assertEqual(self.read("SELECT id FROM runs"), [])
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(provider.label_calls, [])
        self.assertEqual(provider.read_calls, [])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-2"])

    def test_this_writers_labels_with_no_live_run_are_stale_and_removed(self):
        """Run 1 ended with the board down and its label stayed. It is no
        lease: the store has no live run under it, so the claim goes ahead
        -- the stale label off under the store lease, then the fresh one
        on, then off again at the merge."""
        self.seed_ended_run()
        provider = self.labelled(StubProvider, [self.label()])

        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)

        self.assertIn("carries this writer's lease label holo:writer-1"
                      " with no live run; removing the stale label and"
                      " claiming", out)
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "failed"), (2, "merged")])
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], [])
        self.assertEqual(self.last_fake.roles, ["implement", "review"])

    def test_a_stale_label_seen_by_two_loops_is_touched_only_by_the_claim_holder(self):
        """Two loops on one store admit the same stale label. The one whose
        `store.claim()` lands takes the stale one off and writes its own;
        the other reaches its claim a moment later -- here, at the instant
        the first is writing its label -- waits its turn, and is refused
        by the store before it has touched the board. The loser's
        `_claim_run()` is the real one, on a thread of its own as a
        sibling loop's would be: the witness is that the provider saw no
        call from it at all."""
        self.seed_ended_run()
        db, target, tgt = self.db, self.target, self.tgt
        seen = type("Seen", (), {"trips": (), "watched": ()})()
        competitor = []
        threads = []

        def compete(provider, issue_id):
            conn = store.open(str(db), migrate="owner")
            try:
                project = tickets.ensure_project(conn, StubProvider.TEAM,
                                               str(target))
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIssueId = ?",
                    (issue_id,)).fetchone()
                competitor.append(holophyte.claim._claim_run(
                    tgt, conn, project, provider, a_task(), ticket_id, seen))
            finally:
                conn.close()

        class Contended(StubProvider):
            def label_issue(self, issue_id, name):
                if not threads:
                    thread = threading.Thread(target=compete,
                                              args=(self, issue_id))
                    threads.append(thread)
                    thread.start()
                    # The competitor is at its claim and stays there: the
                    # turn is this claim's until its label is written.
                    thread.join(0.5)
                    self.assertion = (thread.is_alive(), list(competitor))
                super().label_issue(issue_id, name)

        provider = self.labelled(Contended, [self.label()])
        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)
        threads[0].join(10)

        self.assertEqual(provider.assertion, (True, []))
        self.assertEqual(competitor, [holophyte.claim.HELD])
        self.assertIn("lease already held by run 2; skipping it", out)
        self.assertEqual(self.read("SELECT id, outcome FROM runs ORDER BY id"),
                         [(1, "failed"), (2, "merged")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        # Every board call is the winner's; the loser made none.
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], [])

    def test_a_late_close_out_leaves_the_label_a_fresh_claim_re_asserted(self):
        """The label names the writer, not the run, so the store decides
        whether a close-out may take it off: run 1's release, arriving
        after run 2 has claimed the same ticket and re-asserted the label,
        finds the store naming run 2 as the live one and leaves the label
        on; run 2's own release takes it off."""
        ended = self.seed_ended_run()
        conn = store.open(str(self.db), migrate="owner")
        try:
            project = tickets.ensure_project(conn, StubProvider.TEAM,
                                           str(self.target))
            (ticket_id,) = conn.execute("SELECT id FROM tickets").fetchone()
            live = store.claim(conn, project, ticket_id)
            conn.commit()
            provider = StubProvider(a_task())
            provider.label_issue("iss-131", "holo:writer-1")

            holophyte.board.release_lease_label(self.tgt, conn, ticket_id,
                                                provider, ended)
            self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])

            holophyte.board.release_lease_label(self.tgt, conn, ticket_id,
                                                provider, live)
            self.assertEqual(provider.labels["iss-131"], [])
        finally:
            conn.close()

    def test_a_close_out_racing_a_fresh_claim_cannot_strip_the_fresh_label(self):
        """Run 1's close-out looks at the store, finds no live run, and
        goes to the board -- and at that instant a sibling loop on this
        store reaches its claim of the same ticket. Were the claim to land
        in the gap, the removal would take the fresh run's label off and
        another writer could claim a ticket this store is working (review
        finding P1 on KO-351). The claim waits the close-out's turn
        instead: while the removal is in flight the store still names no
        live run, and the fresh claim's label is written after the removal
        and stays on the board."""
        ended = self.seed_ended_run()
        db, target, tgt = self.db, self.target, self.tgt
        seen = type("Seen", (), {"trips": (), "watched": ()})()
        claimed = []
        threads = []
        during = []

        def claim(provider, issue_id):
            conn = store.open(str(db), migrate="owner")
            try:
                project = tickets.ensure_project(conn, StubProvider.TEAM,
                                               str(target))
                (ticket_id,) = conn.execute(
                    "SELECT id FROM tickets WHERE linearIssueId = ?",
                    (issue_id,)).fetchone()
                claimed.append(holophyte.claim._claim_run(
                    tgt, conn, project, provider, a_task(), ticket_id, seen))
            finally:
                conn.close()

        class Racing(StubProvider):
            def unlabel_issue(self, issue_id, name):
                if not threads:
                    thread = threading.Thread(target=claim,
                                              args=(self, issue_id))
                    threads.append(thread)
                    thread.start()
                    thread.join(0.5)
                    peek = sqlite3.connect(db)
                    try:
                        during.append(peek.execute(
                            "SELECT activeRunId FROM tickets").fetchall())
                    finally:
                        peek.close()
                super().unlabel_issue(issue_id, name)

        provider = Racing(a_task())
        provider.label_issue("iss-131", "holo:writer-1")
        conn = store.open(str(self.db), migrate="owner")
        try:
            (ticket_id,) = conn.execute("SELECT id FROM tickets").fetchone()
            holophyte.board.release_lease_label(self.tgt, conn, ticket_id,
                                                provider, ended)
        finally:
            conn.close()
        threads[0].join(10)

        # No live run while the removal was in flight: the claim waited.
        self.assertEqual(during, [[(None,)]])
        self.assertEqual(claimed, [2])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(2,)])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])

    def test_a_foreign_label_on_read_back_backs_off_removing_only_our_own_label(self):
        """The admission check reads the listing; the read-back reads the
        issue as it is. A `holo:writer-2` that landed in between is
        writer-2's lease: this writer's own label comes off and nothing
        else on the issue does, the store lease goes back without a
        strike, no run starts, and the loop moves on to the next ticket."""
        class Raced(StubProvider):
            def label_issue(self, issue_id, name):
                # writer-2 labels KO-131 after this writer's listing and
                # before its write; the read-back has it.
                if issue_id == "iss-131":
                    self.labels[issue_id].append("holo:writer-2")
                super().label_issue(issue_id, name)

        provider = self.labelled(Raced, ["other"], a_task(2))
        out = self.main_output(Commit("the scripted work"), APPROVE,
                               provider=provider)

        self.assertIn("[holo2] KO-131 is leased by writer-2 on the board;"
                      " skipping it", out)
        self.assertEqual(provider.labels["iss-131"], ["other", "holo:writer-2"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1"),
                          ("label", "iss-132", "holo:writer-1"),
                          ("unlabel", "iss-132", "holo:writer-1")])
        self.assertEqual(
            self.read("SELECT t.linearIdentifier, r.outcome, r.outcomeClass"
                      " FROM runs r JOIN tickets t ON t.id = r.ticketId"
                      " ORDER BY r.id"),
            [("KO-131", "failed", "infra"), ("KO-132", "merged", "work")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"),
                         [(None,), (None,)])
        # Only KO-132 was worked: one implement and one review turn.
        self.assertEqual(self.last_fake.roles, ["implement", "review"])
        self.assertEqual(provider.labels["iss-132"], [])

    def test_a_label_the_board_refuses_releases_the_lease_and_starts_no_run(self):
        """The add itself raised. The store lease goes back and no run
        starts; the removal that follows is best-effort, since a raise is
        not proof that nothing landed (review finding P2 on KO-351)."""
        class Refusing(StubProvider):
            def label_issue(self, issue_id, name):
                self.label_calls.append(("label", issue_id, name))
                raise RuntimeError("linear is down")

        provider = Refusing(a_task())
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.read_calls, [])
        self.assertEqual(self.branches(), ["main"])

    def test_an_add_that_landed_before_it_raised_is_taken_off_again(self):
        """The mutation applied and the response failed -- a timeout after
        the write. The claim is refused as before, and the label the board
        does hold comes off, so a refused claim does not leave a lease
        every other writer will honour forever."""
        class Landed(StubProvider):
            def label_issue(self, issue_id, name):
                super().label_issue(issue_id, name)
                raise RuntimeError("linear timed out after the write")

        provider = self.labelled(Landed, ["other"])
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.labels["iss-131"], ["other"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])

    def test_a_read_back_the_board_refuses_ends_with_our_label_off_and_no_lease(self):
        """The add landed and the read-back raised. The store lease goes
        back as before; this writer's label goes with it -- and only that
        one: the issue's other labels are not touched."""
        class HalfTaken(StubProvider):
            def issue_labels(self, issue_id):
                self.read_calls.append(issue_id)
                raise RuntimeError("linear timed out on the read-back")

        provider = self.labelled(HalfTaken, ["other"])
        out = self.main_output(Commit("never reached"), APPROVE,
                               provider=provider)

        self.assertIn("did not take the lease label holo:writer-1", out)
        self.assertEqual(self.last_fake.turns, [])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(provider.labels["iss-131"], ["other"])
        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])

    def test_a_read_back_failure_tries_the_removal_once_and_still_releases(self):
        """The board is down for the read-back and for the removal that
        follows: one removal attempt, not a retry loop, and the store
        lease still goes back -- the label left behind is this writer's
        next claim's stale one."""
        class Down(StubProvider):
            def issue_labels(self, issue_id):
                raise RuntimeError("linear timed out on the read-back")

            def unlabel_issue(self, issue_id, name):
                self.label_calls.append(("unlabel", issue_id, name))
                raise RuntimeError("linear is down")

        provider = Down(a_task())
        self.main_output(Commit("never reached"), APPROVE, provider=provider)

        self.assertEqual(provider.label_calls,
                         [("label", "iss-131", "holo:writer-1"),
                          ("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["holo:writer-1"])
        self.assertEqual(self.read("SELECT outcome, outcomeClass FROM runs"),
                         [("failed", "infra")])
        self.assertEqual(self.read("SELECT activeRunId FROM tickets"), [(None,)])
        self.assertEqual(
            self.read("SELECT COUNT(*) FROM runEvents WHERE kind = 'warning'"
                      " AND summary LIKE '%could not be removed%'"), [(1,)])

    def test_a_requeue_takes_off_the_label_before_the_ticket_is_claimable(self):
        """`--requeue KO-131` removes this writer's label -- run 1's, the
        one whose close-out the board missed -- while the ticket is still
        `in_flight` and no loop can claim it, so a fresh claim's label can
        never be the one it takes off; nothing else on the issue moves."""
        ended = self.seed_ended_run(requeue=False)
        db = self.db
        status_at_removal = []

        class Watched(StubProvider):
            def unlabel_issue(self, issue_id, name):
                conn = store.open(str(db), migrate="owner")
                try:
                    status_at_removal.append(conn.execute(
                        "SELECT status FROM tickets WHERE linearIssueId = ?",
                        (issue_id,)).fetchone()[0])
                finally:
                    conn.close()
                super().unlabel_issue(issue_id, name)

        provider = self.labelled(Watched, ["other", self.label()])
        out = io.StringIO()
        holophyte.operator.requeue(self.tgt, "KO-131", "board back", out,
                               provider=provider)

        self.assertEqual(out.getvalue().strip(),
                         f"[holo2] KO-131 requeued after run {ended}")
        self.assertEqual(status_at_removal, ["in_flight"])
        self.assertEqual(self.read("SELECT status FROM tickets"), [("ready",)])
        self.assertEqual(provider.label_calls,
                         [("unlabel", "iss-131", "holo:writer-1")])
        self.assertEqual(provider.labels["iss-131"], ["other"])


class FactoryCommitIdentityTests(unittest.TestCase):
    """The commits the factory makes itself on worktree reuse carry the
    target's configured identity, and the pinned factory one only when none
    is configured (KO-656): a deploy platform that checks authors refused
    a merge authored `holophyte@factory.invalid` as a pull request's head.
    Real git, with the global and system config shut out."""

    PINNED = "holophyte holophyte@factory.invalid"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": str(root / "home"),
                                      "GIT_CONFIG_GLOBAL": os.devnull,
                                      "GIT_CONFIG_NOSYSTEM": "1"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                     "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
            os.environ.pop(name, None)
        self.repo = root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("base\n")
        self.setup_commit("base")
        self.tgt = holophyte.project.Project(
            path=self.repo, holo_dir=root, store_path=root / "store.db",
            config_path=root / "config.toml", worktrees=root / "wts")
        self.branch = "task/ko-656"
        self.wt = root / "wts" / "ko-656"
        self.git("worktree", "add", "-q", "-b", self.branch, str(self.wt),
                 "main")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.repo),
                              check=True, capture_output=True,
                              text=True).stdout.strip()

    def setup_commit(self, message, cwd=None):
        """The fixture's own commits name their author inline, so they set
        no identity the factory's commits could pick up."""
        self.git("add", "-A", cwd=cwd)
        self.git("-c", "user.name=Setup", "-c", "user.email=setup@example.com",
                 "commit", "-q", "-m", message, cwd=cwd)

    def identities(self, rev):
        return self.git("log", "-1", "--format=%an %ae%n%cn %ce", rev,
                        cwd=self.wt).splitlines()

    def reuse_after_main_moved(self):
        (self.wt / "work.txt").write_text("preserved\n")
        self.setup_commit("preserved work", cwd=self.wt)
        (self.repo / "new.txt").write_text("newer main\n")
        self.setup_commit("main moved on")
        ok, why = holophyte.claim.reuse_leftover(self.tgt, self.wt,
                                                 self.branch)
        self.assertTrue(ok, why)
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "HEAD",
                                  cwd=self.wt).count(" "), 2)  # a merge

    def test_the_reuse_merge_carries_the_configured_identity(self):
        self.git("config", "user.name", "Operator")
        self.git("config", "user.email", "operator@example.com")

        self.reuse_after_main_moved()

        self.assertEqual(self.identities("HEAD"),
                         ["Operator operator@example.com"] * 2)

    def test_the_reuse_merge_falls_back_to_the_pinned_identity(self):
        self.reuse_after_main_moved()

        self.assertEqual(self.identities("HEAD"), [self.PINNED] * 2)

    def test_the_wip_rescue_carries_the_configured_identity(self):
        self.git("config", "user.name", "Operator")
        self.git("config", "user.email", "operator@example.com")
        (self.wt / "dirty.txt").write_text("uncommitted\n")

        ok, why = holophyte.claim.reuse_leftover(self.tgt, self.wt,
                                                 self.branch)

        self.assertTrue(ok, why)
        self.assertIn("WIP: uncommitted leftovers preserved on reuse",
                      self.git("log", "-1", "--format=%s", cwd=self.wt))
        self.assertEqual(self.identities("HEAD"),
                         ["Operator operator@example.com"] * 2)

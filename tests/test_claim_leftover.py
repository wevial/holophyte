"""A claim over a leftover worktree: resumed, reused, kept or refused."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
sys.path.insert(0, str(HERE))
from babysit_fixture import ResolveMerge  # noqa: E402
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    REQUEST_CHANGES,
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
    StubProvider,
    a_task,
)

import holophyte.environment_git  # noqa: E402
import holophyte.loop.claim  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above


class LeftoverWorktreeTests(LoopFixture):
    def test_pause_after_review_resumes_findings_without_repeating_review(self):
        from pause_fixture import PauseReply

        from holophyte.loop.stop import command
        self.loop(Commit(), PauseReply(self.db, REQUEST_CHANGES))
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "addressing")])
        command(self.project, "KO-131", None, resume=True)
        fake, _ = self.loop(Commit("address recorded findings"), APPROVE)
        self.assertIn("scripted change is incomplete", fake.turns[0].goal)
        self.assertEqual(self.read("SELECT outcome FROM runs ORDER BY id"),
                         [("paused",), ("merged",)])

    def test_pause_after_approval_preserves_human_merge_gate(self):
        from pause_fixture import PauseReply

        from holophyte.loop.stop import command
        self.configure('[merge]\napprove = "human"\n')
        self.loop(Commit(), PauseReply(self.db, APPROVE))
        self.assertEqual(self.read("SELECT outcome, resumePhase FROM runs"),
                         [("paused", "merge_gate")])
        command(self.project, "KO-131", None, resume=True)
        self.loop()  # No new agent turn, and no merge approval granted by resume.
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        self.assertEqual(self.read("SELECT phase FROM runs ORDER BY id"),
                         [("paused",), ("awaiting_merge_approval",)])

    def test_resumed_pause_reuses_worktree_without_implementing(self):
        from pause_fixture import PauseEdit

        from holophyte.loop.stop import command
        self.loop(PauseEdit(self.db))
        wt = self.worktrees / "ko-131-add-a-thing"
        original = wt.stat().st_ino
        command(self.project, "KO-131", None, resume=True)
        # An APPROVE-only script fails if another implement turn is dispatched.
        cut = holophyte.loop.claim._cut_worktree
        def reuse(*args, **kwargs):
            self.assertEqual(wt.stat().st_ino, original)
            return cut(*args, **kwargs)
        with patch.object(holophyte.loop.pipeline, "_cut_worktree", side_effect=reuse):
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
                holophyte.loop.claim.write_worktree_environment(self.project, wt)
                self.assertTrue((wt / ".env").is_file())
                rule = ".gitignore" if own_ignore else "info/exclude"
                self.assertIn(rule, self.git("check-ignore", "-v", ".env", cwd=wt))
                (wt / "source.py").write_text("print('work')\n")
                if forced:
                    self.git("add", "-f", ".env", cwd=wt)
                    self.assertIn(".env", self.git("ls-files", cwd=wt).splitlines())
                holophyte.environment_git.stage_work(self.project, wt)
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
                holophyte.loop.claim, "sh", wraps=holophyte.loop.claim.sh) as status:
            self.loop(Idle(), APPROVE)
        self.assertIn(["git", "add", "-A"], [c.args[0] for c in stage.call_args_list])
        self.assertEqual({tuple(c.args[0]) for c in status.call_args_list
                          if c.args[0][:2] == ["git", "status"]},
                         {("git", "status", "--porcelain")})

    def test_an_idle_implementer_on_a_dirty_leftover_does_not_merge_debris(self):
        """A reclaimed WIP candidate still needs independent approval."""
        wt = self.leftover()
        self.environment()
        holophyte.loop.claim.write_worktree_environment(self.project, wt)
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
        holophyte.loop.claim.write_worktree_environment(self.project, wt)
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
        conn = store.open(str(self.db))
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
        holophyte.loop.gates.run_capped(
            ["sh", "-c", 'printf %s "$1"; git add -A; sleep 600',
             "sh", self.reply], cwd, timeout=2)

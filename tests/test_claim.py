from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# Both discovery and named-module unittest commands need the harness on sys.path.
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    FAIL,
    REQUEST_CHANGES,
    REVIEW_ROLES,
    Commit,
    Idle,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    LoopFixture,
    MergeModeFixture,
    StubProvider,
    a_task,
)
from worktree_setup_cases import WorktreeSetupCases  # noqa: E402

import holophyte.board.projection  # noqa: E402 - after the sys.path insert above
import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import holophyte.config.project  # noqa: E402 - after the sys.path insert above
import holophyte.loop.claim  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import store  # noqa: E402 - after the sys.path insert above
import store.tickets as tickets  # noqa: E402 - after the sys.path insert above


class BabysitClaimTests(MergeModeFixture):
    def test_send_back_claim_resumes_the_parked_candidate(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("candidate"), APPROVE, Idle(""), provider=self.provider())
        candidate = self.git("rev-parse", BRANCH).strip()
        self.assertIn("PR open:", self.question())
        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", "sent back to the babysitter", out=io.StringIO())
        self.assertEqual(self.read("SELECT blockedQuestion FROM tickets"), [(None,)])
        observed = []
        set_phase = holophyte.pr.pullrequest.set_phase

        def observe_resume(conn, run_id, phase, note):
            observed.append(self.read(
                "SELECT prUrl, candidateSha, phase, endedAt FROM runs"
                f" WHERE id = {run_id}"))
            self.assertTrue(self.read(
                "SELECT summary FROM runEvents"
                f" WHERE runId = {run_id} AND summary LIKE 'resuming run %'"))
            return set_phase(conn, run_id, phase, note)

        with patch.object(holophyte.pr.pullrequest, "set_phase", observe_resume):
            output = self.main_output(provider=self.provider())
        self.assertEqual(observed, [[(self.URL, candidate, "claimed", None)]])
        self.assertNotIn("parked on PR", output)
        self.assertEqual(self.read("SELECT id, candidateSha FROM runs ORDER BY id"),
                         [(1, candidate), (2, candidate)])
        self.assertEqual([k for k, _ in self.api_calls()], ["state", "state"])

    def test_fresh_claim_has_no_pull_request_or_candidate(self):
        observed = []
        cut = holophyte.loop.pipeline._cut_worktree

        def observe_claim(target, conn, run_id, *args):
            observed.append(self.read(
                "SELECT prUrl, candidateSha, phase FROM runs"
                f" WHERE id = {run_id}"))
            return cut(target, conn, run_id, *args)

        with patch.object(holophyte.loop.pipeline, "_cut_worktree", observe_claim):
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
        conn = store.open(str(self.db))
        try:
            store.record_event(conn, 1, "diagnostic", "sentinel-public-value",
                               level="detail", payload="sentinel quoted value")
            store.record_ledger(conn, 1, "failure", "sentinel-db-value")
            store.record_review_round(
                conn, 1, 1, "pass", "reviewer",
                verification_results=[{"output": "sentinel-auth-value"}])
            output = holophyte.loop.gates.VerificationOutput(
                "sentinel-other-value",
                [{"source": "baseline", "output": "sentinel-other-value"}])
            holophyte.loop.gates.record_unreviewed_verification(conn, 1, output)
            store.resume(conn, 1)
            store.release(conn, 1, "failed", "sentinel-db-value")
            ticket_id = conn.execute("SELECT id FROM tickets").fetchone()[0]
            self.assertTrue(holophyte.board.projection.block_ticket(
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
        self.assertTrue(holophyte.loop.claim.run_worktree_setup(self.project, wt)[0])
        self.assertFalse((wt / ".env").is_symlink())
        self.assertEqual((wt / ".env").stat().st_mode & 0o777, 0o600)
        (wt / ".env").write_text("checkout changes\n")
        self.assertEqual(source.read_text(), "PUBLIC=sentinel-link-value\n")

    def test_local_capture_spec_stays_on_disk_and_out_of_the_commit(self):
        wt = self.target.parent / "capture-local"
        self.git("worktree", "add", "-b", "capture-local", str(wt), "main")
        self.configure('[merge]\nui_capture_dir = ".holophyte-capture"\n'
                       "ui_capture_local = true\n")
        self.assertTrue(holophyte.loop.claim.run_worktree_setup(self.project, wt)[0])
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
                ok, report = holophyte.loop.claim.run_worktree_setup(self.project, wt)
                self.assertFalse(ok)
                self.assertIn("symlink", report)
                self.assertEqual(sorted(p.name for p in outside.iterdir()),
                                 [".gitignore"])
                self.assertEqual((outside / ".gitignore").read_text(), "kept\n")

    def test_capture_directory_untouched_without_local_key(self):
        wt = self.target.parent / "capture-kept"
        self.git("worktree", "add", "--detach", str(wt), "main")
        self.configure('[merge]\nui_capture_dir = ".holophyte-capture"\n')
        self.assertTrue(holophyte.loop.claim.run_worktree_setup(self.project, wt)[0])
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
        struck = holophyte.loop.claim.skip_line("KO-131", 2, None, None)
        self.assertIn("2 failures", struck)
        self.assertIn("a human owns it now", struck)

        url = "https://github.com/example/repo/pull/7"
        parked = holophyte.loop.claim.skip_line("KO-131", 0, url,
                                          f"PR open: {url}\nready to merge")
        self.assertIn(url, parked)
        self.assertIn("--approve KO-131", parked)
        self.assertNotIn("fail", parked)

        asked = holophyte.loop.claim.skip_line(
            "KO-131", 0, None, "merge?\nthe branch is at abc123")
        self.assertIn("a question: merge?;", asked)
        self.assertNotIn("abc123", asked)
        self.assertNotIn("fail", asked)

        closed = holophyte.loop.claim.skip_line(
            "KO-131", 0, url, f"rejected: {url}", "pull_request_closed")
        self.assertIn(f"a question: rejected: {url};", closed)
        self.assertNotIn("--approve", closed)

    def test_a_module_question_outranks_the_strike_count(self):
        """The run that parked the ticket on a merge conflict may also be
        the failure that reached the threshold. The conflict is what the
        operator has to resolve, so it is the line -- and since KO-365 the
        line names the way back, `--requeue`; the escalation's own
        question is the one park the count speaks for."""
        conflicted = holophyte.loop.claim.skip_line(
            "KO-131", 2, None,
            "merge conflict with main on: README.md; resolve it on the branch")
        self.assertIn("parked on a merge-gate conflict; resolve the branch"
                      " and --requeue KO-131", conflicted)
        self.assertNotIn("struck out", conflicted)
        self.assertNotIn("a question", conflicted)

        struck = holophyte.loop.claim.skip_line(
            "KO-131", 2, None, holophyte.board.projection.strike_question(2))
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
        conn = store.open(str(self.db))
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
        self.project = holophyte.config.project.Project(
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
        ok, why = holophyte.loop.claim.reuse_leftover(self.project, self.wt,
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

        ok, why = holophyte.loop.claim.reuse_leftover(self.project, self.wt,
                                                 self.branch)

        self.assertTrue(ok, why)
        self.assertIn("WIP: uncommitted leftovers preserved on reuse",
                      self.git("log", "-1", "--format=%s", cwd=self.wt))
        self.assertEqual(self.identities("HEAD"),
                         ["Operator operator@example.com"] * 2)

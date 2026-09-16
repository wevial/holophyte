"""PR conflict and send-back fixtures shared by the KO-440 regressions."""
import dataclasses
import io
import sqlite3
import subprocess
from unittest.mock import patch

import holophyte
import holophyte.operator
import holophyte.pr_status
import store
import store.tickets
from tests.fake_agent import APPROVE, Commit, Idle
from tests.loop_fixture import BRANCH

MINUTE = 60 * 1000
T0 = 1_700_000_000_000


class ConflictRefusalCases:
    def conflict_refusal(self, conflict=False):
        """GitHub refuses the first merge after main moves under the PR."""
        import test_babysitter
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        gh = self.calls.parent / "gh"
        text = gh.read_text()
        answer = f"    echo '{{\"sha\":\"{self.MERGE_SHA}\",\"merged\":true}}'"
        self.refusal = "gh: Pull Request has merge conflicts (HTTP 405)"
        marker = self.calls.parent / "refused"
        gh.write_text(text.replace(answer,
            f'    if [ ! -f "{marker}" ]; then\n'
            f'      touch "{marker}"; echo "{self.refusal}" >&2; exit 1\n'
            f'    fi\n{answer}'))
        path = "tests/test_file_sizes.py" if conflict else "MOVED.md"
        # Move the remote only after the initial candidate was reviewed.
        fixture = self
        class MoveMain:
            role = APPROVE.role
            def play(self, cwd, turn):
                fixture.moved = test_babysitter.ConflictingPullRequestTests.remote_main(
                    fixture, path, "main's line\n")
                return APPROVE.play(cwd, turn)
        return MoveMain()

    @staticmethod
    def ratchet_work():
        work = Commit("branch ratchet", path="tests/test_file_sizes.py",
                      body="branch's line\n")
        class CreateTests:
            role = work.role
            def play(self, cwd, turn):
                (cwd / "tests").mkdir(exist_ok=True)
                return work.play(cwd, turn)
        return CreateTests()

    def assert_conflict_merge_landed(self):
        pushes = self.pushed()
        self.assertEqual(len(pushes), 2)
        original, merged = [sha for _, sha in pushes]
        self.assertEqual(self.git("rev-parse", f"{merged}^1").strip(), original)
        self.assertEqual(self.git("rev-parse", f"{merged}^2").strip(), self.moved)
        calls = [v["sha"] for kind, v in self.api_calls() if kind == "merge"]
        self.assertEqual(calls, [original, merged])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"),
                         [("merged", self.MERGE_SHA)])

    def test_human_approval_conflict_refusal_preserves_the_candidate(self):
        review = self.conflict_refusal()
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.loop(Commit("candidate"), review, provider=self.provider())
        candidate = self.git("rev-parse", BRANCH).strip()
        holophyte.operator.approve(self.tgt, "KO-131", "merge this candidate",
                                   out=io.StringIO())
        fake, _ = self.loop(provider=self.provider())
        self.assertEqual(fake.roles, [])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), candidate)
        self.assertEqual(len(self.pushed()), 1)
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [candidate])
        self.assertIn(self.refusal, self.question())

    def test_conflict_refusal_merges_main_pushes_and_retries(self):
        review = self.conflict_refusal()
        self.loop(Commit("the scripted work"), review, APPROVE,
                  provider=self.provider())
        self.assert_conflict_merge_landed()

    def test_conflict_refusal_runs_the_implementer_and_continues(self):
        review = self.conflict_refusal(conflict=True)
        path = "tests/test_file_sizes.py"
        fake, _ = self.loop(self.ratchet_work(), review,
                            Commit("Merge main: retain both ratchets", path=path,
                                   body="branch's line\nmain's line\n"), APPROVE,
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement", "review"])
        self.assertIn(path, fake.turns[2].goal)
        self.assertIn("mid-merge", fake.turns[2].goal)
        self.assertEqual(fake.turns[2].cwd, self.worktrees / "ko-131-add-a-thing")
        self.assert_conflict_merge_landed()
        merged = self.pushed()[-1][1]
        self.assertEqual(self.git("show", f"{merged}:{path}"),
                         "branch's line\nmain's line\n")

    def test_unresolved_conflict_refusal_parks_with_the_refusal(self):
        review = self.conflict_refusal(conflict=True)
        path = "tests/test_file_sizes.py"
        fake, _ = self.loop(self.ratchet_work(), review, Idle("Cannot resolve"),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertIn(path, fake.turns[2].goal)
        self.assertIn(self.refusal, self.question())
        self.assertEqual(len(self.pushed()), 1)
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), self.pushed()[0][1])

    def _state_with_rest(self, rest):
        pull = holophyte.pr_status.parse_pr_url(self.URL)
        with patch.object(holophyte.pr_status, "graphql",
                          lambda *a, **k: self.pr_state(checks="SUCCESS")
                          ["data"]), \
                patch.object(holophyte.pr_status, "rest", rest):
            return holophyte.pr_status.pr_state(self.tgt, pull)


@dataclasses.dataclass
class ResolveMerge:
    """An implementer turn on a worktree left mid-merge: it records the paths
    git says are unmerged, writes `resolved` to `path`, commits the merge
    with a message naming both sides, then does one scripted commit of the
    ticket's own work."""

    path: str
    resolved: str
    conflicted: list = dataclasses.field(default_factory=list)

    role = "implement"

    def play(self, cwd, turn):
        self.conflicted = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"], cwd=cwd,
            capture_output=True, text=True).stdout.split()
        (cwd / self.path).write_text(self.resolved)
        self.git(cwd, "add", self.path)
        self.git(cwd, "commit", "-q", "-m",
                 "Merge main into the preserved branch: both tests")
        return Commit("the scripted work").play(cwd, turn)

    @staticmethod
    def git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True,
                       capture_output=True, text=True)


class StoreBabysitCases:
    def test_babysit_writes_its_own_action_and_the_old_word_is_refused(self):
        """KO-374: `store.babysit()` records the action 'babysit' on the
        parked run; 'shepherd', the word it wrote before, no longer passes
        the CHECK -- a hand-written row with it fails at the database."""
        for phase in ("working", "verifying", "reviewing", "merge_gate"):
            store.set_phase(self.conn, self.run, phase, now=T0 + MINUTE)
        store.park(self.conn, self.run, "awaiting_merge_approval",
                   pr_url="https://example.test/pull/1", now=T0 + 2 * MINUTE)
        store.tickets.transition(self.conn, self.ticket, "blocked_on_operator")

        self.conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?",
                          ("PR open: https://example.test/pull/1", self.ticket))
        store.babysit(self.conn, self.ticket, "look again",
                      now=T0 + 3 * MINUTE)

        self.assertEqual(
            self.rows('SELECT runId, source, "action" FROM interventions'),
            [(self.run, "human", "babysit")])
        self.assertEqual(self.rows("SELECT status, blockedQuestion FROM tickets"),
                         [("ready", None)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                'INSERT INTO interventions (runId, source, "trigger",'
                ' "action", at) VALUES (?, \'human\', \'manual\','
                ' \'shepherd\', ?)', (self.run, T0))

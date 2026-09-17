"""PR conflict and send-back fixtures shared by the KO-440 regressions."""
import dataclasses
import io
import sqlite3
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import holophyte
import holophyte.operator
import holophyte.pr_status
import store
import store.tickets
from tests.fake_agent import APPROVE, REQUEST_CHANGES, Commit, Idle, Reply
from tests.loop_fixture import BRANCH

MINUTE = 60 * 1000
T0 = 1_700_000_000_000


class ConflictRefusalCases:
    def refresh_wait(self, changed=False, checks="SUCCESS"):
        review = self.conflict_refusal(conflict=changed)
        (self.calls.parent / "refused").touch()  # This case reports CONFLICTING.
        def stamp(seconds):
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        self.serve(self.pr_state(mergeable="CONFLICTING", updated_at=stamp(800)),
                   self.pr_state(checks="PENDING", updated_at=stamp(1000)),
                   self.pr_state(checks=checks, updated_at=stamp(1000)))
        naps = []
        work = self.ratchet_work() if changed else Commit("candidate")
        fixes = [Commit("Resolve main", path="tests/test_file_sizes.py",
                        body="branch's line\nmain's line\n"), APPROVE
                 ] if changed else []
        with patch.object(holophyte.pr, "SLEEP", naps.append), \
                patch("holophyte.babysitter.time",
                      side_effect=lambda: 1000 + sum(naps)):
            out = self.main_output(work, review, Idle(""), *fixes,
                                   provider=self.provider())
        return out, naps

    def test_main_refresh_carries_review_and_quiet_but_waits_for_checks(self):
        out, naps = self.refresh_wait()
        self.assertEqual(self.last_fake.roles, ["implement", "review", "implement"])
        self.assertIn("green and quiet for 230s of the 300s", out)
        self.assertEqual(sum(naps), 100)
        head = self.pushed()[-1][1]
        self.assertEqual(self.read("SELECT summary FROM runEvents WHERE"
                                   " kind = 'pull_request' AND summary LIKE"
                                   " 'main refreshed%'"),
                         [(f"main refreshed at {head}; diff to main unchanged,"
                           " review and quiet carried forward",)])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_main_refresh_failed_checks_park(self):
        self.refresh_wait(checks="FAILURE")
        self.assertEqual(self.last_fake.roles, ["implement", "review", "implement"])
        self.assertEqual(self.read("SELECT phase, outcome FROM runs"),
                         [("awaiting_merge_approval", None)])
        self.assertIn("checks failure on the head commit", self.question())
        self.assertFalse([v for kind, v in self.api_calls() if kind == "merge"])

    def test_changed_main_merge_restarts_review_and_quiet(self):
        out, naps = self.refresh_wait(changed=True)
        self.assertEqual(self.last_fake.roles,
                         ["implement", "review", "implement", "implement", "review"])
        self.assertIn("green and quiet for 30s of the 300s", out)
        self.assertEqual(sum(naps), 300)
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])

    def test_conflict_push_waits_for_the_head_to_catch_up(self):
        review = self.conflict_refusal(heads=("old", "pushed"))
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("candidate"), review, Idle(""), APPROVE,
                      provider=self.provider())
        self.assert_conflict_merge_landed()
        self.assertEqual(naps, [holophyte.pr.CHECK_POLL_S])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge", "state", "state", "merge"])

    def test_conflict_push_head_timeout_parks_naming_both_shas(self):
        review = self.conflict_refusal(heads=("old",))
        self.configure('[merge]\nmode = "pr"\npr_poll_sec = 31\n')
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            self.loop(Commit("candidate"), review, Idle(""),
                      provider=self.provider())
        original, pushed = [sha for _, sha in self.pushed()]
        self.assertEqual(sum(naps), 31)
        self.assertEqual(self.read("SELECT phase, outcome, candidateSha FROM runs"),
                         [("awaiting_merge_approval", None, pushed)])
        self.assertIn(f"the pull request's head is {original[:12]} after 31s;"
                      f" the babysitter pushed {pushed[:12]}", self.question())
        self.assertEqual([v["sha"] for kind, v in self.api_calls()
                          if kind == "merge"], [original])

    def review_fix_propagation(self, catches_up):
        self.resume_rejected_fix()
        self.configure('[merge]\nmode = "pr"\npr_poll_sec = 31\n')
        old = self.git("rev-parse", BRANCH).strip()
        states = [self.pr_state(head=old), self.pr_state(head=old)]
        if catches_up:
            states += [self.pr_state(checks="PENDING"), self.pr_state()]
        self.serve(*states)
        naps = []
        with patch.object(holophyte.pr, "SLEEP", naps.append):
            fake, _ = self.loop(REQUEST_CHANGES, Commit("review fix"), APPROVE,
                                provider=self.provider())
        return old, fake, naps

    def resume_rejected_fix(self):
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.DEFECT]), self.pr_state()])
        self.loop(Commit("candidate"), APPROVE, Idle(""),
                  Reply("THREAD 1: ADDRESS -- a real crash"), Commit("thread fix"),
                  REQUEST_CHANGES, provider=self.provider())
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "repair the pin",
                                         out=io.StringIO())

    def conflict_refusal(self, conflict=False, heads=()):
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
                if heads:
                    old = fixture.git("rev-parse", BRANCH).strip()
                    fixture.serve(fixture.pr_state(), *[
                        fixture.pr_state(head=old if head == "old" else fixture.HEAD)
                        for head in heads])
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
        self.loop(Commit("candidate"), review, Idle(""), provider=self.provider())
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
        self.loop(Commit("the scripted work"), review, Idle(""), APPROVE,
                  provider=self.provider())
        self.assert_conflict_merge_landed()

    def test_conflict_refusal_runs_the_implementer_and_continues(self):
        review = self.conflict_refusal(conflict=True)
        path = "tests/test_file_sizes.py"
        fake, _ = self.loop(self.ratchet_work(), review, Idle(""),
                            Commit("Merge main: retain both ratchets", path=path,
                                   body="branch's line\nmain's line\n"), APPROVE,
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement", "review"])
        self.assertIn(path, fake.turns[3].goal)
        self.assertIn("mid-merge", fake.turns[3].goal)
        self.assertEqual(fake.turns[3].cwd, self.worktrees / "ko-131-add-a-thing")
        self.assert_conflict_merge_landed()
        merged = self.pushed()[-1][1]
        self.assertEqual(self.git("show", f"{merged}:{path}"),
                         "branch's line\nmain's line\n")

    def test_unresolved_conflict_refusal_parks_with_the_refusal(self):
        review = self.conflict_refusal(conflict=True)
        path = "tests/test_file_sizes.py"
        fake, _ = self.loop(self.ratchet_work(), review, Idle(""),
                            Idle("Cannot resolve"),
                            provider=self.provider())
        self.assertEqual(fake.roles, ["implement", "review", "implement",
                                      "implement"])
        self.assertIn(path, fake.turns[3].goal)
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


class SpentCapReview:
    """A reviewer reached with the live run's round allowance already spent."""
    role = APPROVE.role

    def __init__(self, db, reply):
        self.db, self.reply = db, reply
        self.count = None

    def play(self, cwd, turn):
        with sqlite3.connect(self.db) as conn:
            run_id = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            self.count = conn.execute(
                "SELECT COUNT(*) FROM reviewRounds WHERE runId = ?",
                (run_id,)).fetchone()[0]
            store.set_review_round_cap(conn, run_id, self.count)
        return self.reply.play(cwd, turn)


class OperatorNoteCase:
    def operator_note_pass(self, bots):
        import store
        from store.operator_notes import notes, send_back
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state()])
        self.loop(Commit("candidate"), APPROVE, Idle(""), provider=self.provider())
        with store.open(str(self.tgt.store_path)) as conn:
            run_id = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            note_id = send_back(conn, run_id, "remove the subheader", "maintainer")
        for path in self.api_dir.iterdir():
            path.unlink()
        threads = [self.DEFECT, self.NIT] if bots else []
        self.serve(self.pr_state(threads), self.pr_state())
        verdict = ([Reply("THREAD 1: ADDRESS -- crash\nTHREAD 2: ADDRESS -- style")]
                   if bots else [])
        fake, _ = self.loop(*verdict, Commit("apply requested changes"),
                            provider=self.provider())
        self.assertEqual(fake.roles, (["adjudicate"] if bots else []) + ["implement"])
        brief = fake.turns[-1].goal
        self.assertIn("Maintainer's instruction "
                      "(amends the ticket where they conflict):",
                      brief)
        self.assertIn("remove the subheader", brief)
        self.assertIn(f"operator_note event {note_id}",
                      self.git("log", "-1", "--format=%B", BRANCH))
        posts = [(kind, value) for kind, value in self.api_calls()
                 if kind in ("reply", "resolve")]
        self.assertEqual(len(posts), 4 if bots else 0)
        if bots:
            self.assertIn(self.DEFECT[3], brief)
            self.assertIn(self.NIT[3], brief)
            self.assertNotIn("THREAD 3 --", fake.turns[0].goal)
            self.assertTrue(all("remove the subheader" not in str(value)
                                for _, value in posts))
        with store.open(str(self.tgt.store_path)) as conn:
            current = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            instruction, = notes(conn, current)
            self.assertTrue(instruction["consumed"])
            self.assertEqual(instruction["run_id"], current)
            self.assertEqual(notes(conn, current, pending=True), [])

        return current, note_id

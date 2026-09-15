"""`holophyte.pullrequest` under `[merge] mode = "pr"`, driven end to end.

The push and the open, the park on the pull request and the skip line it
earns, the written PR text, the resume on the pull request, the parked
pull request's fate on GitHub and on the tick, and the merge through the
pull request's API. `MergeModeFixture` (`loop_fixture.py`) is the shared
base: a real throwaway repo with a fake `gh` and a scripted GitHub. The
pass over threads and checks lives in `test_babysit_pass.py`.

Run: python3 -m unittest discover -s tests -p 'test_pullrequest*' -v
"""
from __future__ import annotations

import io
import json
import shlex
import sqlite3
import sys
import unittest
from pathlib import Path
from time import monotonic
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))  # factory.py imports store/ticket_template by name
# `fake_agent` is a helper, not a test module: discovery never imports it, and
# how this file is imported decides whether `tests/` is on the path at all.
# Putting it there explicitly makes `discover -s tests` and `-m unittest
# tests.<name>` resolve the harness the same way.
sys.path.insert(0, str(HERE))
from fake_agent import (  # noqa: E402 - after the sys.path insert above
    APPROVE,
    Commit,
    FakeAgent,
    Idle,
    Reply,
)
from loop_fixture import (  # noqa: E402 - after the sys.path insert above
    BRANCH,
    TICK,
    FakePool,
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.config_tables  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.pool  # noqa: E402 - after the sys.path insert above
import holophyte.pr  # noqa: E402 - after the sys.path insert above
import holophyte.pr_status  # noqa: E402 - after the sys.path insert above
import holophyte.pullrequest  # noqa: E402 - after the sys.path insert above


class MergeModePullRequestTests(MergeModeFixture):
    """The `[merge] mode = "pr"` tests that open, park, resume and merge
    the pull request; the passes over threads and checks are
    `MergeModeBabysitPassTests` (`test_babysit_pass.py`)."""

    def test_pr_pushes_opens_the_pull_request_and_parks_the_run(self):
        """Push, then create, in that order; the PR is titled `KO-n: TITLE`
        and its body is the ticket body followed by the run's FINDINGS
        entry; the babysitter's one pass finds no thread and green checks,
        and under `approve = "human"` the run parks "ready to merge": the
        URL `gh` printed is the run's `prUrl` and heads the ticket's
        question; main is untouched, the branch and worktree stay, and the
        run is parked alive in `awaiting_merge_approval` with its lease
        released -- the `approve = "human"` park, with a URL."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        provider = self.provider()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review"])
        calls = self.recorded()
        # The seventh is the park reading the pull request once more, after
        # the pass's own writes, for the activity mark it records (KO-362);
        # the eighth is the pass after the park asking GitHub whether the
        # parked pull request has been merged (KO-359).
        self.assertEqual(len(calls), 8, calls)
        self.assertEqual(calls[6:], ["gh api --hostname github.com --method"
                                     " POST graphql --input -"] * 2)
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        # Between the push and the create, the open step's lookup of an
        # open pull request on the branch (KO-407) -- answered none here.
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        # Beside the state query: the head's check runs and main's rules,
        # so a rollup that says success before the checks have reported is
        # not read as green.
        tip = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(calls[4:6], [
            "gh api --hostname github.com --method GET"
            f" repos/example/repo/commits/{tip}/check-runs?per_page=100",
            "gh api --hostname github.com --method GET"
            " repos/example/repo/rules/branches/main"])
        # Pinned to the repository the push went to, not `gh`'s own default
        # repository (`gh repo set-default`), which can point elsewhere.
        self.assertEqual(
            calls[2],
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title KO-131: add a thing --body-file -")
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        body = self.pr_body.read_text()
        self.assertIn("The thing, added.", body)
        self.assertIn("— KO-131", body)  # the FINDINGS entry heading
        self.assertIn("estimate: 5 min · rounds: 1", body)
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(
            self.read("SELECT phase, endedAt, outcome, prUrl, candidateSha"
                      " FROM runs"),
            [("awaiting_merge_approval", None, None, self.URL,
              self.git("rev-parse", BRANCH).strip())])
        self.assertEqual(self.read("SELECT activeRunId FROM projects"),
                         [(None,)])
        question = self.question()
        self.assertTrue(question.startswith(f"PR open: {self.URL}\n"),
                        question)
        self.assertIn("ready to merge", question)
        (_, comment) = provider.comments[-1]
        self.assertIn("PR OPEN", comment)
        self.assertIn(self.URL, comment)
        # The pass is a round of the run, stamped as the checks' pass.
        self.assertEqual(
            self.read("SELECT round, verdict, reviewerModel FROM reviewRounds"
                      " ORDER BY round")[-1],
            (2, "pass", "github:ci"))

    def test_an_open_pull_request_on_the_branch_is_adopted_not_created(self):
        """KO-407: a run resumed on a branch its failed predecessor left
        open as a pull request -- the requeue scenario -- must not call
        `gh pr create`: GitHub refuses a second open PR for one head, and
        the run used to fail after doing everything right. The open step
        asks GitHub first; a hit is adopted -- `runs.prUrl` is that PR --
        and the run goes straight into a babysit pass, which here finds
        green checks and no threads and parks "ready to merge"."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        adopted = "https://github.com/example/repo/pull/2177"
        self.fake_route(open_pr=adopted)
        provider = self.provider()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review"])
        calls = self.recorded()
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        # The lookup, between the push and where the create would be --
        # and no `gh pr create` follows it.
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        body = json.loads((self.api_dir / "1.json").read_text())
        self.assertIn("headRefName", body["query"])
        self.assertIn("states: OPEN", body["query"])
        self.assertEqual(body["variables"],
                         {"owner": "example", "name": "repo",
                          "branch": BRANCH})
        self.assertFalse(any(c.startswith("gh pr create") for c in calls),
                         calls)
        # The adopted PR is babysat like an opened one: the state read is
        # the pass's, the round is stamped, and the park names the PR.
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        self.assertEqual(
            self.read("SELECT phase, prUrl, candidateSha FROM runs"),
            [("awaiting_merge_approval", adopted,
              self.git("rev-parse", BRANCH).strip())])
        question = self.question()
        self.assertTrue(question.startswith(f"PR open: {adopted}\n"),
                        question)
        self.assertIn("ready to merge", question)
        (_, comment) = provider.comments[-1]
        self.assertIn("PR OPEN", comment)
        self.assertIn(adopted, comment)
        self.assertEqual(
            self.read("SELECT verdict, reviewerModel FROM reviewRounds"
                      " ORDER BY round")[-1],
            ("pass", "github:ci"))

    def test_an_open_pull_request_is_adopted_through_a_slashed_origin(self):
        """An `origin` ending in `/` -- `https://github.com/example/repo/`
        is a URL `git remote add` accepts -- must still reach GitHub:
        `_origin_pull()` gluing `/pull/0` onto the slash would hand
        `PR_URL_RE` a doubled slash it refuses, the lookup would return
        None without asking, and `gh pr create` would fire and fail just
        as before KO-407. With the slash normalized the branch's open PR
        is found and adopted."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        adopted = "https://github.com/example/repo/pull/2177"
        self.fake_route(open_pr=adopted)
        self.git("remote", "set-url", "origin",
                 "https://github.com/example/repo/")

        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())

        calls = self.recorded()
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        body = json.loads((self.api_dir / "1.json").read_text())
        self.assertEqual(body["variables"],
                         {"owner": "example", "name": "repo",
                          "branch": BRANCH})
        self.assertFalse(any(c.startswith("gh pr create") for c in calls),
                         calls)
        self.assertEqual(self.read("SELECT prUrl FROM runs"), [(adopted,)])

    def test_no_open_pull_request_on_the_branch_opens_one_as_today(self):
        """The lookup answering no open pull request for the branch: the
        push and the lookup run, then `gh pr create` opens the PR exactly
        as before (KO-407)."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()  # open_pr=None: no open pull request

        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())

        calls = self.recorded()
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        self.assertEqual(
            calls[2],
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title KO-131: add a thing --body-file -")
        self.assertEqual(self.read("SELECT prUrl FROM runs"),
                         [(self.URL,)])

    AGENTS_MD = ("# Agent guide\n\nTitle starts with [Feature Name]."
                 " No testing plan.\n")
    WRITTEN = Idle("Reading the diff.\n"
                   "TITLE: [Contacts] Put Contact Name first\n\n"
                   "The two forms now ask for the contact's name before"
                   " anything else.\n\nThe thing file is what changed.\n")

    def written_target(self):
        """`pr_text = "written"` with a style line, and an `AGENTS.md` on
        main for the worktree to carry."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       'pr_text = "written"\n'
                       'pr_style = "No ticket identifier in the title."\n')
        (self.target / "AGENTS.md").write_text(self.AGENTS_MD)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "agent guide")
        self.base = self.git("rev-parse", "main").strip()
        self.fake_route()
        return StubProvider(dict(
            a_task(), body=self.BODY,
            url="https://linear.app/example/issue/KO-131/add-a-thing"))

    def test_a_ticket_parked_on_a_pr_is_skipped_by_its_url(self):
        """The park keeps the ticket in Todo, so the next pass is offered it
        first. The skip line names the pull request and the `--approve`
        that merges it -- not a failure -- and the ticket behind it is
        claimed and merged in the same pass."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        self.assertEqual(self.question().split("\n")[0],
                         f"PR open: {self.URL}")
        # The pass after the park, merging locally so the second ticket's
        # own path is not this test's subject.
        self.configure("")
        parked, other = a_task(), dict(a_task(2), title="add another thing")

        out = self.main_output(Commit("the other work"), APPROVE,
                               provider=StubProvider(parked, other))

        self.assertIn(f"[holo2] KO-131 is parked on PR {self.URL} awaiting"
                      " --approve KO-131; skipping it\n", out)
        self.assertNotIn("failures", out)
        self.assertIn("the other work", self.subjects())
        self.assertEqual(
            self.read("SELECT linearIdentifier, status FROM tickets"
                      " ORDER BY id"),
            [("KO-131", "blocked_on_operator"), ("KO-132", "merged")])

    def test_a_written_pr_takes_the_turns_title_and_body(self):
        """`pr_text = "written"`: after the approval one more implementer
        turn is given the diff, the ticket, the repository's `AGENTS.md`
        and the style line; the PR is created with the title it answered
        and a body ending with the Linear line, with no FINDINGS entry."""
        provider = self.written_target()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            self.WRITTEN, provider=provider)

        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        prompt = fake.turns[2].goal
        self.assertEqual(fake.turns[2].cwd,
                         self.worktrees / "ko-131-add-a-thing")
        self.assertIn("+the scripted work", prompt)  # the diff
        self.assertIn("The thing, added.", prompt)  # the ticket
        self.assertIn(self.AGENTS_MD.strip(), prompt)
        self.assertIn("No ticket identifier in the title.", prompt)
        # A small budget from the run's remaining box, never the whole run.
        self.assertTrue(60 <= fake.turns[2].timeout <= 5 * 60,
                        fake.turns[2].timeout)
        create = [c for c in self.recorded() if c.startswith("gh pr create")]
        self.assertEqual(create, [
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title [Contacts] Put Contact Name first --body-file -"])
        body = self.pr_body.read_text()
        self.assertTrue(body.startswith(
            "The two forms now ask for the contact's name"), body)
        self.assertEqual(
            body.rstrip().splitlines()[-1],
            "Linear: KO-131 (https://linear.app/example/issue/KO-131/"
            "add-a-thing)")
        self.assertNotIn("\u2014 KO-131", body)  # no FINDINGS entry heading
        self.assertNotIn("estimate: 5 min", body)
        self.assertNotIn("Reading the diff.", body)
        self.assertEqual(
            self.read("SELECT phase, prUrl FROM runs"),
            [("awaiting_merge_approval", self.URL)])

    def test_a_reply_without_a_title_falls_back_to_the_ticket_form(self):
        """No `TITLE:` line: the PR is still opened, titled `KO-n: TITLE`
        with the ticket body and the FINDINGS entry, and one printed line
        says the written text was refused."""
        provider = self.written_target()

        out = self.main_output(
            Commit("the scripted work"), APPROVE,
            Idle("I would call this [Contacts] Put Contact Name first.\n\n"
                 "The two forms now \u2026"),
            provider=provider)

        create = [c for c in self.recorded() if c.startswith("gh pr create")]
        self.assertEqual(create, [
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title KO-131: add a thing --body-file -"])
        body = self.pr_body.read_text()
        self.assertIn("The thing, added.", body)
        self.assertIn("\u2014 KO-131", body)
        refused = [line for line in out.splitlines()
                   if "written PR text refused" in line]
        self.assertEqual(len(refused), 1, out)
        self.assertIn("KO-131", refused[0])
        self.assertIn("TITLE:", refused[0])
        self.assertEqual(
            self.read("SELECT phase, prUrl FROM runs"),
            [("awaiting_merge_approval", self.URL)])

    PR_TEMPLATE = ("## Summary\n\n<!-- what the change does. -->\n\n"
                   "## Why\n\n<!-- why it is needed. -->\n")

    # The prompt the frozen base's written turn was handed for this
    # scenario, recorded once from `refs/review/base`: criterion 2's
    # oracle. Comparing the live prompt against the same run's own output
    # would let a drift in the shared text move both sides together.
    RECORDED = (ROOT / "tests" / "fixtures" / "pullrequest"
                / "written_prompt_base.txt")

    def test_the_written_turn_fills_the_repositorys_pr_template(self):
        """KO-430: a worktree carrying `.github/pull_request_template.md`
        gives the written turn the file under a "fill its sections"
        heading, ahead of the style line; a worktree without one gets
        the frozen base's prompt byte for byte, witnessed against the
        recording."""
        provider = self.written_target()
        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            self.WRITTEN, provider=provider)
        # A worktree with no template file: the recorded base prompt,
        # byte for byte.
        base = self.RECORDED.read_text()
        self.assertEqual(fake.turns[2].goal, base)

        # The same candidate's parked worktree now carrying the file: the
        # rebuilt prompt is the recorded one plus exactly the template's
        # part.
        wt = self.worktrees / "ko-131-add-a-thing"
        (wt / ".github").mkdir()
        (wt / ".github" / "pull_request_template.md").write_text(
            self.PR_TEMPLATE)
        turn = FakeAgent(Idle("TITLE: [Contacts] Put Contact Name first\n\n"
                              "The two forms now ask.\n"))
        with patch.object(holophyte.loop, "agent", turn):
            holophyte.pullrequest._written_pr_text(
                self.tgt, None, None, "KO-131", "add a thing", BRANCH,
                self.BODY.strip(), 60, wt, monotonic(), 5,
                "https://linear.app/example/issue/KO-131/add-a-thing")
        filled = turn.turns[0].goal
        self.assertIn("## Summary", filled)  # the template's first heading
        part = ("Pull request template, fill its sections:\n\n"
                + self.PR_TEMPLATE.strip() + "\n\n")
        self.assertIn(part, filled)
        self.assertLess(filled.index(part),
                        filled.index("Style instructions"))
        self.assertEqual(filled.replace(part, ""), base)

    def test_a_squash_only_repository_merges_with_its_configured_method(self):
        """`[merge] pr_merge_method = "squash"`: the one `PUT
        .../pulls/7/merge` carries `merge_method` `squash`, still pinned to
        the approved candidate, and the run records the sha GitHub answered
        -- for a squash, the new commit on `main`, not a merge commit."""
        self.configure('[merge]\nmode = "pr"\npr_merge_method = "squash"\n')
        self.fake_route()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=self.provider())

        self.assertEqual(self.api_calls()[-1],
                         ("merge", {"merge_method": "squash",
                                    "sha": fake.turns[1].candidate_sha}))
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])

    def test_a_slow_push_keeps_the_run_heartbeating(self):
        """The push and the create block for as long as the remote takes,
        outside any agent turn or verify: a push longer than the stale
        budget was a `stale_heartbeat` trip for the supervisor, which could
        fail the run before its URL was recorded. The fake push here samples
        the run's `lastHeartbeat` from the store while it takes longer than
        the whole stale budget; the beat must move under it."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       "[supervisor]\nheartbeat_stale_min = 0.01\n")
        knobs = holophyte.config_tables.sweep_config(self.tgt)
        budget_s = knobs.heartbeat_stale_ms * knobs.stale_strikes / 1000
        samples = self.db.parent / "heartbeats.log"
        sampler = (
            "import sqlite3, sys, time\n"
            f"deadline = time.monotonic() + {budget_s * 5 / 3}\n"
            f"conn = sqlite3.connect({str(self.db)!r})\n"
            "while time.monotonic() < deadline:\n"
            "    time.sleep(0.2)\n"
            "    row = conn.execute('SELECT phase, lastHeartbeat FROM runs')"
            ".fetchone()\n"
            f"    open({str(samples)!r}, 'a').write('%s %s\\n' % row)\n")
        self.fake_route(push_sh=f"  {sys.executable} -c {shlex.quote(sampler)}")

        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())

        seen = [line.split() for line in samples.read_text().splitlines()]
        self.assertGreaterEqual(len(seen), 4, seen)
        self.assertEqual({phase for phase, _ in seen}, {"merge_gate"})
        beats = [int(beat) for _, beat in seen]
        self.assertGreater(beats[-1], beats[0])
        # No gap between beats reached the stale threshold.
        self.assertLess(max(b - a for a, b in zip(beats, beats[1:])),
                        knobs.heartbeat_stale_ms)
        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])

    def test_a_green_quiet_pr_under_auto_merges_through_the_api(self):
        """Acceptance: zero unresolved threads and green checks with
        `approve = "auto"`: the PR is merged through the merge API -- one
        `PUT .../pulls/7/merge`, never a local merge or a push of main --
        and the run is marked merged with the sha GitHub answered; the
        worktree and local branch are cleaned up, local main untouched."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route()
        provider = self.provider()

        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            provider=provider)

        self.assertEqual(fake.roles, ["implement", "review"])
        self.assertEqual(self.api_calls(),
                         [("state", {"owner": "example", "name": "repo",
                                     "number": 7, "after": None}),
                          # Pinned to the candidate the reviewer approved.
                          ("merge", {"merge_method": "merge",
                                     "sha": fake.turns[1].candidate_sha})])
        self.assertIn("gh api --hostname github.com --method PUT"
                      " repos/example/repo/pulls/7/merge --input -",
                      self.recorded())
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [f"git push origin {BRANCH}"])
        # Local main is untouched: the candidate landed on GitHub's main,
        # and the close-out renders no FINDINGS.md by default (KO-363).
        self.assertEqual(self.subjects(), ["base"])
        self.assertNotIn(BRANCH, self.branches())
        self.assertFalse((self.worktrees / "ko-131-add-a-thing").exists())
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha, prUrl FROM runs"),
            [("done", "merged", self.MERGE_SHA, None)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        (_, comment) = provider.comments[-1]
        self.assertIn(f"MERGED through {self.URL} as {self.MERGE_SHA}",
                      comment)
        # The persisted ledger line for a pass with nothing to answer opens
        # the way every pass does, so the console reads one shape (KO-373).
        ((ledger,),) = self.read(
            "SELECT text FROM ledger WHERE kind = 'round' AND text LIKE"
            " 'Babysit pass%'")
        self.assertTrue(ledger.startswith(f"Babysit pass 1 over {self.URL}"),
                        ledger)

    def park_on_a_declined_nit_with_a_bare_origin(self):
        """A run parked on a declined nit at its approved sha, and the
        target's `origin` then pointed at a bare repository holding the
        candidate branch -- the remote a person pushes on top of. Returns
        `(approved sha, bare path, scratch clone path)`; the clone is where
        a test makes the person's commits, and `publish()` moves them to
        the bare branch (`force` for a rewritten history)."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.NIT]), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE,
                  Reply("THREAD 1: DECLINE -- a naming preference"),
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        for path in self.api_dir.iterdir():
            path.unlink()
        bare = self.worktrees.parent / "origin.git"
        self.git("init", "-q", "--bare", str(bare))
        self.git("fetch", "-q", str(self.target), f"{BRANCH}:{BRANCH}",
                 cwd=bare)
        self.git("remote", "set-url", "origin", str(bare))
        clone = self.worktrees.parent / "person"
        self.git("clone", "-q", "-b", BRANCH, str(bare), str(clone))
        self.git("config", "user.email", "person@example.invalid", cwd=clone)
        self.git("config", "user.name", "A Person", cwd=clone)
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "look again",
                                       out=io.StringIO())
        return approved, bare, clone

    def publish(self, clone, bare, force=False):
        """The person's branch in `clone` moved onto the bare remote --
        by a fetch into the bare repository, since the fixture's `git
        push` is the witnessed fake."""
        spec = f"{'+' if force else ''}{BRANCH}:{BRANCH}"
        self.git("fetch", "-q", str(clone), spec, cwd=bare)
        return self.git("rev-parse", BRANCH, cwd=bare).strip()

    def test_a_babysit_resume_fast_forwards_to_the_remote_branch(self):
        """A person pushed one commit on top of the parked candidate. The
        resume fetches the branch, fast-forwards the worktree and the
        local branch to the remote's head, notes the fast-forward in the
        ledger naming one commit, and judges that head against the
        approved sha as a fix round's commit is judged: a review, whose
        approval lets the green, quiet PR merge."""
        approved, bare, clone = self.park_on_a_declined_nit_with_a_bare_origin()
        (clone / "README.md").write_text("a person's touch\n")
        self.git("commit", "-q", "-am", "operator: adjust the candidate",
                 cwd=clone)
        theirs = self.publish(clone, bare)
        self.assertNotEqual(theirs, approved)

        fake, _ = self.loop(APPROVE, provider=self.provider())

        # The merged run removes the worktree and branch at close-out, so
        # the fast-forward is witnessed by what the review was handed and
        # the sha the merge recorded, not by a branch that no longer exists.
        self.assertEqual(fake.roles, ["review"])
        self.assertEqual(fake.turns[0].candidate_sha, theirs)
        # The new head is judged against the sha the reviewer approved,
        # as a fix round's commit is: the brief names that approval, and
        # would say the last review asked for changes had it been lost.
        self.assertIn(f"candidate was approved at {approved[:12]}",
                      fake.turns[0].goal)
        self.assertEqual(
            self.read("SELECT summary FROM runEvents WHERE runId = 2 AND"
                      " summary LIKE 'resuming run%'"),
            [(f"resuming run 1's candidate {BRANCH} at {theirs[:12]} on"
              f" {self.URL} for another babysit pass",)])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE kind = 'note' AND text"
                      " LIKE 'Fast-forwarded%'"),
            [(f"Fast-forwarded {BRANCH} to {theirs} from origin (1 commit(s)"
              " pushed by someone else)",)])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome, mergeSha FROM runs"
                                   " WHERE id = 2"),
                         [("merged", self.MERGE_SHA)])

    def test_a_babysit_resume_parks_when_the_branches_diverged(self):
        """The remote branch was rewritten past the candidate rather than
        built on it. Nothing fast-forwards: the worktree and the local
        branch stay at the candidate, and the run parks with a question
        naming both shas, no review and no merge."""
        approved, bare, clone = self.park_on_a_declined_nit_with_a_bare_origin()
        self.git("reset", "-q", "--hard", "HEAD~1", cwd=clone)
        (clone / "README.md").write_text("rewritten\n")
        self.git("commit", "-q", "-am", "operator: a rewrite", cwd=clone)
        theirs = self.publish(clone, bare, force=True)

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual(self.api_calls(), [])
        self.assertEqual(self.git("rev-parse", BRANCH).strip(), approved)
        self.assertEqual(self.git("rev-parse", "HEAD",
                                  cwd=self.worktrees / "ko-131-add-a-thing")
                         .strip(), approved)
        question = self.question()
        self.assertIn(approved[:12], question)
        self.assertIn(theirs[:12], question)
        self.assertIn("diverged", question)
        self.assertEqual(self.read("SELECT phase, outcome, candidateSha FROM"
                                   " runs WHERE id = 2"),
                         [("awaiting_merge_approval", None, approved)])

    def test_a_babysit_resume_with_an_equal_remote_writes_no_note(self):
        """The remote holds exactly the candidate: no note is written, and
        the pass goes on as before -- the approved sha merges with no
        second review."""
        approved, bare, clone = self.park_on_a_declined_nit_with_a_bare_origin()

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual(
            self.read("SELECT summary FROM runEvents WHERE runId = 2 AND"
                      " summary LIKE 'resuming run%'"),
            [(f"resuming run 1's candidate {BRANCH} at {approved[:12]} on"
              f" {self.URL} for another babysit pass",)])
        self.assertEqual(
            self.read("SELECT text FROM ledger WHERE text LIKE"
                      " 'Fast-forwarded%'"), [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual(self.read("SELECT outcome FROM runs WHERE id = 2"),
                         [("merged",)])

    def test_an_approval_of_an_open_pull_request_merges_it_through_the_api(
            self):
        """`--approve KO-n` on a run parked with a PR open is the human's
        "merge": the resumed run babysits the PR once more and, green and
        quiet, merges it through the API -- no implementer, no reviewer,
        no push, no local merge, main untouched."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        self.calls.unlink()
        for path in self.api_dir.iterdir():
            path.unlink()
        holophyte.operator.approve(self.tgt, "KO-131", "looks fine",
                               out=io.StringIO())

        fake, guard = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual(guard.spawned, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "merge"])
        self.assertEqual([c for c in self.recorded() if c.startswith("git")],
                         [])
        self.assertEqual(self.subjects(), ["base"])  # local main untouched
        self.assertNotIn(BRANCH, self.branches())
        self.assertEqual(
            self.read("SELECT id, phase, outcome, resumePhase, prUrl,"
                      " candidateSha, mergeSha FROM runs ORDER BY id"),
            [(1, "failed", "abandoned", "merge_gate", self.URL, approved,
              None),
             (2, "done", "merged", None, None, None, self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])

    def test_a_babysitter_release_parks_again_rather_than_merging(self):
        """`--babysit KO-n` is "look again", not "merge": the resumed run
        babysits the PR and, green and quiet under `approve = "human"`,
        parks again on the same URL."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        holophyte.operator.babysit_ticket(self.tgt, "KO-131", "bots are done",
                                       out=io.StringIO())

        fake, _ = self.loop(provider=self.provider())

        self.assertEqual(fake.roles, [])
        self.assertEqual([kind for kind, _ in self.api_calls()],
                         ["state", "state"])
        self.assertEqual(
            self.read("SELECT id, phase, outcome, prUrl FROM runs"
                      " ORDER BY id"),
            [(1, "failed", "abandoned", self.URL),
             (2, "awaiting_merge_approval", None, self.URL)])
        self.assertEqual(
            self.read('SELECT "action" FROM interventions'), [("babysit",)])


    # What GitHub says about a parked pull request when the reconcile asks
    # (`pr_status.PULL_QUERY`'s node): merged by a coworker, closed
    # unmerged, open.
    MERGED_PULL = {"state": "MERGED", "merged": True,
                   "mergeCommit": {"oid": MergeModeFixture.MERGE_SHA},
                   "mergedBy": {"login": "coworker"}}
    CLOSED_PULL = {"state": "CLOSED", "merged": False, "mergeCommit": None,
                   "mergedBy": None}
    OPEN_PULL = {"state": "OPEN", "merged": False, "mergeCommit": None,
                 "mergedBy": None}

    def fake_client(self, *answers, rate=None):
        """The reconcile's GitHub, faked: `holophyte.pr_status.graphql`
        answers each ask with the next of `answers` (the last one
        forever) and
        records the pull request and variables it was asked about. An
        answer that is an exception is raised instead: GitHub down.
        `rate` is the `rateLimit` node every answer carries, when one
        does. Only the pull-status read is faked here: the babysitter's own
        reads and writes still go to the scripted `gh`."""
        asked = []
        real = holophyte.pr.graphql

        def graphql(target, pull, query, variables):
            if "mergedBy" not in query:
                return real(target, pull, query, variables)
            asked.append((pull.url, query, variables))
            node = answers[min(len(asked), len(answers)) - 1]
            if isinstance(node, Exception):
                raise node
            data = {"repository": {"pullRequest": node}}
            if rate is not None:
                data["rateLimit"] = rate
            return data

        patcher = patch.object(holophyte.pr_status, "graphql", graphql)
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def parked_on_pr(self, extra=""):
        """A run parked on its pull request under `approve = "human"`, the
        state every reconcile test starts from; `extra` is further config
        text appended after the `[merge]` table."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n' + extra)
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE,
                  provider=self.provider())
        self.assertEqual(self.read("SELECT phase, prUrl FROM runs"),
                         [("awaiting_merge_approval", self.URL)])

    def test_a_pull_request_merged_on_github_ships_its_parked_run(self):
        """KO-359: a person merged the pull request on GitHub instead of
        saying `--approve`. The next pass asks GitHub once, and the merge
        is the approval: the parked run ends `merged` with the pull
        request's merge commit as its `mergeSha`, the ticket is `merged`
        and the board saw Done, the ledger names who merged it, the local
        branch is gone and the findings window, where a target renders
        one, shows the run."""
        self.parked_on_pr('[report]\nfindings = "repo"\n')
        asked = self.fake_client(self.MERGED_PULL)
        provider = StubProvider()

        out = self.main_output(provider=provider)

        self.assertEqual([(url, v["number"]) for url, _, v in asked],
                         [(self.URL, 7)])
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha, prUrl FROM runs"),
            [("done", "merged", self.MERGE_SHA, self.URL)])
        self.assertEqual(
            self.read("SELECT status, blockedQuestion FROM tickets"),
            [("merged", None)])
        self.assertEqual(provider.states, [("iss-131", "Done")])
        self.assertEqual(self.read('SELECT "action" FROM interventions'),
                         [("approve",)])
        (merge_line,) = [text for (text,) in self.read(
            "SELECT text FROM ledger WHERE kind = 'merge'")]
        self.assertIn("coworker", merge_line)
        self.assertIn(self.MERGE_SHA, merge_line)
        self.assertIn(f"{self.URL} was merged on GitHub by coworker", out)
        self.assertNotIn(BRANCH, self.branches())
        self.assertIn("KO-131", (self.target / "FINDINGS.md").read_text())
        self.assertEqual(self.subjects(), ["base"])  # local main not moved

    def test_a_merged_pull_request_ships_when_linear_already_says_done(self):
        """Review of KO-359: the person who merged the pull request on
        GitHub also moved the ticket to Done on the board. At startup the
        mirror reconcile used to see Done first and walk the ticket
        `merged` on its own, and the pull request was never asked about:
        the run stayed parked with no outcome and no `mergeSha`. GitHub
        is asked before the mirror is repaired, so the run ships."""
        self.parked_on_pr()
        asked = self.fake_client(self.MERGED_PULL)
        provider = StubProvider()
        provider.closed = {"KO-131": "completed"}

        self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        self.assertEqual(self.read('SELECT "action" FROM interventions'),
                         [("approve",)])

    def test_a_github_error_leaves_the_parked_run_for_the_next_pass(self):
        """Review of KO-359: GitHub could not be read at startup while the
        board already said Done. The mirror reconcile used to take that
        Done and walk the ticket `merged` around its parked run, and no
        later pass asked GitHub about a ticket no longer blocked: the run
        was stranded with no outcome and no `mergeSha`. Now the ticket
        stays parked with its run through the failure, and the pass after
        GitHub recovers ships it."""
        self.parked_on_pr()
        asked = self.fake_client(RuntimeError("GitHub is down"),
                                 self.MERGED_PULL)
        provider = StubProvider()
        provider.closed = {"KO-131": "completed"}

        out = self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertIn("could not be read (GitHub is down); the run stays"
                      " parked", out)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("awaiting_merge_approval", None, None)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

        again = StubProvider()
        again.closed = {"KO-131": "completed"}
        self.main_output(provider=again)

        self.assertEqual(len(asked), 2)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])
        self.assertEqual(again.states, [("iss-131", "Done")])
        self.assertEqual(self.read('SELECT "action" FROM interventions'),
                         [("approve",)])

    def test_a_pull_request_closed_without_merge_keeps_the_run_parked(self):
        """The pull request was closed on GitHub unmerged: the run stays
        parked, the ticket's question says so, the skip line reads the
        question rather than an `--approve` that would merge nothing, and
        a second pass finding the same neither writes nor prints again."""
        self.parked_on_pr()
        self.fake_client(self.CLOSED_PULL)

        out = self.main_output(provider=StubProvider(
            dict(a_task(), body=self.BODY)))
        again = self.main_output(provider=StubProvider())

        self.assertEqual(
            self.read("SELECT phase, outcome, prUrl FROM runs"),
            [("awaiting_merge_approval", None, self.URL)])
        self.assertEqual(self.question(),
                         f"PR closed without merge: {self.URL}")
        self.assertIn(f"[holo2] KO-131 is parked on a question: PR closed"
                      f" without merge: {self.URL}; skipping it\n", out)
        self.assertNotIn("--approve", out)
        self.assertIn("closed on GitHub without merging", out)
        self.assertNotIn("closed on GitHub", again)
        self.assertEqual(self.last_provider.states, [])

    def test_an_open_pull_request_is_asked_about_once_and_left_alone(self):
        self.parked_on_pr()
        runs = self.read("SELECT * FROM runs")
        tickets = self.read("SELECT * FROM tickets")
        asked = self.fake_client(self.OPEN_PULL)
        provider = StubProvider()

        self.main_output(provider=provider)

        self.assertEqual(len(asked), 1)
        self.assertEqual(self.read("SELECT * FROM runs"), runs)
        self.assertEqual(self.read("SELECT * FROM tickets"), tickets)
        self.assertEqual(provider.states, [])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    # The open pull request as it reads with review activity on it (KO-362):
    # `updatedAt` and the thread count are what the reconcile holds against
    # the run's mark.
    T1, T2, T3 = ("2026-09-10T10:00:00Z", "2026-09-10T11:00:00Z",
                  "2026-09-10T11:00:30Z")

    def open_pull(self, at, threads, checks=None, review=None):
        """The open pull request read at `at` with `threads` review
        threads; `checks` is the head's `statusCheckRollup.state` and
        `review` GitHub's `reviewDecision`, both absent when None (a PR
        with no checks, a repository requiring no review)."""
        pull = dict(self.OPEN_PULL, updatedAt=at,
                    reviewThreads={"totalCount": threads})
        if checks is not None:
            pull["commits"] = {"nodes": [
                {"commit": {"statusCheckRollup": {"state": checks}}}]}
        if review is not None:
            pull["reviewDecision"] = review
        return pull

    def parked_with_mark(self, at, threads):
        """A run parked on its pull request whose park recorded `at` and
        `threads` as what it saw, parked long enough ago for
        `[merge] pr_poll_sec` to have passed."""
        self.parked_on_pr()
        self.assertEqual(self.read("SELECT prSeenAt, prSeenThreads FROM"
                                   " runs"), [(None, None)])
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenAt = ?, prSeenThreads = ?,"
                         " lastHeartbeat = lastHeartbeat - 200000",
                         (at, threads))
        conn.close()

    def test_new_review_activity_sends_the_parked_run_to_the_babysit(
            self):
        """KO-362: the pull request's `updatedAt` moved past what the last
        pass recorded. The tick sends the run back to the babysitter as
        `--babysit` would -- a `babysit` intervention, the run ended
        with the ticket ready -- and the same pass claims it: the resumed
        run babysits the pull request and parks again, its park recording
        what it saw *after* its own writes (the third answer), so the tick
        after that, reading the same, sends nothing."""
        self.parked_with_mark(self.T1, 0)
        asked = self.fake_client(
            self.open_pull(self.T2, 1, checks="PENDING",
                           review="CHANGES_REQUESTED"),
            self.open_pull(self.T3, 1, checks="SUCCESS", review="APPROVED"))

        out = self.main_output(provider=self.provider())

        self.assertIn(f"KO-131: {self.URL} has new review activity (updated"
                      f" {self.T2}, 1 review threads); run 1 sent back to"
                      " the babysitter", out)
        # Each write records the checks rollup and review decision the
        # same read saw beside the mark (KO-368).
        self.assertEqual(
            self.read("SELECT id, phase, outcome, prSeenAt, prSeenThreads,"
                      " prSeenChecks, prSeenReview FROM runs ORDER BY id"),
            [(1, "failed", "abandoned", self.T2, 1, "pending",
              "changes_requested"),
             (2, "awaiting_merge_approval", None, self.T3, 1, "success",
              "approved")])
        self.assertEqual(
            self.read('SELECT "action", source FROM interventions'),
            [("babysit", "supervisor")])
        self.assertEqual(
            self.read("SELECT summary FROM runEvents"
                      " WHERE kind = 'intervention'"),
            [(f"supervisor babysit: new review activity on {self.URL}:"
              f" updated {self.T2} (last seen {self.T1}), 1 review threads"
              " (last seen 0)",)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("blocked_on_operator",)])
        # Three reads so far: the tick's, the park's after its writes,
        # and the pass after the park's, which saw the same and sent
        # nothing.
        self.assertEqual(len(asked), 3)

        again = self.main_output(provider=StubProvider())

        self.assertEqual(len(asked), 4)
        self.assertNotIn("new review activity", again)
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(1,)])
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"
                                   " WHERE id = 2"),
                         [("awaiting_merge_approval", self.T3)])

    def test_an_unchanged_pull_request_is_not_babysat_again(self):
        """Two ticks over the same pull request: the first finds no mark
        on the run (parked by a module older than the columns) and records
        what it saw without babysitting; the second finds the same and
        does nothing. No round, no intervention, the run still parked."""
        self.parked_on_pr()
        asked = self.fake_client(self.open_pull(self.T1, 0, checks="SUCCESS"))

        first = self.main_output(provider=StubProvider())
        second = self.main_output(provider=StubProvider())

        self.assertEqual(len(asked), 2)
        self.assertNotIn("review activity", first + second)
        # No `reviewDecision` in the answer is a null review, not an error.
        self.assertEqual(
            self.read("SELECT phase, outcome, prSeenAt, prSeenThreads,"
                      " prSeenChecks, prSeenReview FROM runs"),
            [("awaiting_merge_approval", None, self.T1, 0, "success", None)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    def test_an_unchanged_pull_request_still_refreshes_its_facts(self):
        """KO-368 review round 1: the run holds a mark from the last pass
        and facts from the same read (checks pending, review required).
        The pull request has not moved -- same `updatedAt`, same thread
        count -- but its checks went green and a review landed. The tick
        starts no round and leaves the mark alone, yet the facts on the
        run are what the read saw, not what the last pass saw."""
        self.parked_with_mark(self.T1, 0)
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenChecks = 'pending',"
                         " prSeenReview = 'review_required'")
        conn.close()
        self.fake_client(self.open_pull(self.T1, 0, checks="SUCCESS",
                                        review="APPROVED"))

        out = self.main_output(provider=StubProvider())

        self.assertNotIn("review activity", out)
        self.assertEqual(
            self.read("SELECT phase, prSeenAt, prSeenThreads, prSeenChecks,"
                      " prSeenReview FROM runs"),
            [("awaiting_merge_approval", self.T1, 0, "success",
              "approved")])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    def test_activity_within_the_poll_interval_waits(self):
        """The pull request moved, but the run parked seconds ago: the tick
        names the activity and the wait rather than starting a round."""
        self.parked_on_pr()
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE runs SET prSeenAt = ?, prSeenThreads = 0",
                         (self.T1,))
        conn.close()
        self.fake_client(self.open_pull(self.T2, 1))

        out = self.main_output(provider=StubProvider())

        self.assertIn(f"KO-131: {self.URL} has new review activity; the next"
                      " babysit round waits", out)
        self.assertIn("([merge] pr_poll_sec)", out)
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"),
                         [("awaiting_merge_approval", self.T1)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    def test_a_low_github_budget_stops_the_pull_request_reads(self):
        """The read that finds `rateLimit.remaining` under the floor is the
        last one until the reset it names: the activity it saw starts no
        round, the tick prints one line naming `resetAt`, and the next tick
        reads no pull request at all."""
        self.parked_with_mark(self.T1, 0)
        reset = "2999-01-01T00:00:00Z"
        asked = self.fake_client(self.open_pull(self.T2, 1),
                                 rate={"remaining": 200, "resetAt": reset})

        first = self.main_output(provider=self.provider())
        second = self.main_output(provider=self.provider())

        self.assertEqual(len(asked), 1)
        line = ("[holo2] GitHub's GraphQL budget is down to 200 points; no"
                f" parked pull request is read until it resets at {reset}")
        self.assertIn(line, first)
        self.assertIn(line, second)
        self.assertNotIn("sent back to the babysitter", first + second)
        self.assertEqual(self.read("SELECT phase, prSeenAt FROM runs"),
                         [("awaiting_merge_approval", self.T1)])
        self.assertEqual(self.read("SELECT COUNT(*) FROM interventions"),
                         [(0,)])

    def test_the_scheduler_ships_a_merged_pull_request_on_a_timer_tick(
            self):
        """The pool's timer tick asks too: open at startup, merged by the
        tick, and the parked run is closed out between two waits with no
        worker involved (KO-353's tick carrying KO-359's reconcile)."""
        self.parked_on_pr()
        asked = self.fake_client(self.OPEN_PULL, self.MERGED_PULL)
        provider = StubProvider(a_task(2))
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
                       '[loop]\nworkers = 2\ntick_sec = 30\n')
        pool = FakePool([(TICK, provider.queue.clear),
                         (holophyte.pool.WORKER_MERGED, None)])
        out = io.StringIO()
        with patch.object(holophyte.pool, "SPAWN", pool.spawn), \
                patch.object(holophyte.pool, "WAIT", pool.wait), \
                patch.object(sys, "stdout", out):
            rc = holophyte.operator.main(self.tgt, provider)

        self.assertIsNone(rc)
        self.assertEqual(len(asked), 2)
        self.assertEqual(pool.timeouts, [30, 30])
        self.assertEqual(len(pool.spawned), 1)
        self.assertEqual(
            self.read("SELECT phase, outcome, mergeSha FROM runs"),
            [("done", "merged", self.MERGE_SHA)])
        self.assertEqual(
            self.read("SELECT status FROM tickets"
                      " WHERE linearIdentifier = 'KO-131'"),
            [("merged",)])
        self.assertIn(("iss-131", "Done"), provider.states)
        self.assertIn(f"{self.URL} was merged on GitHub", out.getvalue())

    def test_a_refused_push_is_an_infra_failure_with_no_pull_request(self):
        """The remote said no: the run ends as an infra failure naming the
        push, the branch and worktree are preserved, `gh` was never called
        and nothing is recorded as a PR."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(push_exit=1)

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.recorded(), [f"git push origin {BRANCH}"])
        self.assertFalse(self.pr_body.exists())
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertIn(BRANCH, self.branches())
        self.assertTrue((self.worktrees / "ko-131-add-a-thing").exists())
        rows = self.read("SELECT phase, outcome, outcomeClass, outcomeReason,"
                         " prUrl FROM runs")
        (phase, outcome, klass, reason, url), = rows
        self.assertEqual((phase, outcome, klass, url),
                         ("failed", "failed", "infra", None))
        self.assertIn(f"git push origin {BRANCH} failed", reason)
        self.assertIn("refused", reason)

    def test_local_merges_as_today_and_pushes_nothing(self):
        self.configure('[merge]\nmode = "local"\n')
        self.fake_route()

        self.loop(Commit("the scripted work"), APPROVE)

        self.assertEqual(self.recorded(), [])
        self.assertIn("the scripted work", self.subjects())
        self.assertNotIn(BRANCH, self.branches())
        self.assertEqual(self.read("SELECT outcome, prUrl FROM runs"),
                         [("merged", None)])


if __name__ == "__main__":
    unittest.main()

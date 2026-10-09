"""`holophyte.pr.pullrequest` under `[merge] mode = "pr"`, driven end to end.

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
    MergeModeFixture,
    StubProvider,
    a_task,
)

import holophyte.cli.operator  # noqa: E402 - after the sys.path insert above
import holophyte.loop.gates  # noqa: E402 - after the sys.path insert above
import holophyte.pr.github  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pr_status  # noqa: E402 - after the sys.path insert above
import holophyte.pr.pullrequest  # noqa: E402 - after the sys.path insert above
from holophyte.loop import implement, pipeline  # noqa: E402


class MergeModePullRequestTests(MergeModeFixture):
    """The `[merge] mode = "pr"` tests that open, park, resume and merge
    the pull request; the passes over threads and checks are
    `MergeModeBabysitPassTests` (`test_babysit_pass.py`)."""

    def test_resumed_human_candidate_keeps_current_description(self):
        from test_babysit_threads import MergeModeBabysitThreadsTests as H
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(states=[self.pr_state([H.DEFECT]), self.pr_state()])
        self.loop(Commit("candidate"), APPROVE, Idle(""),
                  Reply("THREAD 1: ADDRESS -- a real crash"), Commit("fix"),
                  Idle("TITLE: Fixed load\nLoad handles missing input.\n\n"
                       "## Changes since first review\n"
                       "- Missing input no longer crashes the load."),
                  provider=self.provider())
        body = self.pr_body.read_text()
        self.assertIn("Load handles missing input.", body)
        self.assertNotIn("Changes since first review", body)
        self.serve(self.pr_state())
        edits = [c for c in self.recorded() if c.startswith("gh pr edit")]
        self.assertEqual(len(edits), 1)
        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", "sent back to the babysitter", out=io.StringIO())
        fake, _ = self.loop(provider=self.provider())
        self.assertEqual(fake.roles, [])
        self.assertEqual(self.pr_body.read_text(), body)
        self.assertEqual([c for c in self.recorded() if c.startswith("gh pr edit")],
                         edits)
        self.assertEqual(self.read("SELECT phase FROM runs ORDER BY id")[-1],
                         ("awaiting_merge_approval",))

    def test_pr_pushes_opens_the_pull_request_and_parks_the_run(self):
        """Push, then create with the ticket title and Summary stub.
        With green checks and `approve = "human"`, park ready to merge: the
        URL `gh` printed is the run's `prUrl` and heads the ticket's
        question; main is untouched, the branch and worktree stay, and the
        run is parked alive in `awaiting_merge_approval` with its lease
        released -- the `approve = "human"` park, with a URL."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        provider = self.provider()
        fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                            provider=provider)
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        calls = self.recorded()
        # The eighth is the park reading the pull request once more, after
        # the pass's own writes, for the activity mark it records (KO-362);
        # the ninth is the pass after the park asking GitHub whether the
        # parked pull request has been merged (KO-359).
        self.assertEqual(len(calls), 9, calls)
        self.assertEqual(calls[7:], ["gh api --hostname github.com --method"
                                     " POST graphql --input -"] * 2)
        self.assertEqual(calls[0], f"git push origin {BRANCH}")
        # Between the push and the create, the open step's lookup of an
        # open pull request on the branch (KO-407) -- answered none here.
        self.assertEqual(calls[1], "gh api --hostname github.com --method"
                                   " POST graphql --input -")
        # Beside the state query: the head's check runs, main's rules and
        # main's protection (KO-652), so a rollup that says success before
        # the checks have reported is not read as green.
        tip = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(calls[4:7], [
            "gh api --hostname github.com --method GET"
            f" repos/example/repo/commits/{tip}/check-runs?per_page=100",
            "gh api --hostname github.com --method GET"
            " repos/example/repo/rules/branches/main",
            "gh api --hostname github.com --method GET"
            " repos/example/repo/branches/main"])
        # Pin the repository to the push destination, not gh's default.
        self.assertEqual(
            calls[2],
            f"gh pr create --repo {self.ORIGIN} --base main --head {BRANCH}"
            " --title KO-131: add a thing --body-file -")
        self.assertEqual([kind for kind, _ in self.api_calls()], ["state"])
        body = self.pr_body.read_text()
        self.assertIn("The thing, added.", body)
        self.assertIn("could not be written", body)
        self.assertIn("Linear: KO-131", body)
        self.assertNotIn("## Acceptance criteria", body)
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

    def test_a_fresh_open_records_its_url_and_event_before_parking(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        observed = []
        def open_and_observe(*args, **kwargs):
            url = holophyte.pr.pullrequest._push_and_open(*args, **kwargs)
            observed.append((url, self.read(
                "SELECT phase, endedAt, prUrl FROM runs"), self.read(
                "SELECT summary FROM runEvents WHERE kind = 'pull_request'"
                " ORDER BY seq")))
            return url
        with patch.object(pipeline, "_push_and_open", open_and_observe):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=self.provider())
        (url, rows, events), = observed
        self.assertEqual(url, self.URL)
        self.assertEqual(rows, [("merge_gate", None, url)])
        self.assertEqual(events[-1], (f"pull request open: {url}",))

    def lock_witness(self):
        """A log, and the shell line that appends whether the merge lock
        file exists at the moment it runs, for the verify and the push."""
        log = self.db.parent / "lock.log"
        path = holophyte.loop.gates.merge_lock_path(self.project)
        return log, lambda who: (
            f"if [ -e {shlex.quote(str(path))} ]; then echo {who} locked;"
            f" else echo {who} free; fi >> {shlex.quote(str(log))}")

    def test_the_gate_verifies_unlocked_and_pushes_under_the_lock(self):
        """KO-644: the pre-merge verify shares nothing another run's gate
        touches, so it runs with no lock held; the push, which moves refs
        in the target checkout, runs under it."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        log, witness = self.lock_witness()
        self.fake_route(push_sh=f"  {witness('push')}")
        gate = pipeline._merge_gate

        def reexeced(*args, **kwargs):
            # A re-exec'd run cites no pass (KO-646), so the gate's runs.
            holophyte.loop.gates._PASSES.clear()
            return gate(*args, **kwargs)

        with patch.object(pipeline, "_merge_gate", reexeced):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=StubProvider(dict(a_task(), body=self.BODY,
                                                 verify=witness("verify"))))
        # The review round's verify, then the gate's, then the push.
        self.assertEqual(log.read_text().splitlines(),
                         ["verify free", "verify free", "push locked"])
        self.assertEqual(self.read("SELECT prUrl FROM runs"), [(self.URL,)])

    def test_a_held_lock_parks_after_the_verify_and_before_the_push(self):
        """A lock another run holds past the wait still parks the run on
        it, now with the gate's verify run and passed -- a failed one
        parks on its own question -- and nothing pushed."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        log, witness = self.lock_witness()
        self.fake_route(push_sh=f"  {witness('push')}")
        gate = pipeline._merge_gate

        def taken_by_run_7(*args, **kwargs):
            # Run 7 takes the lock as this run enters the gate; the claim's
            # own fetch, under the same lock, is long done.
            holophyte.loop.gates.merge_lock_path(self.project).write_text("7 0\n")
            holophyte.loop.gates._PASSES.clear()  # as after a re-exec (KO-646)
            return gate(*args, **kwargs)

        with (patch.object(holophyte.loop.gates, "MERGE_LOCK_WAIT_SEC", 0),
              patch.object(pipeline, "_merge_gate", taken_by_run_7)):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                      provider=StubProvider(dict(a_task(), body=self.BODY,
                                                 verify=witness("verify"))))
        # The review round's verify, then the gate's with run 7's lock
        # held; no push.
        self.assertEqual(log.read_text().splitlines(),
                         ["verify free", "verify locked"])
        self.assertFalse(any(c.startswith("git push") for c in self.recorded()))
        self.assertEqual(self.read("SELECT parkKind FROM runs"),
                         [("merge_lock",)])
        self.assertTrue(self.question().startswith("merge lock: merge lock"))

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
        with patch.object(holophyte.pr.github, "SLEEP") as sleep:
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                provider=provider)
        sleep.assert_not_called()
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
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
        self.assertEqual(self.read(
            "SELECT summary FROM runEvents WHERE summary LIKE"
            " 'pull request head settled%'"), [])
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

    def test_adopted_head_settles_before_waiting_for_checks(self):
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route(open_pr=self.URL, states=[
            self.pr_state(head=self.base),
            self.pr_state(checks="PENDING"), self.pr_state()])
        observed = []

        def sleep(seconds):
            observed.append((seconds, self.read("SELECT phase FROM runs")))

        with patch.object(holophyte.pr.github, "SLEEP", sleep):
            self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                provider=self.provider())

        sha = self.git("rev-parse", BRANCH).strip()
        self.assertEqual(self.read(
            "SELECT summary FROM runEvents WHERE summary LIKE"
            " 'pull request head settled%'"),
            [(f"pull request head settled to {sha} after 2 reads",)])
        self.assertEqual(observed, [(5, [("merge_gate",)])])
        self.assertEqual(self.read("SELECT parkKind FROM runs"), [("ci",)])
        self.assertIn("pending checks", self.question())

    def test_adopted_stale_head_continues_after_bounded_reads(self):
        self.configure('[merge]\nmode = "pr"\napprove = "auto"\n')
        self.fake_route(open_pr=self.URL, states=[self.pr_state(head=self.base)])
        with patch.object(holophyte.pr.github, "SLEEP") as sleep:
            fake, _ = self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                                provider=self.provider())

        sha = self.pushed()[-1][1]
        self.assertEqual([call.args for call in sleep.call_args_list],
                         [(5,), (5,), (5,)])
        self.assertEqual(fake.roles, ["implement", "review", "implement"])
        self.assertEqual(self.read("SELECT outcome FROM runs"), [("merged",)])
        self.assertEqual([v["sha"] for kind, v in self.api_calls() if kind == "merge"],
                         [sha])

    def adopt(self, evidence):
        """Push and adopt the branch's open pull request at `URL`, whose
        body reads `evidence`, with a prepared text carrying newer Evidence;
        answer the pull request's body afterwards."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(open_pr=self.URL)
        self.git("branch", BRANCH)
        link = "Linear: KO-131 (https://linear.app/example/KO-131)\n"
        self.pr_body.write_text(f"Earlier prose.\n\n{evidence}\n\n{link}")
        prepared = ("Newer prose.\n\n## Evidence\n\nCaptured at 2500d82ac13c\n\n"
                    "![one](https://example/one.png)\n\n"
                    "![two](https://example/two.png)\n\n" + link)
        url = holophyte.pr.pullrequest._push_and_open(
            self.project, None, None, BRANCH, "title", prepared, 60)
        self.assertEqual(url, self.URL)
        self.assertFalse(any(c.startswith("gh pr create")
                             for c in self.recorded()))
        return self.pr_body.read_text()

    def test_adopting_an_open_pull_request_replaces_only_its_evidence(self):
        body = self.adopt("## Evidence\n\nCaptured at 657e8529c357\n\n"
                          "![one](https://example/old.png)")
        self.assertEqual(
            body, "Earlier prose.\n\n## Evidence\n\nCaptured at 2500d82ac13c\n\n"
            "![one](https://example/one.png)\n\n"
            "![two](https://example/two.png)\n\n"
            "Linear: KO-131 (https://linear.app/example/KO-131)\n")

    def test_adopting_a_pull_request_with_the_same_evidence_edits_nothing(self):
        self.adopt("## Evidence\n\nCaptured at 2500d82ac13c\n\n"
                   "![one](https://example/one.png)\n\n"
                   "![two](https://example/two.png)")
        self.assertFalse(any(c.startswith("gh pr edit")
                             for c in self.recorded()))

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

        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
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

    AGENTS_MD = ("# Agent guide\n\nTitle starts with [Feature Name]."
                 " No testing plan.\n")
    WRITTEN = Idle("Reading the diff.\n"
                   "TITLE: [Contacts] Put Contact Name first\n\n"
                   "The two forms now ask for the contact's name before"
                   " anything else.\n\nThe thing file is what changed.\n")

    def written_target(self):
        """A style line, and an `AGENTS.md` on
        main for the worktree to carry."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n'
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
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
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

    def test_writer_commands_and_prompts(self):
        from holophyte.agents import roles

        calls = self.db.parent / "writer-calls.jsonl"
        command = self.db.parent / "fake-writer"
        command.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"with open({str(calls)!r}, 'a') as out:\n"
            " out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "print('TITLE: Faster search\\nSearch results arrive sooner.'"
            " + ('\\n\\n## Changes since first review\\n- Search no longer stalls.'"
            " if 'what this fix answered' in sys.argv[-1] else ''))\n")
        command.chmod(0o755)
        pull = holophyte.pr.pr_status.parse_pr_url(self.URL)
        for writer in (True, False):
            with self.subTest(writer=writer):
                config = f'[agents]\nimplementer = "{command} implementer"\n'
                if writer:
                    config += f'writer = "{command} writer"\n'
                self.configure(config + '[merge]\npr_style = "Use plain prose."\n')
                with patch.object(implement, "agent", roles.agent):
                    title, body = holophyte.pr.pullrequest._written_pr_text(
                        self.project, None, None, "KO-131", "add a thing", BRANCH,
                        self.BODY, 60, self.target, monotonic(), 5, None)
                    with patch.object(holophyte.pr.github, "rest",
                                      return_value={"body": body}), \
                            patch.object(holophyte.pr.github, "edit_pr_body") as edit:
                        holophyte.pr.pullrequest.refresh_pr_text(
                            self.project, None, None, "KO-131", "add a thing", BRANCH,
                            self.BODY, 60, self.target, 5, pull,
                            "ADDRESS: replace correlated subquery")
                self.assertEqual(title, "Faster search")
                self.assertIn("Search results arrive sooner.",
                              edit.call_args.args[2])
                self.assertNotIn("correlated subquery", edit.call_args.args[2])
                turns = [json.loads(line)
                         for line in calls.read_text().splitlines()][-2:]
                self.assertEqual([turn[0] for turn in turns],
                                 ["writer" if writer else "implementer"] * 2)
                for _, prompt in turns:
                    self.assertIn("what the change does for a user or caller and why",
                                  prompt)
                    prohibition = ("Do not list files, styles, class names, "
                                   "renames or tests.")
                    self.assertIn(prohibition, prompt)
                    self.assertLess(prompt.index(prohibition),
                                    prompt.index("Use plain prose."))
    def test_writer_unusable_reply_and_timeout_use_stub(self):
        from holophyte.agents import roles

        command = self.db.parent / "refused-writer"
        self.configure(f'[agents]\nwriter = "{command}"\nimplementer="false"\n')
        real_run = roles.run_capped
        for timeout in (False, True):
            command.write_text(f"#!{sys.executable}\nimport time\n"
                               + ("time.sleep(60)\n" if timeout else
                                  "print('Unusable reply')\n"))
            command.chmod(0o755)
            def bounded(cmd, cwd, cap, **kwargs):
                return real_run(cmd, cwd, 0.1, **kwargs)
            with self.subTest(timeout=timeout), patch.object(
                    roles, "run_capped", side_effect=bounded), patch(
                    "sys.stdout", new_callable=io.StringIO) as out:
                result = holophyte.pr.pullrequest._written_pr_text(
                    self.project, None, None, "KO-131", "add a thing", BRANCH,
                    self.BODY, 60, self.target, monotonic(), 5, None)
                self.assertIn("could not be written", result[1])
                self.assertEqual(out.getvalue().count("written PR text refused"), 1)
                self.assertIn("ran out of time" if timeout else "no `TITLE:` line",
                              out.getvalue())

    def test_a_written_title_citing_another_ticket_takes_the_runs_key(self):
        turn = FakeAgent(Idle("TITLE: Fix the timeout (KO-9)\n\n"
                              "Requests no longer hang.\n"))
        with patch.object(implement, "agent", turn):
            title, _ = holophyte.pr.pullrequest._written_pr_text(
                self.project, None, None, "HOLO-9", "fix the timeout", BRANCH,
                self.BODY, 60, self.target, monotonic(), 5, None)
        self.assertEqual(title, "Fix the timeout (HOLO-9)")

    def test_a_written_pr_takes_the_turns_title_and_body(self):
        """After the approval one more implementer
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

    def test_a_reply_without_a_title_falls_back_to_the_stub(self):
        """No `TITLE:` line: the PR is still opened, titled `KO-n: TITLE`
        with a Summary stub and the Linear link, and one printed line
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
        self.assertEqual(body.splitlines()[0], "The thing, added.")
        self.assertIn("could not be written", body)
        self.assertIn("Linear: KO-131 (https://linear.app/example/issue/KO-131/", body)
        self.assertNotIn("## Acceptance criteria", body)
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

    # The frozen prompt for this scenario, updated in KO-561 for the
    # behavior-first writing instruction: criterion 2's
    # oracle. Comparing the live prompt against the same run's own output
    # would let a drift in the shared text move both sides together.
    RECORDED = (ROOT / "tests" / "fixtures" / "pullrequest"
                / "written_prompt_base.txt")

    def test_the_written_turn_fills_the_repositorys_pr_template(self):
        """KO-430: a worktree carrying `.github/pull_request_template.md`
        gives the written turn the file under a "fill its sections"
        heading, ahead of the style line; a worktree without one gets
        the frozen fixture's prompt byte for byte, witnessed against the
        recording."""
        provider = self.written_target()
        fake, _ = self.loop(Commit("the scripted work"), APPROVE,
                            self.WRITTEN, provider=provider)
        # A worktree with no template file: the recorded fixture prompt,
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
        with patch.object(implement, "agent", turn):
            holophyte.pr.pullrequest._written_pr_text(
                self.project, None, None, "KO-131", "add a thing", BRANCH,
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

    def park_on_a_declined_nit_with_a_bare_origin(self):
        """A run parked on a declined nit at its approved sha, and the
        target's `origin` then pointed at a bare repository holding the
        candidate branch -- the remote a person pushes on top of. Returns
        `(approved sha, bare path, scratch clone path)`; the clone is where
        a test makes the person's commits, and `publish()` moves them to
        the bare branch (`force` for a rewritten history)."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(states=[self.pr_state([self.NIT]), self.pr_state()])
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
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
        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", "sent back to the babysitter", out=io.StringIO())
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

        fake, _ = self.loop(APPROVE, self.WRITTEN, provider=self.provider())

        # The merged run removes the worktree and branch at close-out, so
        # the fast-forward is witnessed by what the review was handed and
        # the sha the merge recorded, not by a branch that no longer exists.
        self.assertEqual(fake.roles, ["review", "implement"])
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
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        approved = self.git("rev-parse", BRANCH).strip()
        self.serve(self.pr_state(head=approved, review="APPROVED"))
        holophyte.cli.operator.approve(self.project, "KO-131", "looks fine",
                               out=io.StringIO())
        self.calls.unlink()
        for path in self.api_dir.iterdir():
            path.unlink()

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
             (2, "done", "merged", None, self.URL, approved, self.MERGE_SHA)])
        self.assertEqual(self.read("SELECT status FROM tickets"),
                         [("merged",)])

    def test_a_babysitter_release_parks_again_rather_than_merging(self):
        """`--babysit KO-n` is "look again", not "merge": the resumed run
        babysits the PR and, green and quiet under `approve = "human"`,
        parks again on the same URL."""
        self.configure('[merge]\nmode = "pr"\napprove = "human"\n')
        self.fake_route()
        self.loop(Commit("the scripted work"), APPROVE, Idle(""),
                  provider=self.provider())
        holophyte.cli.operator.babysit_ticket(
            self.project, "KO-131", "sent back to the babysitter", out=io.StringIO())

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
            self.read('SELECT "action" FROM interventions'
                      " WHERE action != 'migrate'"), [("babysit",)])
    def test_a_refused_push_is_an_infra_failure_with_no_pull_request(self):
        """The remote said no: the run ends as an infra failure naming the
        push, the branch and worktree are preserved, `gh` was never called
        and nothing is recorded as a PR."""
        self.configure('[merge]\nmode = "pr"\n')
        self.fake_route(push_exit=1)
        self.loop(Commit("the scripted work"), APPROVE, Idle(""))
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

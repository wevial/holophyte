from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
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
    Commit,
    FakeAgent,
    Idle,
    block_until_killed,
    no_agent_processes,
)

import holophyte.gates  # noqa: E402 - after the sys.path insert above
import holophyte.loop  # noqa: E402 - after the sys.path insert above
import holophyte.operator  # noqa: E402 - after the sys.path insert above
import holophyte.project  # noqa: E402 - after the sys.path insert above
import holophyte.reconcile  # noqa: E402 - after the sys.path insert above

# The branch the loop cuts for the task below. Spelled out rather than derived
# from `factory`'s slug rule: an expectation computed by the code under test
# is not an expectation.
BRANCH = "task/ko-131-add-a-thing"


class StubProvider:
    """The provider seam `main()` drives, queueing tasks it hands out in order."""

    TEAM = "team-under-test"
    team = TEAM  # the `Provider` protocol's spelling

    def __init__(self, *tasks):
        self.queue = list(tasks)
        # The ids `claim_next()` has handed out: refused (`skip`) tasks
        # stay on the board; the mirror reconcile (KO-425) reads this.
        self.offered = set()
        self.last_listing = None
        # What `fetch_task()` hands back, kept apart from the queue so a
        # test can leave the live ticket saying something other than the
        # one claimed — the mid-run edit the merge gate exists to catch.
        self.live = {task["issue_id"]: task for task in tasks}
        self.states = []
        self.comments = []
        # The board lease label (KO-351): the labels each issue carries
        # now, seeded from the task's `labels`; the label calls in order;
        # the read-backs.
        self.labels = {task["issue_id"]: list(task.get("labels") or [])
                       for task in tasks}
        self.label_calls = []
        self.read_calls = []

    def claim_next(self, skip=(), order="identifier"):
        """The first queued task the loop has not already refused.

        `skip` is honored rather than ignored because the real provider hands
        back the *same* head-of-queue ticket on every ask; a stub that popped
        blindly would let a loop that cannot skip look like one that can.

        `last_listing` is the ready listing as this ask saw it: the queue
        plus the offered tasks `skip` marks refused, which the pop removes.
        """
        self.last_listing = sorted(
            {task["id"] for task in self.queue} | (self.offered & set(skip)))
        for i, task in enumerate(self.queue):
            if task["id"] not in skip:
                self.offered.add(task["id"])
                return self.queue.pop(i)
        return None

    def ready_issues(self):
        """Every task the board would offer `claim_next()`: the queue as it
        stands, so the loop's queue mirror sees what the claim sees."""
        return [dict(task) for task in self.queue]

    def fetch_task(self, issue_id):
        """The ticket as the board holds it now; None when there is no such issue."""
        task = self.live.get(issue_id)
        return dict(task) if task else None

    def set_state(self, issue_id, state):
        self.states.append((issue_id, state))

    def comment(self, task_id, body):
        self.comments.append((task_id, body))

    def label_issue(self, issue_id, name):
        self.label_calls.append(("label", issue_id, name))
        have = self.labels.setdefault(issue_id, [])
        if name not in have:
            have.append(name)

    def issue_labels(self, issue_id):
        self.read_calls.append(issue_id)
        return list(self.labels.setdefault(issue_id, []))

    def unlabel_issue(self, issue_id, name):
        self.label_calls.append(("unlabel", issue_id, name))
        have = self.labels.setdefault(issue_id, [])
        self.labels[issue_id] = [n for n in have if n != name]

    # What the board says it has closed, identifier -> state type, when the
    # startup reconcile asks; empty means the mirror is current.
    closed = {}

    def closed_identifiers(self, identifiers):
        self.asked = list(identifiers)
        return {i: self.closed[i] for i in identifiers if i in self.closed}


def a_task(n=1):
    """One ticket in the shape `linear_provider.parse_task()` returns."""
    return {"id": f"KO-13{n}", "issue_id": f"iss-13{n}", "title": "add a thing",
            "verify": "echo ok", "budget_min": 5, "contracts": [],
            "criteria": ["Given the thing, when it runs, then it works"]}


# A body `ticket_template.validate()` accepts, in the shape the Linear
# provider hands over; the tests that route on the body use it.
VALID_BODY = (
    "# Add a thing\n\n## Summary\n\nThe thing, added.\n\n"
    "## What / Why / How\n\n**What:** Add the thing.\n\n"
    "**Why:** The thing is wanted.\n\n**How:** Write the thing.\n\n"
    "## In scope\n\n* The thing.\n\n## Out of scope\n\n"
    "* Everything else.\n\n## Acceptance criteria\n\n"
    "- [ ] Given the thing, when it runs, then it works (a test witnesses"
    " this)\n\n## Verify command(s)\n\n```\necho ok\n```\n\n"
    "## Implementation notes\n\n* None.\n\n"
    "## Estimate & dependencies\n\nEstimate: 5 min · Depends on: none\n\n"
    "## Open questions\n\n* None\n")


# The same body with the template's own Summary placeholder left in, as
# KO-165 was claimed: criteria and a verify command present, so every
# store-side gate says `ready`, and only the validator objects.
INVALID_BODY = VALID_BODY.replace(
    "The thing, added.", "<Describe the outcome in one or two sentences.>")


class LoopFixture(unittest.TestCase):
    """The real repo, worktree directory and store every loop test runs on;
    split from the tests so a suite with its own configuration reuses the
    fixture."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # The GitHub budget the reconcile remembers is the process's; a
        # test that ran it low must not back off the tests after it.
        budget = patch.object(holophyte.reconcile, "GITHUB_BUDGET",
                              holophyte.reconcile.GitHubBudget())
        budget.start()
        self.addCleanup(budget.stop)
        root = Path(tmp.name)
        self.target = root / "repo"
        self.worktrees = root / "repo.worktrees"
        self.target.mkdir()
        # Self-reexec tests run against the disposable factory checkout.
        factory_file = patch("holophyte.pool_handoff.factory_checkout",
                             return_value=self.target)
        factory_file.start()
        self.addCleanup(factory_file.stop)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "factory@example.invalid")
        self.git("config", "user.name", "Factory Test")
        (self.target / "README.md").write_text("base\n")
        # The test the scripted approvals name as their witness: since KO-215
        # the loop checks a named test exists in the worktree.
        (self.target / "tests").mkdir()
        (self.target / "tests" / "test_thing.py").write_text(
            "def test_it_works():\n    pass\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base = self.git("rev-parse", "main").strip()

        # Where `Project.locate(self.target)` will look: the target's directory
        # under a HOLOPHYTE_HOME of this test's own, never the operator's real
        # one.
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.db = holophyte.project.state_dir(self.target) / "store.db"
        from tests.test_store_phase_gate import audit_loop_store
        self.addCleanup(audit_loop_store, self)
        self.db.parent.mkdir(parents=True)
        self.project = holophyte.project.Project.locate(self.target)
        assert self.project.store_path == self.db
        assert self.project.worktrees == self.worktrees

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def configure(self, toml):
        """Give the fixture target a config file and a `Project` that reads it.

        Through `Project.locate()` rather than a hand-set `config_path`, so a
        test that set the config by hand could pass with the file unwired.
        A fresh value, too: a `Project` parses its config once.
        """
        (self.db.parent / "config.toml").write_text(toml)
        self.project = holophyte.project.Project.locate(self.target)

    def loop(self, *script, provider=None, fake=None):
        """Run `main()` over the queued tasks with the script answering agents.

        Returns the fake and the spawn guard; `main()`'s return code lands
        in `self.rc`. A test that needs the fake before the loop runs --
        a step that reads the turn the loop asks for -- builds it and
        passes it as `fake`; `script` is then unused.
        """
        fake = fake or FakeAgent(*script)
        provider = provider or StubProvider(a_task())
        self.last_provider = provider
        self.last_fake = fake
        with no_agent_processes() as guard:
            with patch.dict(sys.modules, {"linear_provider": provider}):
                with patch.object(holophyte.loop, "agent", fake):
                    self.rc = holophyte.operator.main(self.project, provider)
        return fake, guard

    def main_output(self, *script, provider=None):
        out = io.StringIO()
        with patch.object(sys, "stdout", out):
            self.loop(*script, provider=provider)
        return out.getvalue()

    def read(self, sql):
        """Query the store over a connection the factory never touched."""
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn.execute(sql).fetchall()

    def subjects(self, rev="main"):
        return self.git("log", rev, "--format=%s").splitlines()

    def transitions(self):
        """The edges the run's narrative stream says it walked."""
        return [summary.split(":")[0] for (summary,) in
                self.read("SELECT summary FROM runEvents ORDER BY seq")]

    def branches(self):
        return [line[2:].strip() for line in
                self.git("branch", "--list").splitlines()]


class CommitThenTimeout(Commit):
    """An implementer turn that commits real work, then hits the budget.

    The block is a real process under `run_capped`'s cap — the dispatch
    `agent()` arms the budget on — so the kill, the group reap and the
    `TimeoutExpired` carrying what the turn printed first are the real ones,
    not a scripted raise.
    """

    def play(self, cwd, turn):
        super().play(cwd, turn)
        block_until_killed(cwd, "partial progress before cap")


class IdleThenTimeout(Idle):
    """`CommitThenTimeout`'s empty-branch counterpart: the turn said its
    piece and committed nothing before the cap killed it, so its words exist
    only in the `TimeoutExpired`'s captured output."""

    def play(self, cwd, turn):
        block_until_killed(cwd, self.reply)


class Boom:
    """An implementer turn that dies the way a failed `sh()` does."""

    role = "implement"

    def play(self, cwd, turn):
        raise RuntimeError("`['git', 'checkout']` failed:\nfatal: scripted")


class Refuse:
    """An implementer turn that fails the run on purpose, reason attached."""

    role = "implement"

    def play(self, cwd, turn):
        raise holophyte.gates.RunFailure("some reason")


class InfraRefuse:
    """A turn lost to the factory's own plumbing, as the reviewer route
    raises it when its container will not start."""

    role = "implement"

    def play(self, cwd, turn):
        raise holophyte.gates.InfraFailure("the reviewer container did not start")


class Interrupt:
    """An implementer turn hit by Ctrl-C."""

    role = "implement"

    def play(self, cwd, turn):
        raise KeyboardInterrupt


# A scripted `WAIT` "exit" that is the timer tick instead: no child exited
# before the deadline (KO-353).
TICK = object()


class FakePool:
    """The spawn and wait seams of the scheduler, scripted.

    `spawn` records each command line and hands back a child with a pid of
    its own; `wait` takes the next scripted exit -- `(code, before)`, where
    `before` runs just before the exit is reported, the way a real worker's
    merge empties its ticket out of the board's listing -- and reports it
    for the oldest live child. Nothing here forks.
    """

    def __init__(self, exits):
        self.exits = list(exits)
        self.spawned = []   # the command lines, in spawn order
        self.envs = []
        self.alive = []     # pids, oldest first
        self.reaped = []
        self.timeouts = []  # the timeout each WAIT was called with
        self.next_pid = 5000

    def spawn(self, argv, **kwargs):
        self.spawned.append(argv)
        self.envs.append(kwargs.get("env") or {})
        self.next_pid += 1
        self.alive.append(self.next_pid)
        return type("Child", (), {"pid": self.next_pid})()

    def wait(self, children, timeout):
        if not self.alive:
            raise AssertionError("the scheduler waited with no child alive")
        if set(children) != set(self.alive):
            raise AssertionError(f"the scheduler waited on {sorted(children)}"
                                 f" with {self.alive} alive")
        self.timeouts.append(timeout)
        code, before = self.exits.pop(0)
        if before is not None:
            before()
        if code is TICK:
            # The deadline passed with no exit: the scheduler recounts.
            return None, None
        pid = self.alive.pop(0)
        self.reaped.append((pid, code))
        return pid, code


class MergeModeFixture(LoopFixture):
    """`[merge] mode = "pr"`'s fixture: an approved, verified candidate is
    pushed and opened as a pull request instead of merged, and the loop
    babysits the PR -- threads verdicted, fixed and answered, checks
    awaited -- until it merges through the PR's API or the run parks.
    `"local"`, or no key, merges as it always has.

    `git` and `gh` on PATH are fakes that record their argv: the fake `git`
    intercepts `push` alone and hands everything else to the real one, so
    the loop's worktrees, merges and rev-parses are real while the one call
    that would leave the machine is witnessed instead of made. The fake
    `gh` answers `pr create` with `URL` and `api` with what the test put in
    the state files: the PR's threads and checks for the state query, an
    empty success for the reply and resolve mutations, `MERGE_SHA` for the
    merge.

    Split from the tests so a suite elsewhere -- the conflicting-PR tests
    in `test_babysitter.py` -- drives the same fake GitHub without
    re-running the tests that came with it."""

    URL = "https://github.com/example/repo/pull/7"
    # The `origin` the fixture target is given: the repository the push
    # goes to and the one `gh pr create` must be pinned to.
    ORIGIN = "https://github.com/example/repo.git"
    MERGE_SHA = "9f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
    # A body the claim-time template gate accepts, so the run reaches the
    # gate with a ticket body for the PR to carry.
    BODY = VALID_BODY
    # Two threads a review bot might leave: a clear defect and a style nit.
    DEFECT = ("src/app.py", 10, "review-bot",
              "`load()` returns None when the file is missing and the"
              " caller indexes it: a crash on first run.")
    NIT = ("src/app.py", 20, "style-bot",
           "Prefer `thing_count` over `n` for this variable name.")

    @staticmethod
    def comment(number, author, body):
        """GraphQL comment: a bot login or a (login, typename) pair."""
        login, kind = (author if isinstance(author, tuple)
                       else (author, "Bot"))
        return {"author": {"login": login, "__typename": kind},
                "body": body,
                "url": f"{MergeModeFixture.URL}#discussion_r{number}"}

    @classmethod
    def thread(cls, number, path, line, author, body, replies=(),
               resolved=False, next_cursor=None):
        """One review thread as the GraphQL answer carries it: the opening
        comment, then `replies` (each `(author, body)`) as the follow-ups
        on its first page of comments; `next_cursor` names a further page
        the babysitter must fetch."""
        nodes = [cls.comment(number, author, body)]
        nodes += [cls.comment(f"{number}_{n}", who, text)
                  for n, (who, text) in enumerate(replies, 1)]
        return {"id": f"PRRT_{number}", "isResolved": resolved,
                "isOutdated": False, "path": path, "line": line,
                "comments": {"pageInfo": {"hasNextPage": next_cursor
                                          is not None,
                                          "endCursor": next_cursor},
                             "nodes": nodes}}

    @classmethod
    def comments_page(cls, number, replies, next_cursor=None):
        """A later page of one thread's comments from the thread query."""
        return {"data": {"node": {"comments": {
            "pageInfo": {"hasNextPage": next_cursor is not None,
                         "endCursor": next_cursor},
            "nodes": [cls.comment(f"{number}_p{n}", who, text)
                      for n, (who, text) in enumerate(replies, 1)]}}}}

    # What the fake `gh` swaps for the branch's real tip when it serves a
    # state: the PR's head is the candidate the loop pushed, unless a test
    # says otherwise (`head=`).
    HEAD = "HEAD_SHA"

    def pr_state(self, threads=(), checks="SUCCESS", merged=False,
                 head=HEAD, resolved=(), next_cursor=None,
                 mergeable="MERGEABLE", updated_at="2000-01-01T00:00:00Z"):
        """The state query's answer: `threads` (each a `DEFECT`/`NIT`-shaped
        tuple) open, `resolved` the same shape but resolved, the head's
        check rollup, whether the PR is merged, GitHub's `mergeable`
        answer (None for the lazy-computation `null`), `updated_at` the
        `updatedAt` ISO stamp, and -- for a page that is not the last --
        the cursor of the next."""
        nodes = [self.thread(n, *t) for n, t in enumerate(threads, 1)]
        nodes += [self.thread(n, *t[:4], resolved=True)
                  for n, t in enumerate(resolved, len(nodes) + 1)]
        return {"data": {"repository": {"pullRequest": {
            "state": "MERGED" if merged else "OPEN", "merged": merged,
            "headRefOid": head, "mergeable": mergeable,
            "updatedAt": updated_at,
            "mergeCommit": {"oid": self.MERGE_SHA} if merged else None,
            "commits": {"nodes": [{"commit": {"statusCheckRollup":
                                              {"state": checks}}}]},
            "reviewThreads": {
                "pageInfo": {"hasNextPage": next_cursor is not None,
                             "endCursor": next_cursor},
                "nodes": nodes}}}}}

    def fake_route(self, push_exit=0, push_sh="", states=None,
                   comments=(), open_pr=None, close_exit=0,
                   refuse_labels=False, refuse_reactions=False):
        """Put a recording `git` and `gh` ahead of the real PATH, and give
        the target an `origin` for them to name.

        Each call appends its argv to `self.calls`; `gh pr create` keeps
        the body it read on stdin in `self.pr_body` and prints `URL`; each
        `gh api` call keeps its JSON body under `self.api_dir` (read back
        by `api_calls()`) and answers by what the body asks: the state
        query gets the first of `states` (each served once until the last,
        which is served forever), a mutation an empty success, the merge
        `MERGE_SHA`, a thread's further comments page the next of
        `comments` (each a `comments_page()`), the open step's
        `pullRequests(headRefName:)` lookup (KO-407) one open pull request
        at `open_pr` -- none without it -- and the reconcile's pull-status
        read (KO-359) an open pull request; the check-runs, branch-rules
        and branch reads answer no runs, no rules and no protection
        (KO-652), and a job's log
        read answers `self.job_log`'s text -- a failed call without it.
        A conversation comment answers its id (the body's number); a label
        call or a comment delete is witnessed by `recorded()`, a label
        call's body kept a line each in `self.label_log`, and the label
        call refused when `refuse_labels` (KO-608). An `addReaction`
        mutation answers an empty success, or fails when `refuse_reactions`
        (KO-679).
        `push_exit` and `push_sh` control push failure and an optional
        delay; a pull request's REST close (`PATCH`, KO-611) answers
        closed, or fails with `close_exit`. A push
        the fake answers successfully also appends `REF SHA` to
        `self.push_log`: the refspec's source resolved in the pushing
        checkout at push time, which is the tip a real remote's branch
        would have received (`pushed()` reads it back).
        """
        self.git("remote", "add", "origin", self.ORIGIN)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bindir = Path(tmp.name)
        self.calls = bindir / "calls.log"
        self.pr_body = bindir / "pr_body.md"
        self.push_log = bindir / "pushes.log"
        self.api_dir = bindir / "api"
        self.api_dir.mkdir()
        self.job_log = bindir / "job.log"
        self.label_log = bindir / "labels.log"
        # The open step's lookup answer: `open_pr` is the URL the branch
        # is already open as, None the common "no open pull request".
        self.open_answer = bindir / "open.json"
        nodes = [{"url": open_pr}] if open_pr else []
        self.open_answer.write_text(json.dumps(
            {"data": {"repository":
                      {"pullRequests": {"nodes": nodes}}}}))
        answers = bindir / "states"
        answers.mkdir()
        for n, state in enumerate([self.pr_state()] if states is None
                                  else states, 1):
            (answers / f"{n:03d}.json").write_text(json.dumps(state))
        # Kept on the fixture so `serve()` can hand a resumed run a fresh
        # answer sequence mid-test without re-faking PATH.
        self.answers = answers
        pages = bindir / "comments"
        pages.mkdir()
        for n, page in enumerate(comments, 1):
            (pages / f"{n:03d}.json").write_text(json.dumps(page))
        real_git = shutil.which("git")
        # Unqualified fetch stays local; fetches with a refspec use real git.
        # Pushes are witnessed in push_log, and ls-remote reads that remote
        # head independently of the API; ancestry and worktrees use real git.
        (bindir / "git").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = fetch ] && [ "$#" = 2 ] && [ "$2" = origin ];'
            " then exit 0; fi\n"
            'if [ "$1" = ls-remote ] && [ "$2" = origin ]; then\n'
            f'  tail -1 "{self.push_log}" | awk \'{{print $2}}\'\n'
            '  exit 0\n'
            'fi\n'
            'if [ "$1" = push ]; then\n'
            f'  printf "git %s\\n" "$*" >> "{self.calls}"\n'
            f"{push_sh}\n"
            f'  if [ {push_exit} -ne 0 ]; then\n'
            '    echo "remote: refused" >&2\n'
            f"    exit {push_exit}\n"
            "  fi\n"
            # The push is witnessed, not made; what a real remote's
            # branch would have received is the refspec's source
            # resolved now, in the pushing checkout.
            '  for src in "$@"; do :; done\n'
            '  src="${src%%:*}"; src="${src#+}"\n'
            f'  printf "%s %s\\n" "$src" "$("{real_git}" rev-parse'
            f' "$src" 2>/dev/null || echo MISSING)" >> "{self.push_log}"\n'
            "  exit 0\n"
            "fi\n"
            f'exec "{real_git}" "$@"\n')
        (bindir / "gh").write_text(
            "#!/bin/sh\n"
            f'printf "gh %s\\n" "$*" >> "{self.calls}"\n'
            'if [ "$1" = api ]; then\n'
            '  case "$*" in\n'
            '    *check-runs*) echo \'{"check_runs":[]}\'; exit 0;;\n'
            '    *rules/branches/*) echo \'[]\'; exit 0;;\n'
            '    */branches/*) echo \'{}\'; exit 0;;\n'
            '    *"--method PATCH repos/example/repo/pulls/"*) cat >/dev/null;'
            f' [ {close_exit} -eq 0 ] || {{ echo "HTTP 422 refused" >&2;'
            f' exit {close_exit}; }}; echo \'{{"state":"closed"}}\'; exit 0;;\n'
            f'    */issues/*/labels*) cat >> "{self.label_log}"; '
            f'echo >> "{self.label_log}"; '
            + ('echo "label refused" >&2; exit 1;;\n' if refuse_labels
               else "echo '[]'; exit 0;;\n")
            + '    *" DELETE "*/issues/comments/*) exit 0;;\n'
            f'    *actions/jobs/*/logs*) cat "{self.job_log}" && exit 0;'
            ' exit 1;;\n'
            '    *"GET repos/example/repo/pulls/"*) '
            "python3 -c 'import json,pathlib; "
            f'p=pathlib.Path("{self.pr_body}"); '
            'print(json.dumps(dict(title="feat(x): do y (KO-1)", '
            'body=p.read_text() if p.exists() else "")))'
            "'; exit 0;;\n"
            '  esac\n'
            f'  n=$(ls "{self.api_dir}" | wc -l); n=$((n+1))\n'
            f'  body="{self.api_dir}/$n.json"; cat > "$body"\n'
            '  if echo "$*" | grep -q "/issues/.*/comments"; then\n'
            '    echo "{\\"id\\":$n}"; exit 0\n'
            '  fi\n'
            '  if grep -q resolveReviewThread "$body"; then\n'
            "    echo '{\"data\":{\"resolveReviewThread\":{}}}'\n"
            '  elif grep -q addPullRequestReviewThreadReply "$body"; then\n'
            "    echo '{\"data\":{\"addPullRequestReviewThreadReply\":{}}}'\n"
            '  elif grep -q addReaction "$body"; then\n'
            + ('    echo "reaction refused" >&2; exit 1\n' if refuse_reactions
               else "    echo '{\"data\":{\"addReaction\":{}}}'\n")
            + '  elif grep -q mergedBy "$body"; then\n'
            "    echo '{\"data\":{\"repository\":{\"pullRequest\":"
            "{\"state\":\"OPEN\",\"merged\":false}}}}'\n"
            '  elif grep -q PullRequestReviewThread "$body"; then\n'
            f'    f=$(ls "{pages}"/*.json | head -1); cat "$f"; rm "$f"\n'
            '  elif grep -q headRefName "$body"; then\n'
            f'    cat "{self.open_answer}"\n'
            '  elif grep -q reviewThreads "$body"; then\n'
            f'    f=$(ls "{answers}"/*.json | head -1)\n'
            f'    tip=$("{real_git}" -C "{self.target}" rev-parse {BRANCH})\n'
            f'    sed "s/{self.HEAD}/$tip/" "$f"\n'
            f'    [ $(ls "{answers}"/*.json | wc -l) -gt 1 ] && rm "$f"\n'
            "  else\n"
            f"    echo '{{\"sha\":\"{self.MERGE_SHA}\",\"merged\":true}}'\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            f'cat > "{self.pr_body}"\n'
            f"echo {self.URL}\n")
        for script in ("git", "gh"):
            (bindir / script).chmod(0o755)
        patcher = patch.dict(os.environ,
                             {"PATH": f"{bindir}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorded(self):
        return (self.calls.read_text().splitlines()
                if self.calls.exists() else [])

    def pushed(self):
        """Every `git push` the fake answered, as `(ref, sha)`: the
        refspec's source resolved in the pushing checkout at push time --
        the tip a real remote's branch would have received, which is the
        witness a bare argv count cannot give."""
        return [tuple(line.split())
                for line in (self.push_log.read_text().splitlines()
                             if self.push_log.exists() else [])]

    def serve(self, *states):
        """Replace the state answers the fake `gh` still owes with `states`
        -- served in order, the last one sticky -- so a run resumed
        mid-test reads what GitHub now says. The calls log is untouched:
        the pushes and requests already witnessed keep counting."""
        n = max((int(p.stem) for p in self.answers.iterdir()), default=0)
        for p in self.answers.iterdir():
            p.unlink()
        for k, state in enumerate(states or (self.pr_state(),), n + 1):
            (self.answers / f"{k:03d}.json").write_text(json.dumps(state))

    def api_calls(self):
        """Every `gh api` body the babysitter made, in order, as `(kind,
        variables)`: the kind is `state`, `reply`, `resolve`, `react` or
        `merge`.
        The loop's per-pass pull-status read of a parked run (KO-359) is
        left out: it is the reconcile's, tested on its own below, and
        every pass after a park makes one. The open step's
        `pullRequests(headRefName:)` lookup (KO-407) is left out too: the
        open step's, not the pass's, witnessed by `recorded()` and the
        api bodies instead."""
        calls = []
        for path in sorted(self.api_dir.iterdir(),
                           key=lambda p: int(p.stem)):
            body = json.loads(path.read_text())
            query = body.get("query", "")
            if "mergedBy" in query or "headRefName" in query:
                continue
            kind = ("resolve" if "resolveReviewThread" in query
                    else "reply" if "addPullRequestReviewThreadReply" in query
                    else "react" if "addReaction" in query
                    else "comments" if "PullRequestReviewThread" in query
                    else "state" if "reviewThreads" in query
                    else "conversation" if "body" in body else "merge")
            calls.append((kind, body.get("variables", body)))
        return calls

    def provider(self):
        return StubProvider(dict(a_task(), body=self.BODY))

    def question(self):
        ((status, question),) = self.read(
            "SELECT status, blockedQuestion FROM tickets")
        self.assertEqual(status, "blocked_on_operator")
        return question

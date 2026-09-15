from __future__ import annotations

import io
import os
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
import holophyte.reconcile  # noqa: E402 - after the sys.path insert above
import holophyte.target  # noqa: E402 - after the sys.path insert above

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
        # What `fetch_task()` hands back, kept apart from the queue so a test
        # can leave the live ticket saying something other than the one that
        # was claimed — the mid-run edit the merge gate exists to catch. The
        # loop only reads it at the gate, so seeding it up front and editing
        # it during the run are the same thing from the loop's side.
        self.live = {task["issue_id"]: task for task in tasks}
        self.states = []
        self.comments = []
        # The board lease label (KO-351): the labels each issue carries now,
        # seeded from the task's `labels`; every label write in order as
        # `("label" | "unlabel", issue_id, name)` with the exact name; and
        # every read-back, as the issue asked about.
        self.labels = {task["issue_id"]: list(task.get("labels") or [])
                       for task in tasks}
        self.label_calls = []
        self.read_calls = []

    def claim_next(self, skip=(), order="identifier"):
        """The first queued task the loop has not already refused.

        `skip` is honored rather than ignored because the real provider hands
        back the *same* head-of-queue ticket on every ask; a stub that popped
        blindly would let a loop that cannot skip look like one that can.
        """
        for i, task in enumerate(self.queue):
            if task["id"] not in skip:
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
    """The real repo, worktree directory and store every loop test runs on.

    Split from the tests so a suite with its own configuration — the
    `[worktree]` one below — reuses the fixture without re-running the tests
    that came with it.
    """

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

        # Where `Target.locate(self.target)` will look: the target's directory
        # under a HOLOPHYTE_HOME of this test's own, never the operator's real
        # one.
        home = patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")})
        home.start()
        self.addCleanup(home.stop)
        self.db = holophyte.target.state_dir(self.target) / "store.db"
        self.db.parent.mkdir(parents=True)
        self.tgt = holophyte.target.Target.locate(self.target)
        assert self.tgt.store_path == self.db
        assert self.tgt.worktrees == self.worktrees

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=str(cwd or self.target),
                              check=True, capture_output=True, text=True).stdout

    def configure(self, toml):
        """Give the fixture target a config file and a `Target` that reads it.

        Through `Target.locate()` rather than by writing `config_path` by
        hand: it derives every path from the target the same way the fixture
        does, so a test that set the config by hand could pass with the file
        unwired. A fresh value, too: a `Target` parses its config once.
        """
        (self.db.parent / "config.toml").write_text(toml)
        self.tgt = holophyte.target.Target.locate(self.target)

    def loop(self, *script, provider=None, fake=None):
        """Run `main()` over the queued tasks with the script answering agents.

        Returns the fake and the spawn guard, so a test can read both the
        turns the loop took and the processes it did not start; `main()`'s
        return code lands in `self.rc` for the tests that pin the exit
        contract. A test that needs the fake before the loop runs -- a step
        that reads the turn the loop is asking for -- builds it and passes
        it as `fake`; `script` is then unused.
        """
        fake = fake or FakeAgent(*script)
        provider = provider or StubProvider(a_task())
        self.last_provider = provider
        self.last_fake = fake
        with no_agent_processes() as guard:
            with patch.dict(sys.modules, {"linear_provider": provider}):
                with patch.object(holophyte.loop, "agent", fake):
                    self.rc = holophyte.operator.main(self.tgt, provider)
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

    `SPAWN` records each command line and hands back a child with a pid of
    its own; `WAIT` takes the next scripted exit -- `(code, before)`, where
    `before` runs against the provider just before the exit is reported, the
    way a real worker's merge empties its ticket out of the board's listing
    -- and reports it for the oldest live child. Nothing here forks: the
    pids are numbers, and the test reads what would have run.
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

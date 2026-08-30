"""A scripted stand-in for the loop's agent turns — zero real agent calls.

`factory.run_task()` reaches the outside world at exactly one point: the
module-level `agent(role, goal, cwd, ...)` call it makes for every implement,
review and adjudicate turn. Everything else the loop does — cutting the
worktree, the verify gate, the fix commits, the `--no-ff` merge — is real git
against a real repo. So a test can drive the whole control flow honestly by
replacing that one callable, and nothing else, with a script.

A script is a flat list of steps consumed one per agent turn, in the order the
loop takes them:

    FakeAgent(Commit("work"), REQUEST_CHANGES, Commit("fix"), APPROVE)

Implementer steps (`Commit`, `Idle`) really run git in the worktree the loop
hands them, so the verify gate and the merge see commits an agent could have
made. Reviewer steps (`Reply`, and the named verdicts below) hand back text
verbatim, so a malformed reply reaches the loop as malformed text rather than
as an exception the harness invented.

Mismatches fail loudly: a script whose step kind disagrees with the turn the
loop asked for, or that runs out mid-run, raises `ScriptError` instead of
quietly answering the wrong turn.
"""
from __future__ import annotations

import contextlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

IMPLEMENT = "implement"
REVIEW_ROLES = ("review", "adjudicate")


class ScriptError(AssertionError):
    """The script and the loop disagree about what turn this is."""


def _git(cwd, *args):
    """One git command in `cwd`, loud on failure.

    The worktree inherits the fixture repo's identity config, so a scripted
    commit needs no setup of its own.
    """
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True)
    if r.returncode != 0:
        raise ScriptError(f"scripted git {args} failed in {cwd}:\n"
                          f"{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


@dataclass
class Commit:
    """An implementer turn that really writes a file and really commits it.

    `path` defaults to a per-turn name so consecutive steps never collide;
    naming it explicitly is how a script makes a later step edit an earlier
    step's file.
    """

    message: str = "scripted work"
    path: str | None = None
    body: str | None = None

    role = IMPLEMENT

    def play(self, cwd, turn):
        target = cwd / (self.path or f"scripted-{turn}.txt")
        target.write_text(self.body or f"{self.message}\n")
        _git(cwd, "add", "-A")
        _git(cwd, "commit", "-q", "-m", self.message)
        return f"committed: {self.message}"


@dataclass
class Idle:
    """An implementer turn that answers but commits nothing.

    The loop reads "no new commit" as no progress, so this is how a script
    drives the stalled-implementer paths without simulating a clock.
    """

    reply: str = "I looked at it and changed nothing."

    role = IMPLEMENT

    def play(self, cwd, turn):
        return self.reply


@dataclass(frozen=True)
class Reply:
    """A review or adjudication turn: scripted text, handed back verbatim.

    Frozen so the shared verdict constants below can be reused across scripts
    and across tests without one run editing another's script.
    """

    text: str

    role = REVIEW_ROLES

    def play(self, cwd, turn):
        return self.text


# The four well-formed verdicts, plus the reply that names none. Written out
# as the reviewer would end them, because `review_runner.terminal_verdict()`
# reads the last line and the loop's malformed-reply path exists precisely for
# text that does not have one.
APPROVE = Reply("Reviewed the diff; no blockers.\nVERDICT: APPROVE")
REQUEST_CHANGES = Reply("Blocker: the scripted change is incomplete.\n"
                        "VERDICT: REQUEST_CHANGES")
PASS = Reply("Mergeable as it stands.\nVERDICT: PASS")
FAIL = Reply("Not mergeable as it stands.\nVERDICT: FAIL")
MALFORMED = Reply("I have some thoughts about this but never say the word.")


@dataclass(frozen=True)
class Turn:
    """One agent call as the loop made it."""

    role: str
    goal: str
    cwd: Path
    base_sha: str | None = None
    candidate_sha: str | None = None


class FakeAgent:
    """A drop-in for `factory.agent` that replays `script`, one step per turn.

    Patch it over the real callable — `patch.object(factory, "agent", fake)` —
    and the loop runs unchanged with no agent process anywhere in it. The
    turns it was asked for are kept in order, so a test can assert the flow
    the loop actually walked rather than the flow the script hoped for.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.turns: list[Turn] = []
        self.replies: list[str] = []

    def __call__(self, role, goal, cwd, *, base_sha=None, candidate_sha=None):
        n = len(self.turns) + 1
        if not self.script:
            raise ScriptError(f"script exhausted: the loop asked for a {role!r}"
                              f" turn (#{n}) the script has no step for")
        step = self.script.pop(0)
        wanted = step.role
        wanted = (wanted,) if isinstance(wanted, str) else wanted
        if role not in wanted:
            raise ScriptError(f"turn #{n} is a {role!r} turn but the script's"
                              f" next step answers {'/'.join(wanted)}")
        if role in REVIEW_ROLES and not (base_sha and candidate_sha):
            # The real reviewer route refuses the turn without both, so a loop
            # that stopped passing them would otherwise only break in prod.
            raise ScriptError(f"{role!r} turn #{n} arrived without an exact"
                              f" base_sha and candidate_sha")
        self.turns.append(Turn(role, goal, Path(cwd), base_sha, candidate_sha))
        reply = step.play(Path(cwd), n)
        self.replies.append(reply)
        return reply

    @property
    def roles(self):
        """The turn sequence the loop asked for, e.g. `['implement', 'review']`."""
        return [turn.role for turn in self.turns]


# The binaries a real agent turn runs. `docker` is here because the review
# route dispatches Codex inside a container: a review that escaped the fake
# would spawn that, not `codex` directly.
AGENT_BINARIES = ("claude", "codex", "docker", "podman")


@dataclass
class SpawnGuard:
    """Records — and refuses — every attempt to spawn a real agent process."""

    blocked: tuple = AGENT_BINARIES
    spawned: list = field(default_factory=list)

    def check(self, args):
        if isinstance(args, (str, bytes)):
            argv = str(args).split()
        else:
            argv = [str(a) for a in (args or [])]
        name = Path(argv[0]).name if argv else ""
        if name in self.blocked:
            self.spawned.append(argv)
            raise AssertionError(f"a real agent process was spawned: {argv}")


@contextlib.contextmanager
def no_agent_processes(guard=None):
    """Fail the test if anything under it spawns a real agent process.

    The independent oracle for "zero API calls": patching `factory.agent` is
    what the test *does*, and asserting on the same patch would only restate
    it. This watches the process boundary underneath instead, so an agent
    reached by any other path — a review route the fake never covered, a
    subprocess added later — is a failure and not a silent live call. Git and
    the verify command run untouched.
    """
    guard = guard or SpawnGuard()
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run(*args, **kwargs):
        guard.check(args[0] if args else kwargs.get("args"))
        return real_run(*args, **kwargs)

    def popen(*args, **kwargs):
        guard.check(args[0] if args else kwargs.get("args"))
        return real_popen(*args, **kwargs)

    with patch.object(subprocess, "run", run), \
            patch.object(subprocess, "Popen", popen):
        yield guard

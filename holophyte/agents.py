"""The agent routes: one turn of a role, dispatched and read back as text.

`agent` runs an implementer, reviewer or adjudicator turn -- the default
route for each role, or the `[agents]` command the target configured --
under the process-group cap the gate shares; `agent_route` names what ran, for
the record the round leaves; `publish_review_refs` gives a configured reviewer
the same two `refs/review/*` names the staged default route gets. This is the
factory's only process-spawning surface besides the gate. Nothing here knows
the loop, the store or the board: config and gates in, output text out.

Third slice of the phase-2 module split; moved verbatim from `factory.py`,
which imports back the names its remaining call sites use.
"""
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import review_runner
from holophyte.config import (
    AGENT_CONFIG_KEYS,
    DEFAULT_IMPLEMENTER,
    IMPL_EFFORT,
    IMPL_MODEL,
    IMPL_TIMEOUT,
    agent_command,
    carry_directories,
    review_profile,
    review_route,
)
from holophyte.gates import InfraFailure, run_capped, sh

# The one-line prompt the implementer probe hands a configured route, and the
# word its answer has to contain. Short enough that any harness answering at
# all answers it inside `PROBE_TIMEOUT` seconds; the cap is generous next to a
# healthy CLI's cold start and small next to the implement turn that would
# otherwise be the first evidence of a route that does not answer.
PROBE_GOAL = "Reply with the single word: ready"
PROBE_WORD = "ready"
PROBE_TIMEOUT = 90
PROBE_TAIL_LINES = 5


@dataclass(frozen=True)
class ProbeResult:
    """What one implementer probe found: the exact argv it ran, the exit
    code (`None` when the cap ended it), and the output it read back."""

    command: list
    returncode: object
    output: str
    timeout: int

    @property
    def timed_out(self):
        return self.returncode is None

    @property
    def ok(self):
        return self.returncode == 0 and PROBE_WORD in self.output.lower()

    def describe(self):
        """The line(s) the loop prints: the command as a shell would take it,
        then the verdict -- passed, the exit code, or the timeout -- and the
        last lines of what the route said, so a failure is actionable from
        the terminal without re-running it by hand."""
        shown = " ".join(shlex.quote(part) for part in self.command)
        if self.ok:
            return f"[holo2] implementer probe passed: {shown}"
        if self.timed_out:
            why = f"no answer within {self.timeout}s"
        elif self.returncode:
            why = f"exit {self.returncode}"
        else:
            why = f"exit 0 but no {PROBE_WORD!r} in the output"
        tail = self.tail()
        lines = [f"[holo2] implementer probe failed ({why}): {shown}"]
        lines += [f"[holo2]   | {line}" for line in tail] or [
            "[holo2]   | (no output)"]
        return "\n".join(lines)

    def tail(self):
        """The last `PROBE_TAIL_LINES` lines the route printed."""
        return self.output.strip().splitlines()[-PROBE_TAIL_LINES:]

    def to_json(self):
        """The result as the daemon's `PUT /config` reply carries it: the
        verdict, the exact argv, the exit code (`null` on the timeout, with
        `timed_out` saying so), the cap and the output's last lines."""
        return {"ok": self.ok, "command": list(self.command),
                "returncode": self.returncode, "timed_out": self.timed_out,
                "timeout": self.timeout, "output": self.tail()}


def probe_implementer(target, timeout=None):
    """Run the configured `[agents] implementer` once, with `PROBE_GOAL` as
    the goal, and say whether it answered. `None` when the table names no
    implementer: the default route is not probed here.

    Routing is explicit policy, so the exact command a turn would run is the
    one probed: the argv comes from `agent_command()`, the same builder the
    turn uses, with the goal in the same last position. It runs in an empty
    temporary directory -- a harness that reads its cwd finds nothing to act
    on -- under `run_capped`'s process-group cap, so a route that hangs is
    ended with everything it started rather than left behind the loop. A pass
    is exit 0 with `PROBE_WORD` somewhere in the output; anything else, the
    timeout included, is a failure carrying what the route printed.
    """
    cmd = agent_command(target, "implement", PROBE_GOAL)
    if cmd is None:
        return None
    cap = PROBE_TIMEOUT if timeout is None else timeout
    with tempfile.TemporaryDirectory(prefix="holophyte-probe-") as scratch:
        try:
            code, out = run_capped(cmd, scratch, cap)
        except subprocess.TimeoutExpired as expired:
            partial = expired.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            return ProbeResult(cmd, None, partial, cap)
    return ProbeResult(cmd, code, out or "", cap)


def agent_route(target, role):
    """What ran `role`'s turn, named for the record the round leaves.

    The profile of the container route the config chooses (`codex-sol-medium`
    by default), or the configured command when the target named one. A
    `reviewRounds` row reading `codex-sol-medium` about a round some other
    harness or model ran would be evidence of something that did not happen,
    and the rows are what FINDINGS.md and the fingerprint are built from.
    """
    return ((target.config().get("agents") or {}).get(AGENT_CONFIG_KEYS[role])
            or review_profile(*review_route(target)))


def publish_review_refs(repo, base_sha, candidate_sha):
    """Name this round's two commits `refs/review/base` and
    `refs/review/candidate` inside `repo`.

    The default reviewer route gets those two refs from
    `review_runner.stage_candidate()`, which creates them in the checkout it
    builds. A configured reviewer or adjudicator runs in the task worktree
    instead, where nothing had ever created them — and the prompt it is handed
    tells it to review the base and the candidate by exactly those names. So
    the worktree gets the same two names for the same two commits, and the
    override is asked about the frozen pair rather than about whatever HEAD
    happens to be.

    The exact-SHA requirement holds on this route too, the same way the staged
    one enforces it: each side must be a full commit SHA that resolves here to
    itself, and the base must be an ancestor of the candidate. A round argues
    about one named candidate against one named base, whoever runs it.

    The refs live in the target repository's ref store, shared by its
    worktrees; that is safe because the project's run lease single-threads
    runs, and each round overwrites both refs with its own pair before
    dispatching. They are left behind afterwards, like the branch a finished
    run leaves for a human to look at.
    """
    for sha in (base_sha, candidate_sha):
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
            cwd=repo, capture_output=True, text=True)
        if resolved.returncode or resolved.stdout.strip() != sha:
            raise review_runner.ReviewBoundaryError(
                f"not a full commit SHA in {repo}: {sha}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", base_sha,
                       candidate_sha], cwd=repo).returncode:
        raise review_runner.ReviewBoundaryError(
            f"base {base_sha} is not an ancestor of {candidate_sha}")
    for name, sha in (("base", base_sha), ("candidate", candidate_sha)):
        sh(["git", "update-ref", f"refs/review/{name}", sha], cwd=repo)


def agent(target, role, goal, cwd, *, base_sha=None, candidate_sha=None,
          timeout=None, on_start=None):
    """Run one agent turn for a role. Returns combined output text.

    An `implement` turn runs in a process group of its own under `timeout`
    seconds (`IMPL_TIMEOUT` when the caller names none, and never more): a
    `claude -p` that reaches the cap is killed with every subagent and Bash
    child it started, and the turn raises `subprocess.TimeoutExpired` carrying
    what it printed first. Signalling only the CLI left its children
    committing into a worktree the loop had already given up on. `on_start`
    is handed that group's `Popen` as it starts (`GroupKill.arm`), so the
    loop can end the same group from outside the wait when the supervisor
    sweeps the run mid-turn (KO-339). The review and adjudicate routes run
    through `subprocess.run` -- the container runner's and the configured
    command's -- and hold no handle to hand over, so `on_start` is not
    called for them.

    `adjudicate` is the terminal pass/fail round. It takes the same
    independent reviewer route as `review` — a fresh dispatch that knows only
    the diff and the ticket — but its verdict is not enforced at the boundary:
    a reply that names no clean verdict has to reach the loop as text so it
    can be recorded and read as FAIL.

    A configured command replaces the role's route, so an `[agents] reviewer`
    override is also an opt-out of the hardened container the default reviewer
    runs behind. What it does not opt out of is the pair the round is about:
    the exact-SHA requirement is enforced either way, and either way the two
    commits reach the reviewer as `refs/review/base` and
    `refs/review/candidate` — the names its prompt uses — the staged checkout
    on the default route, the task worktree on the configured one.
    """
    if role not in AGENT_CONFIG_KEYS:
        raise ValueError(role)
    if role in ("review", "adjudicate") and not (base_sha and candidate_sha):
        raise ValueError(f"{role} requires exact base_sha and candidate_sha")
    cmd = agent_command(target, role, goal)
    if cmd is None:
        if role != "implement":
            model, effort = review_route(target)
            try:
                return review_runner.run_review(
                    repo=Path(cwd),
                    base_sha=base_sha,
                    candidate_sha=candidate_sha,
                    prompt=goal,
                    model=model,
                    effort=effort,
                    profile=review_profile(model, effort),
                    timeout=1800,
                    verdicts=(review_runner.REVIEW_VERDICTS
                              if role == "review" else None),
                    carry=carry_directories(target),
                )
            except review_runner.ReviewBoundaryError as e:
                # The runner could not stage, start or read the reviewer —
                # a missing CLI, an image that will not build, a container
                # that produced no events. The candidate was never judged,
                # so the failure is the factory's, not the ticket's.
                raise InfraFailure(f"reviewer route failed for {role}:"
                                   f" {e}") from e
        cmd = [DEFAULT_IMPLEMENTER, "-p", goal, "--model", IMPL_MODEL,
               "--effort", IMPL_EFFORT]
    elif role != "implement":
        publish_review_refs(Path(cwd), base_sha, candidate_sha)
    if role != "implement":
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=1800)
        return (r.stdout + "\n" + r.stderr).strip()
    cap = IMPL_TIMEOUT if timeout is None else min(timeout, IMPL_TIMEOUT)
    # The hook is passed only when there is one, so a turn without a
    # sweep-time kill runs exactly the call it always did.
    hook = {"on_start": on_start} if on_start is not None else {}
    _, out = run_capped(cmd, cwd, cap, **hook)
    return out.strip()

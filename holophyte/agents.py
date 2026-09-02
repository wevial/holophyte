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
import subprocess
from pathlib import Path

import review_runner
from holophyte.config import (
    AGENT_CONFIG_KEYS,
    DEFAULT_IMPLEMENTER,
    IMPL_EFFORT,
    IMPL_MODEL,
    IMPL_TIMEOUT,
    REVIEW_PROFILE,
    agent_command,
)
from holophyte.gates import InfraFailure, run_capped, sh


def agent_route(target, role):
    """What ran `role`'s turn, named for the record the round leaves.

    The default reviewer profile, or the configured command when the target
    named one. A `reviewRounds` row reading `codex-sol-medium` about a round
    some other harness ran would be evidence of something that did not happen,
    and the rows are what FINDINGS.md and the fingerprint are built from.
    """
    return ((target.config().get("agents") or {}).get(AGENT_CONFIG_KEYS[role])
            or REVIEW_PROFILE)


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
          timeout=None):
    """Run one agent turn for a role. Returns combined output text.

    An `implement` turn runs in a process group of its own under `timeout`
    seconds (`IMPL_TIMEOUT` when the caller names none, and never more): a
    `claude -p` that reaches the cap is killed with every subagent and Bash
    child it started, and the turn raises `subprocess.TimeoutExpired` carrying
    what it printed first. Signalling only the CLI left its children
    committing into a worktree the loop had already given up on.

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
            try:
                return review_runner.run_review(
                    repo=Path(cwd),
                    base_sha=base_sha,
                    candidate_sha=candidate_sha,
                    prompt=goal,
                    profile=REVIEW_PROFILE,
                    timeout=1800,
                    verdicts=(review_runner.REVIEW_VERDICTS
                              if role == "review" else None),
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
    _, out = run_capped(cmd, cwd, cap)
    return out.strip()

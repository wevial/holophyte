import codecs
import contextlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import review_runner
from holophyte.agents.agent_output import AgentOutput
from holophyte.config.reader import AGENT_CONFIG_KEYS
from holophyte.loop.gates import InfraFailure, run_capped, sh
from holophyte.redact import safe_print as print

PUBLISHING = threading.Lock()


def review_refs(run_id):
    prefix = "refs/review" if run_id is None else f"refs/review/{int(run_id)}"
    return f"{prefix}/base", f"{prefix}/candidate"


def cleanup_review_refs(repo, run_id):
    """A failed removal must not prevent lease and board close-out."""
    for ref in review_refs(run_id):
        try:
            sh(["git", "update-ref", "-d", ref], cwd=repo)
        except (OSError, RuntimeError) as exc:
            print(f"[holo2] review ref cleanup failed for {ref}: {exc}")


def check_review_refs(repo, run_id, base_sha, candidate_sha):
    """A moved or missing boundary is a factory failure, never a verdict."""
    for ref, sha in zip(review_refs(run_id), (base_sha, candidate_sha)):
        result = subprocess.run(["git", "rev-parse", "--verify", ref],
                                cwd=repo, capture_output=True, text=True)
        if result.returncode or result.stdout.strip() != sha:
            raise InfraFailure(f"review ref changed during turn: {ref}; expected {sha}")


def publish_review_refs(repo, base_sha, candidate_sha, run_id=None):
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
    with PUBLISHING:
        for name, sha in zip(review_refs(run_id), (base_sha, candidate_sha)):
            sh(["git", "update-ref", name, sha], cwd=repo)


@contextlib.contextmanager
def review_scratch(repo):
    env = scratch_git_environment()
    with tempfile.TemporaryDirectory(prefix="holophyte-review-") as scratch:
        try:
            yield Path(scratch)
        finally:
            try:
                for path in review_worktrees(repo, env=env):
                    if path.resolve().is_relative_to(Path(scratch).resolve()):
                        try:
                            sh(["git", "worktree", "remove", "--force", "--force",
                                str(path)], cwd=repo, env=env)
                        except (OSError, RuntimeError) as exc:
                            print(f"[holo2] review worktree cleanup failed: {exc}")
            finally:
                sh(["git", "worktree", "prune"], cwd=repo, env=env)


def scratch_git_environment():
    """Without git location overrides, a worktree command acts on its cwd."""
    return {key: value for key, value in os.environ.items()
            if key not in {"GIT_DIR", "GIT_COMMON_DIR",
                           "GIT_WORK_TREE", "GIT_INDEX_FILE"}}


def review_worktrees(repo, env=None):
    """Read porcelain on Git 2.34 (raw paths) and newer Git (C-quoted paths)."""
    listing = sh(["git", "worktree", "list", "--porcelain"], cwd=repo, env=env)
    # The HEAD/bare field ends a path: old Git can emit raw newlines in it.
    for raw in re.findall(r'^worktree (.*?)\n(?:HEAD [0-9a-f]+|bare)(?:\n|$)',
                          listing, re.MULTILINE | re.DOTALL):
        if raw.startswith('"'):
            raw = os.fsdecode(codecs.escape_decode(os.fsencode(raw[1:-1]))[0])
        yield Path(raw)


def configured_review(cmd, cwd, cap, env, role, command, on_start=None):
    try:
        code, output = run_capped(cmd, cwd, cap, on_start=on_start, env=env)
    except subprocess.TimeoutExpired:
        message = f"{AGENT_CONFIG_KEYS[role]} timed out after {cap / 60:g} minutes"
        return AgentOutput(message, command, timed_out=True)
    return AgentOutput(output.strip(), command, exit_code=code)


# What `codex exec resume` prints for an id it has no rollout for.
NO_ROLLOUT = "no rollout found"


def table_review(seat, goal, repo, scratch, cap, env, role, *, run_id=None,
                 conn=None, kill=None):
    deadline = time.monotonic() + cap
    on_start = kill.arm if kill is not None else None
    checkout = Path(scratch) / "candidate"
    sh(["git", "worktree", "add", "--detach", "--quiet", str(checkout),
        review_refs(run_id)[1]], cwd=repo, env=scratch_git_environment())

    def remaining():
        return max(0.0, deadline - time.monotonic())

    def ask(argv, timeout):
        return run_capped(argv, checkout, min(timeout, remaining()),
                          on_start=on_start, env=env, stderr=subprocess.DEVNULL)

    session = env.get("HOLOPHYTE_REVIEW_RESUME")
    argv = seat.resume(session) + [goal] if session else seat.turn(goal)
    output = configured_review(argv, checkout, cap, env, role, seat.named(argv),
                               on_start=on_start)
    if session and not output.timed_out and NO_ROLLOUT in output:
        if conn is not None and run_id is not None:
            import store
            store.record_event(conn, run_id, "review_session",
                               "review session: resume found no rollout",
                               level="detail", payload=json.dumps(
                                   {"arm": "resume", "requested": True,
                                    "resumed": False, "reason": NO_ROLLOUT}))
        argv = seat.turn(goal)
        output = configured_review(argv, checkout, remaining(), env, role,
                                   seat.named(argv), on_start=on_start)
    if kill is not None and kill.wanted:
        return output
    reported = seat.reported_session(output, ask)
    if reported and not (kill is not None and kill.wanted):
        (Path(scratch) / "session").write_text(reported, encoding="utf-8")
    return output


@contextlib.contextmanager
def critic_workspace(project):
    with review_scratch(project.path) as scratch:
        checkout = scratch / "main"
        sh(["git", "worktree", "add", "--detach", "--quiet", str(checkout),
            "main"], cwd=project.path, env=scratch_git_environment())
        yield checkout

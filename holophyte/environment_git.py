"""Keep the factory's filtered environment outside candidate history."""
import subprocess
from pathlib import Path

from holophyte.config import config_table
from holophyte.gates import InfraFailure, sh


def protected(target):
    return "env_source" in config_table(target, "worktree")


def paths(target):
    """Pathspecs for factory status and staging, independent of ignore rules."""
    return ["--", ".", ":(top,exclude).env"] if protected(target) else []


def exclude_environment(wt):
    if sh(["git", "ls-files", "--", ".env"], wt):
        raise InfraFailure("worktree environment .env is tracked; "
                           "refusing to overwrite it")
    ignored = subprocess.run(["git", "check-ignore", "-q", "--", ".env"],
                             cwd=wt, capture_output=True)
    if ignored.returncode == 0:
        return
    exclude = Path(wt) / sh(["git", "rev-parse", "--git-path", "info/exclude"], wt)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n/.env\n")


def unstage_environment(target, wt):
    """Clear a forced staged environment even when there is no other work."""
    if protected(target):
        # Also remove a forced, already staged environment from the index.
        sh(["git", "reset", "-q", "HEAD", "--", ".env"], wt)


def stage_work(target, wt):
    unstage_environment(target, wt)
    sh(["git", "add", "-A", *paths(target)], wt)


def refuse_environment_history(target, branch, *, action):
    if not protected(target):
        return
    tree = sh(["git", "ls-tree", "--name-only", branch, "--", ".env"], target.path)
    history = sh(["git", "log", "--format=%H", f"main..{branch}", "--", ".env"],
                 target.path)
    if tree or history:
        raise InfraFailure("candidate contains .env in its tree or history; "
                           f"refusing to {action}")

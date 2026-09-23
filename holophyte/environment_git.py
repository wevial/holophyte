"""Keep the factory's filtered environment outside candidate history."""
import stat
import subprocess
from pathlib import Path

from holophyte.config import config_table
from holophyte.gates import InfraFailure, sh


def protected(target):
    return "env_source" in config_table(target, "worktree")


def paths(target):
    """Hide protected .env from status even if its ignore rule disappeared."""
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
    sh(["git", "add", "-A"], wt)
    unstage_environment(target, wt)


def factory_identity(wt):
    """The `-c` pins a commit the factory makes itself in `wt` carries.

    Empty when the checkout resolves both `user.name` and `user.email`, so
    the commit is authored by the configured identity the implementer's own
    commits carry -- a deploy platform that checks authors refuses a made-up
    one (KO-656). The `holophyte` pins otherwise, so a target with no
    committer configured cannot make the commit raise.
    """
    configured = [subprocess.run(["git", "config", "--get", key], cwd=wt,
                                 capture_output=True, text=True).stdout.strip()
                  for key in ("user.name", "user.email")]
    if all(configured):
        return []
    return ["-c", "user.name=holophyte",
            "-c", "user.email=holophyte@factory.invalid"]


def refuse_environment_history(target, branch, *, action, commit=None):
    if not protected(target):
        return branch
    commit = commit or sh(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"],
        target.path)
    tree = sh(["git", "ls-tree", "--name-only", commit, "--", ".env"], target.path)
    history = sh(["git", "log", "--format=%H", f"main..{commit}", "--", ".env"],
                 target.path)
    if tree or history:
        raise InfraFailure("candidate contains .env in its tree or history; "
                           f"refusing to {action}")
    return commit


def environment_temporary_directory(wt):
    """Recover interrupted writes only in this checkout's metadata directory."""
    git_dir = Path(sh(["git", "rev-parse", "--absolute-git-dir"], wt))
    common_dir = Path(wt) / sh(["git", "rev-parse", "--git-common-dir"], wt)
    if git_dir.resolve() == common_dir.resolve():
        # The primary checkout shares its Git directory with linked worktrees.
        git_dir = git_dir / "holophyte-env"
        git_dir.mkdir(mode=0o700, exist_ok=True)
    for leftover in git_dir.glob(".env-*"):
        if stat.S_ISREG(leftover.lstat().st_mode):
            leftover.unlink()
    return git_dir

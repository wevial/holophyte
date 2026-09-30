"""A task worktree's `.env`, capture `.gitignore`, setup run and retirement."""
import os
import subprocess
import tempfile
from pathlib import Path

from holophyte.config.config_tables import merge_config
from holophyte.config.worktree_settings import (
    setup_commands,
    setup_timeout,
    worktree_environment,
)
from holophyte.environment_git import (
    environment_temporary_directory,
    exclude_environment,
)
from holophyte.loop.gates import InfraFailure, run_verify, sh
from holophyte.loop.runs import set_phase
from holophyte.redact import redact_values
from holophyte.redact import safe_print as print


def timeout_report(cmd, expired):
    out = expired.output or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    out = out.strip()[-2000:]
    return (f"[verify] command timed out after {expired.timeout:g}s: {cmd}\n"
            + (out or "(no output before the timeout)"))


def write_worktree_environment(project, wt):
    """Replace .env atomically, never following an existing symlink or hardlink."""
    values = worktree_environment(project)
    if values is None:
        return
    exclude_environment(wt)
    # Git metadata keeps interrupted writes outside subsequent `git add -A`.
    git_dir = environment_temporary_directory(wt)
    fd, temporary = tempfile.mkstemp(prefix=".env-", dir=git_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write("".join(f"{name}={value}\n" for name, value in values.items()))
        os.replace(temporary, Path(wt) / ".env")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_capture_ignore(project, wt):
    """Tracked symlinks are possible, so no path component is followed."""
    cfg = merge_config(project)
    if not cfg.ui_capture_local:
        return
    root = Path(wt).resolve()
    directory = root
    for part in Path(cfg.ui_capture_dir).parts:
        directory = directory / part
        if directory.is_symlink():
            raise OSError(f"{directory.relative_to(root)} is a symlink")
        directory.mkdir(exist_ok=True)
    ignore = directory / ".gitignore"
    if ignore.is_symlink():
        raise OSError(f"{ignore.relative_to(root)} is a symlink")
    if not directory.resolve().is_relative_to(root):
        raise OSError(f"{cfg.ui_capture_dir} resolves outside the worktree")
    fd, temporary = tempfile.mkstemp(prefix=".gitignore-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write("*\n")
        os.replace(temporary, ignore)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_worktree_setup(project, wt, conn=None, run_id=None):
    try:
        write_worktree_environment(project, wt)
    except (SystemExit, InfraFailure) as error:
        return False, redact_values(str(error))
    except OSError:
        return False, "[holo2] worktree environment file could not be written"
    try:
        write_capture_ignore(project, wt)
    except (SystemExit, OSError) as error:
        return False, f"[holo2] local capture directory not prepared: {error}"
    commands = setup_commands(project)
    timeout = setup_timeout(project)
    if commands:
        set_phase(conn, run_id, "working",
                  f"worktree setup: {len(commands)} command(s) in {wt}")
    for n, command in enumerate(commands, 1):
        try:
            ok, out = run_verify(command, wt, timeout=timeout, project=project)
        except subprocess.TimeoutExpired as e:
            ok, out = False, timeout_report(command, e)
        if not ok:
            return False, (f"[holo2] worktree setup command {n} of "
                           f"{len(commands)} FAILED: {redact_values(command)}\n"
                           f"{redact_values(out)}")
        print(f"[holo2] worktree setup {n}/{len(commands)} ok: {command}")
    if (Path(wt) / ".githooks").is_dir():
        try:
            # Without this extension --worktree writes the shared config.
            sh(["git", "config", "extensions.worktreeConfig", "true"], wt)
            sh(["git", "config", "--worktree", "core.hooksPath", ".githooks"], wt)
        except RuntimeError as error:
            return False, f"[holo2] worktree setup hooks configuration FAILED:\n{error}"
    return True, ""


def _retirement_remote_tip(project, branch):
    remote = subprocess.run(
        ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        cwd=project.path, capture_output=True, text=True, timeout=60)
    if remote.returncode == 2:
        return None
    if remote.returncode:
        raise RuntimeError("remote verification failed (git ls-remote): "
                           + remote.stderr.strip())
    tip = remote.stdout.split()[0]
    fetched = subprocess.run(
        ["git", "fetch", "--no-tags", "--no-write-fetch-head", "origin", tip],
        cwd=project.path, capture_output=True, text=True, timeout=60)
    if fetched.returncode:
        raise RuntimeError("remote verification failed (git fetch): "
                           + fetched.stderr.strip())
    return tip


def retire_worktree(project, branch):
    """Stale remote-tracking refs are not evidence the work is backed up."""
    from holophyte.config.project import worktree_path

    wt = worktree_path(project, branch)
    if wt.resolve() == project.worktrees.resolve() or wt.is_symlink():
        return "worktree is not a task checkout"
    if not wt.resolve().is_relative_to(project.worktrees.resolve()):
        return "worktree is outside the project's worktrees directory"
    try:
        if not wt.exists():
            sh(["git", "worktree", "prune"], project.path)
            return None
        if sh(["git", "status", "--porcelain", "--untracked-files=all"], wt).strip():
            return "uncommitted work"
        if sh(["git", "symbolic-ref", "HEAD"], wt).strip() != f"refs/heads/{branch}":
            return "worktree is not on the recorded task branch"
        head = sh(["git", "rev-parse", "HEAD"], wt).strip()
        def reachable(ref):
            return subprocess.run(
                ["git", "merge-base", "--is-ancestor", head, ref],
                cwd=project.path, capture_output=True).returncode == 0
        backed_up = reachable("refs/heads/main")
        if not backed_up:
            tip = _retirement_remote_tip(project, branch)
            backed_up = tip is not None and reachable(tip)
        if not backed_up:
            return "commits exist nowhere else (not confirmed on remote branch or main)"
        sh(["git", "worktree", "remove", str(wt)], project.path)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return f"worktree retirement refused: {error}"
    return None

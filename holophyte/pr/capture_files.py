"""Capture specs and capture output a local-capture candidate never pushes."""
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from holophyte.capture_playwright import TICKET
from holophyte.config.config_tables import merge_config
from holophyte.environment_git import factory_identity
from holophyte.isolation.isolation_clone import MAIN_REFS
from holophyte.loop.gates import InfraFailure, sh

SUFFIX = ".capture.ts"
OUTPUT = ".holophyte-capture"


def default_spec(command):
    words = shlex.split(command)
    defaults = [after if word == "--default" else word.removeprefix("--default=")
                for word, after in zip(words, words[1:] + [""])
                if word == "--default" or word.startswith("--default=")]
    return defaults[-1] if defaults else None


def is_capture_file(path, capture_dir, default):
    path = PurePosixPath(path)
    if path.name.endswith(SUFFIX) and TICKET.match(path.name[:-len(SUFFIX)]):
        if default is None or path != PurePosixPath(default):
            return True
    if path.is_relative_to(PurePosixPath(capture_dir)):
        return True
    top = path.parts[0]
    return len(path.parts) > 1 and (top == OUTPUT or top.startswith(OUTPUT + "-"))


def added_capture_files(project, repo, ref):
    cfg = merge_config(project)
    if not cfg.ui_capture_local or not cfg.ui_capture.strip():
        return []
    base = next((b for b in MAIN_REFS if subprocess.run(
        ["git", "rev-parse", "--verify", "-q", b], cwd=repo,
        capture_output=True).returncode == 0), None)
    if base is None:
        raise InfraFailure(f"neither {' nor '.join(MAIN_REFS)} resolves in {repo};"
                           " the candidate's capture files cannot be listed")
    added = sh(["git", "diff", "--name-only", "--no-renames", "--diff-filter=A",
                "-z", f"{base}...{ref}"], repo).split("\0")
    default = default_spec(cfg.ui_capture)
    return [p for p in added
            if p and is_capture_file(p, cfg.ui_capture_dir, default)]


def checkout_of(repo, branch):
    listing = sh(["git", "worktree", "list", "--porcelain"], repo)
    worktree = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            worktree = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return Path(worktree)
    return None


def untrack_capture_files(project, branch):
    paths = added_capture_files(project, project.path, f"refs/heads/{branch}")
    if not paths:
        return
    wt = checkout_of(project.path, branch)
    if wt is None:
        raise InfraFailure(f"{branch} adds capture files ({', '.join(paths)})"
                           " and no worktree has it checked out to untrack"
                           " them; branch preserved, not pushed")
    old = sh(["git", "rev-parse", f"refs/heads/{branch}"], wt)
    with tempfile.TemporaryDirectory() as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch, "index"))}
        subprocess.run(["git", "read-tree", old], cwd=wt, env=env, check=True)
        subprocess.run(["git", "rm", "-q", "--cached", "--", *paths], cwd=wt,
                       env=env, check=True, capture_output=True)
        tree = subprocess.run(["git", "write-tree"], cwd=wt, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
    message = ("Untrack capture files before the push\n\n"
               + "\n".join(paths) + "\n")
    new = subprocess.run(
        ["git", *factory_identity(wt), "commit-tree", tree, "-p", old],
        cwd=wt, input=message, check=True, capture_output=True,
        text=True).stdout.strip()
    sh(["git", "rm", "-q", "--cached", "-f", "--ignore-unmatch", "--", *paths], wt)
    sh(["git", "update-ref", f"refs/heads/{branch}", new, old], wt)
    _exclude(wt, paths)


def _exclude(wt, paths):
    exclude = Path(wt) / sh(["git", "rev-parse", "--git-path", "info/exclude"], wt)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    present = (set(exclude.read_text(encoding="utf-8").splitlines())
               if exclude.exists() else set())
    lines = [line for line in ("/" + re.sub(r"([\\*?\[!#])", r"\\\1", p)
                               for p in paths) if line not in present]
    if lines:
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write("\n" + "\n".join(lines) + "\n")

import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from holophyte.agents.review_workspace import scratch_git_environment
from holophyte.files import copy_tree
from holophyte.loop.gates import sh

CLONE_TIMEOUT_S = 600
WORKTREE_EXCLUDE = "scout-sources-exclude"


class Source(NamedTuple):
    name: str
    url: str
    ref: str


class Fetched(NamedTuple):
    name: str
    url: str
    ref: str
    path: Path
    commit: str


class SourceUnfetched(RuntimeError):
    pass


def clone(source, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = ["git", "-c", "init.templateDir=", "-c", "protocol.ext.allow=never",
            "clone", "--quiet", "--depth", "1", "--branch", source.ref, "--",
            source.url, str(path)]
    try:
        cloned = subprocess.run(argv, env=dict(scratch_git_environment(),
                                               GIT_TERMINAL_PROMPT="0"),
                                capture_output=True, text=True,
                                stdin=subprocess.DEVNULL,
                                timeout=CLONE_TIMEOUT_S)
        failure = cloned.stderr.strip() if cloned.returncode else None
    except (OSError, subprocess.TimeoutExpired) as error:
        failure = str(error)
    if failure is not None:
        shutil.rmtree(path, ignore_errors=True)
        raise SourceUnfetched(
            f"source {source.name}: ref {source.ref} could not be fetched "
            f"from {source.url}: {failure or 'git clone failed'}")
    for key, value in (("url", source.url), ("ref", source.ref)):
        sh(["git", "config", f"scout.{key}", value], cwd=path,
           env=scratch_git_environment())


def fetch_sources(directory, declared, fresh=False):
    """Clones each source once; a kept clone keeps its cloned url and ref."""
    env = scratch_git_environment()
    fetched = {}
    for source in declared.values():
        path = directory / source.name
        if fresh:
            shutil.rmtree(path, ignore_errors=True)
        if not (path / ".git").is_dir():
            clone(source, path)
        url, ref = (sh(["git", "config", "--get", f"scout.{key}"], cwd=path,
                       env=env) for key in ("url", "ref"))
        commit = sh(["git", "rev-parse", "HEAD"], cwd=path, env=env)
        fetched[source.name] = Fetched(source.name, url, ref, path, commit)
    return fetched


def sources_summary(fetched):
    return "sources: " + ", ".join(
        f"{source.name} at {source.ref} ({source.commit[:12]})"
        for source in fetched.values())


def sources_payload(fetched):
    return {source.name: {"url": source.url, "ref": source.ref,
                          "commit": source.commit}
            for source in fetched.values()}


def top_git(top):
    return lambda directory, _names: [".git"] if directory == top else []


def place_sources(checkout, fetched):
    """Copies each source's tree to NAME/ and keeps it out of git status."""
    if not fetched:
        return
    env = scratch_git_environment()
    for source in fetched.values():
        target = checkout / source.name
        if target.exists() or target.is_symlink():
            raise SourceUnfetched(
                f"source {source.name}: the checkout already holds "
                f"{source.name}")
        copy_tree(source.path, target, symlinks=True,
                  ignore=top_git(str(source.path)))
    git_dir = Path(sh(["git", "rev-parse", "--absolute-git-dir"],
                      cwd=checkout, env=env))
    common = Path(sh(["git", "rev-parse", "--path-format=absolute",
                      "--git-common-dir"], cwd=checkout, env=env))
    lines = "".join(f"/{name}/\n" for name in fetched)
    if git_dir.resolve() == common.resolve():
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        held = exclude.read_text() if exclude.is_file() else ""
        exclude.write_text(held + ("\n" if held and not held.endswith("\n")
                                   else "") + lines)
        return
    # A linked worktree reads info/exclude from the shared repository.
    exclude = git_dir / WORKTREE_EXCLUDE
    exclude.write_text(lines)
    sh(["git", "config", "--worktree", "core.excludesFile", str(exclude)],
       cwd=checkout, env=env)

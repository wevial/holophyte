"""Disposable turn checkout and a config-free, fast-forward-only return path."""

import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from holophyte.environment_git import protected, refuse_environment_history
from holophyte.gates import InfraFailure
from holophyte.isolation_git import copy_merge_state, git, head, import_objects
from holophyte.target import state_dir


def copy_files(source, destination, protect):
    """Mirror working files without following links or copying Git/environment state."""
    excluded = {".git", ".env"} if protect else {".git"}
    for path in destination.iterdir():
        if path.name not in excluded:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    for path in source.iterdir():
        if path.name in excluded:
            continue
        dest = destination / path.name
        if path.is_dir() and not path.is_symlink():
            shutil.copytree(
                path, dest, symlinks=True, ignore=shutil.ignore_patterns(".git")
            )
        elif path.is_symlink():
            dest.symlink_to(path.readlink())
        else:
            shutil.copy2(path, dest)


def return_turn(worktree, clone, root, old, target, merge_state):
    # Never run Git against the untrusted clone: upload-pack also reads config.
    # Build a bare transport containing only validated objects and its HEAD.
    transport = root / "return.git"
    git(worktree, "-c", "init.templateDir=", "init", "--bare", str(transport))
    sha = head(clone / ".git")
    import_objects(clone / ".git/objects", transport / "objects")
    (transport / "HEAD").write_text(sha + "\n")
    hooks = root / "empty-hooks"
    hooks.mkdir()
    git(
        worktree,
        "-c",
        "protocol.file.allow=always",
        "-c",
        f"core.hooksPath={hooks}",
        "fetch",
        "--no-tags",
        str(transport),
        "HEAD",
    )
    try:
        git(worktree, "merge-base", "--is-ancestor", old, sha)
    except subprocess.CalledProcessError as error:
        raise InfraFailure(
            "container history is not a fast-forward; refusing fetch back"
        ) from error
    if target is not None:
        refuse_environment_history(
            target, sha, action="import container commits", commit=sha
        )
    if git(worktree, "rev-parse", "HEAD") != old:
        raise InfraFailure(
            "task branch changed during container turn; refusing fast-forward"
        )
    git(worktree, "update-ref", "HEAD", sha, old)
    git(worktree, "reset", "--mixed", sha)
    copy_files(clone, worktree, target is not None and protected(target))
    if sha != old:
        for path in merge_state:
            path.unlink(missing_ok=True)


@contextlib.contextmanager
def turn_clone(worktree, target=None):
    worktree = Path(worktree).resolve()
    common = Path(
        git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    state = state_dir(target.path if target is not None else common.parent)
    state.mkdir(parents=True, exist_ok=True)
    old = git(worktree, "rev-parse", "HEAD")
    env = {"GIT_CONFIG_COUNT": "3"}
    for i, (key, value) in enumerate(
        [
            ("safe.directory", "/workspace"),
            ("user.name", git(worktree, "config", "--get", "user.name")),
            ("user.email", git(worktree, "config", "--get", "user.email")),
        ]
    ):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    with tempfile.TemporaryDirectory(prefix="implementer-", dir=state) as directory:
        root = Path(directory)
        clone = root / "clone"
        git(
            worktree,
            "-c",
            "init.templateDir=",
            "clone",
            "--no-hardlinks",
            "--dissociate",
            "--single-branch",
            "--no-checkout",
            str(worktree),
            str(clone),
        )
        git(clone, "remote", "remove", "origin")
        index = Path(
            git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index")
        )
        if index.exists():
            shutil.copyfile(index, clone / ".git/index")
        merge_state = copy_merge_state(worktree, clone)
        copy_files(worktree, clone, target is not None and protected(target))
        yield clone, env
        return_turn(worktree, clone, root, old, target, merge_state)

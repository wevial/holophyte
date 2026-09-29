"""Disposable turn checkout and a config-free, fast-forward-only return path."""

import contextlib
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import review_runner
from holophyte.config.project import state_dir
from holophyte.environment_git import protected, refuse_environment_history
from holophyte.isolation.isolation_git import (
    copy_merge_state,
    git,
    git_environment,
    head,
    import_objects,
)
from holophyte.isolation.isolation_return import locked_return
from holophyte.loop.gates import InfraFailure


def stage_files(source, destination, excluded, skip=frozenset()):
    """Copy only ordinary files, directories and links; never follow links."""
    for path in source.iterdir():
        if path.name in excluded or path in skip:
            continue
        if path.name == ".git":
            raise InfraFailure("refusing working files containing nested .git")
        dest = destination / path.name
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            dest.symlink_to(path.readlink())
        elif stat.S_ISDIR(mode):
            dest.mkdir()
            stage_files(path, dest, set(), skip)
            shutil.copystat(path, dest)
        elif stat.S_ISREG(mode):
            shutil.copy2(path, dest)
        else:
            raise InfraFailure(
                f"refusing working files containing special file: {path}"
            )


def replace_files(staged, destination, excluded, finish):
    """Keep originals on the same filesystem until every replacement succeeds."""
    backup = Path(tempfile.mkdtemp(prefix=".backup-", dir=destination))
    moves = []
    try:
        for source, project in ((destination, backup), (staged, destination)):
            for path in source.iterdir():
                if path.name not in excluded and path not in (staged, backup):
                    replacement = project / path.name
                    path.rename(replacement)
                    moves.append((path, replacement))
        finish()
    except BaseException:
        try:
            for original, moved in reversed(moves):
                moved.rename(original)
        except OSError as error:
            # Do not clean up the only surviving copies if rollback also fails.
            raise InfraFailure(
                f"cannot restore working files; originals retained at {backup}: {error}"
            ) from error
        shutil.rmtree(backup)
        raise
    shutil.rmtree(backup)


@contextlib.contextmanager
def keeping(destination, staged, carry):
    moved = []
    try:
        for entry in carry:
            kept = destination / entry
            if not (kept.is_dir() or kept.is_symlink()):
                continue
            parent = (staged / entry).parent
            if parent.resolve() != staged.resolve() / Path(entry).parent:
                raise InfraFailure(f"[worktree] carry: {entry!r} has a linked parent "
                                   "in the working files")
            parent.mkdir(parents=True, exist_ok=True)
            kept.rename(staged / entry)
            moved.append(entry)
        yield
    except BaseException as failure:
        try:
            for entry in reversed(moved):
                (staged / entry).rename(destination / entry)
        except OSError as error:
            raise InfraFailure(f"{failure}; [worktree] carry directories kept at "
                               f"{staged}: {error}") from error
        raise


def remove_staging(staged):
    for directory, _, _ in os.walk(staged):
        os.chmod(directory, stat.S_IRWXU)
    shutil.rmtree(staged)


def copy_files(source, destination, protect, finish=lambda: None, carry=()):
    """Stage the complete copy and roll back failed destination mutations."""
    excluded = {".git", ".env"} if protect else {".git"}
    staged = Path(tempfile.mkdtemp(prefix=".copy-", dir=destination))
    try:
        try:
            stage_files(source, staged, excluded,
                        {source / entry for entry in carry})
        except (OSError, shutil.Error) as error:
            raise InfraFailure(f"cannot prepare working files: {error}") from error
        try:
            with keeping(destination, staged, carry):
                replace_files(staged, destination, excluded, finish)
        except (OSError, shutil.Error) as error:
            raise InfraFailure(f"cannot replace working files: {error}") from error
    finally:
        held = [entry for entry in carry
                if (staged / entry).is_symlink() or (staged / entry).exists()]
        if not held:
            remove_staging(staged)


def linked_carry(worktree, entry):
    if subprocess.run(["git", "ls-files", "--error-unmatch", "--", entry],
                      cwd=worktree, env=git_environment(),
                      capture_output=True).returncode == 0:
        raise InfraFailure(f"[worktree] carry: {entry!r} is tracked in git")
    resolved = (worktree / entry).resolve()
    if not resolved.is_dir():
        raise InfraFailure(f"[worktree] carry: {entry!r} is a link to no directory")
    listing = git(worktree, "worktree", "list", "--porcelain").splitlines()
    for line in listing:
        other = line.startswith("worktree ") and Path(line[9:]).resolve()
        if other and other != worktree and resolved == other / entry:
            review_runner.check_carry(other, entry)
            return
    raise InfraFailure(
        f"[worktree] carry: {entry!r} is a link that leaves the repository")


def carry_mounts(worktree, carry):
    mounted = []
    for entry in carry:
        path = worktree / entry
        if path.parent.resolve() != path.parent:
            raise InfraFailure(f"[worktree] carry: {entry!r} escapes the repository")
        try:
            if path.is_symlink():
                linked_carry(worktree, entry)
                mounted.append(entry)
                continue
            review_runner.check_carry(worktree, entry)
        except review_runner.ReviewBoundaryError as error:
            raise InfraFailure(str(error)) from error
        if path.exists() and not path.is_dir():
            raise InfraFailure(
                f"[worktree] carry: {entry!r} is not a directory in the worktree "
                f"{worktree}")
        path.mkdir(parents=True, exist_ok=True)
        mounted.append(entry)
    return mounted


def return_turn(worktree, clone, root, old, project, merge_state, carry):
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
    if project is not None:
        refuse_environment_history(
            project, sha, action="import container commits", commit=sha
        )
    if git(worktree, "rev-parse", "HEAD") != old:
        raise InfraFailure(
            "task branch changed during container turn; refusing fast-forward"
        )
    with locked_return(worktree, root, old, sha) as finish:
        copy_files(clone, worktree, project is not None and protected(project),
                   finish, carry)
        if sha != old:
            for path in merge_state:
                path.unlink(missing_ok=True)


@contextlib.contextmanager
def turn_clone(worktree, project=None, carry=()):
    worktree = Path(worktree).resolve()
    common = Path(
        git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )
    state = state_dir(project.path if project is not None else common.parent)
    state.mkdir(parents=True, exist_ok=True)
    old = git(worktree, "rev-parse", "HEAD")
    identity = {key: git(worktree, "config", "--get", key)
                for key in ("user.name", "user.email")}
    env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
           "GIT_CONFIG_VALUE_0": "/workspace"}
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
        for key, value in identity.items():
            git(clone, "config", key, value)
        index = Path(
            git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index")
        )
        if index.exists():
            shutil.copyfile(index, clone / ".git/index")
        merge_state = copy_merge_state(worktree, clone)
        copy_files(worktree, clone, project is not None and protected(project),
                   carry=carry)
        for entry in carry:
            (clone / entry).mkdir(parents=True, exist_ok=True)
        try:
            yield clone, env
        except subprocess.TimeoutExpired:
            # The runner has stopped: preserve its work for the loop's WIP path.
            return_turn(worktree, clone, root, old, project, merge_state, carry)
            raise
        else:
            return_turn(worktree, clone, root, old, project, merge_state, carry)

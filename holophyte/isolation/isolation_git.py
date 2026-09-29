"""Self-contained Git metadata for a worktree-only container mount.

Only objects, HEAD and the index return; container-written config and hooks
never execute on the host. The original Git link/config stays outside the mount.
"""

import contextlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


def git_environment():
    """Discard repository selectors inherited from hooks or the caller's shell."""
    excluded = {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_IMPLICIT_WORK_TREE", "GIT_PREFIX",
    }
    return {key: value for key, value in os.environ.items() if key not in excluded}


def git(worktree, *args, index=None):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=worktree,
        env={**git_environment(), **({"GIT_INDEX_FILE": str(index)} if index else {})},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def regular(path):
    return path.exists() and stat.S_ISREG(path.lstat().st_mode)


def head(metadata):
    value = (
        (metadata / "HEAD").read_text().strip() if regular(metadata / "HEAD") else ""
    )
    if value.startswith("ref: "):
        ref = value[5:]
        if not re.fullmatch(r"refs/heads/[\w./-]+", ref) or ".." in ref:
            raise RuntimeError("invalid container branch ref")
        path = metadata
        for part in ref.split("/"):
            path /= part
            if path.is_symlink():
                raise RuntimeError("symlink in container branch ref")
        value = path.read_text().strip() if regular(path) else packed_ref(metadata, ref)
    if not re.fullmatch("[0-9a-f]{40}", value):
        raise RuntimeError("invalid container HEAD")
    return value


def packed_ref(metadata, ref):
    packed = metadata / "packed-refs"
    if regular(packed):
        for line in packed.read_text().splitlines():
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return ""


def atomic_copy(source, destination):
    """Publish complete Git files atomically on the destination filesystem."""
    mode_source = destination if destination.exists() else source
    mode = stat.S_IMODE(mode_source.stat().st_mode)
    descriptor, name = tempfile.mkstemp(
        prefix=".holophyte-copy-", dir=destination.parent
    )
    temporary = Path(name)
    try:
        os.close(descriptor)
        shutil.copyfile(source, temporary)
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def copy_merge_state(worktree, clone):
    """Carry only validated pending-merge metadata into the disposable repository."""
    metadata = Path(git(worktree, "rev-parse", "--absolute-git-dir"))
    paths = [metadata / name for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE")]
    for path in paths:
        if (path.exists() or path.is_symlink()) and not regular(path):
            raise RuntimeError(f"invalid pending merge file: {path.name}")
    if not paths[0].exists():
        return []
    parents = paths[0].read_text().splitlines()
    if not parents or any(not re.fullmatch("[0-9a-f]{40}", sha) for sha in parents):
        raise RuntimeError("invalid pending MERGE_HEAD")
    for sha in parents:
        git(clone, "cat-file", "-e", sha + "^{commit}")
    if paths[2].exists() and paths[2].read_text().strip() not in ("", "no-ff"):
        raise RuntimeError("invalid pending MERGE_MODE")
    for path in paths:
        if path.exists():
            shutil.copyfile(path, clone / ".git" / path.name)
    return paths


def import_objects(source, destination):
    """Copy only ordinary Git object files, never config, links or hooks."""
    if source.is_symlink():
        raise RuntimeError("container object directory is a symlink")
    for directory in source.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            continue
        if not re.fullmatch(r"[0-9a-f]{2}|pack", directory.name):
            continue
        dest = destination / directory.name
        dest.mkdir(exist_ok=True)
        for path in directory.iterdir():
            pattern = (
                r"pack-[0-9a-f]{40}\.(pack|idx|rev)"
                if directory.name == "pack"
                else r"[0-9a-f]{38}"
            )
            if re.fullmatch(pattern, path.name) and regular(path):
                if not (dest / path.name).exists():
                    atomic_copy(path, dest / path.name)


@contextlib.contextmanager
def isolated_git(worktree):
    entry = worktree / ".git"
    if not entry.exists():
        yield {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": "/workspace",
        }
        return
    old = git(worktree, "rev-parse", "HEAD")
    author = [
        (key, git(worktree, "config", "--get", key))
        for key in ("user.name", "user.email")
    ]
    env = {"GIT_CONFIG_COUNT": "3"}
    for i, (key, value) in enumerate([("safe.directory", "/workspace"), *author]):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    with tempfile.TemporaryDirectory(
        prefix=".holophyte-implement-git-", dir=worktree.resolve().parent
    ) as temporary:
        root = Path(temporary)
        clone = root / "clone"
        git(
            worktree,
            "-c",
            "init.templateDir=",
            "clone",
            "--single-branch",
            "--no-tags",
            "--quiet",
            "--no-hardlinks",
            "--dissociate",
            "--no-checkout",
            str(worktree.resolve()),
            str(clone),
        )
        git(clone, "remote", "remove", "origin")
        index = Path(
            git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index")
        )
        if index.exists():
            shutil.copyfile(index, clone / ".git" / "index")
        merge_state = copy_merge_state(worktree, clone)
        entry.rename(root / "original")
        try:
            (clone / ".git").rename(entry)
            yield env
        finally:
            # Restore the host metadata even when the turn damages its Git directory.
            if entry.exists() or entry.is_symlink():
                entry.rename(root / "result")
            (root / "original").rename(entry)
            result = root / "result"
            if result.is_dir() and not result.is_symlink():
                sha = head(result)
                objects = Path(
                    git(
                        worktree,
                        "rev-parse",
                        "--path-format=absolute",
                        "--git-path",
                        "objects",
                    )
                )
                import_objects(result / "objects", objects)
                git(worktree, "cat-file", "-e", sha + "^{commit}")
                git(worktree, "update-ref", "HEAD", sha, old)
                if (result / "index").exists() and regular(result / "index"):
                    atomic_copy(result / "index", index)
                if sha != old and not (result / "MERGE_HEAD").exists():
                    for path in merge_state:
                        path.unlink(missing_ok=True)

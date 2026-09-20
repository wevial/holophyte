"""Self-contained Git metadata for a worktree-only container mount.

Only objects, HEAD and the index return; container-written config and hooks
never execute on the host. The original Git link/config stays outside the mount.
"""

import contextlib
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


def git(worktree, *args):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=worktree,
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
                    shutil.copyfile(path, dest / path.name)


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
                    shutil.copyfile(result / "index", index)

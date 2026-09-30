from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from holophyte.loop.gates import run_capped

GIT_TIMEOUT = 30
MAX_FILES = 200
# The same filter on both diffs, so they name the same paths.
DIFF_FILTER = "ADMR"
MAIN = "main"


class RangeError(Exception):
    pass


@dataclass(frozen=True)
class TouchedFile:
    path: str
    status: str
    added: int
    deleted: int


@dataclass(frozen=True)
class TouchedFiles:
    """The totals cover every file the diff named, not only the ones listed."""
    base: str
    head: str
    files: list[TouchedFile] = field(default_factory=list)
    total_added: int = 0
    total_deleted: int = 0
    truncated: bool = False


def git(repo, *args, timeout=GIT_TIMEOUT):
    return run_capped(["git", *args], repo, timeout)


def resolve(repo, rev, missing):
    """A branch is named as `refs/heads/NAME`: a bare name falls through to a tag."""
    code, out = git(repo, "rev-parse", "--verify", "--quiet",
                    f"{rev}^{{commit}}")
    if code != 0:
        raise RangeError(missing)
    return out.strip()


def run_range(repo, branch, merge_sha):
    """A recorded merge wins: the branch may be deleted or moved after landing."""
    if merge_sha:
        head = resolve(repo, merge_sha,
                       f"merge commit {merge_sha} is not in the repository")
        base = resolve(repo, f"{head}^1",
                       f"merge commit {merge_sha} has no first parent")
        return base, head
    if branch:
        head = resolve(repo, f"refs/heads/{branch}",
                       f"branch {branch} no longer exists in the repository")
        code, out = git(repo, "merge-base", f"refs/heads/{MAIN}", head)
        if code != 0:
            raise RangeError(f"branch {branch} shares no history with {MAIN}")
        return out.strip(), head
    raise RangeError("the run recorded neither a branch nor a merge commit")


def worktree_range(worktree):
    code, out = git(worktree, "rev-parse", "--verify", "--quiet", "HEAD")
    if code != 0:
        raise RangeError(f"worktree {worktree} has no HEAD")
    head = out.strip()
    code, out = git(worktree, "merge-base", f"refs/heads/{MAIN}", head)
    if code != 0:
        raise RangeError(f"worktree {worktree} shares no history with {MAIN}")
    return out.strip(), head


def untracked_files(worktree, timeout=GIT_TIMEOUT):
    code, out = git(worktree, "status", "--porcelain", "-z",
                    "--untracked-files=all", timeout=timeout)
    if code != 0:
        raise RuntimeError(f"git status in {worktree} failed with {code}:\n{out}")
    counts = {}
    fields = out.split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        entry = fields[i]
        i += 1
        if entry[:2] == "??":
            counts[entry[3:]] = (count_lines(Path(worktree) / entry[3:]), 0)
        elif entry[0] == "R" or entry[1] == "R":
            # A staged rename carries its source as the next record.
            i += 1
    return counts


def count_lines(path):
    """A symlink is its link text, never followed: its target may be a FIFO."""
    try:
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode):
            data = os.readlink(path).encode("utf-8", "surrogateescape")
        elif stat.S_ISREG(mode):
            data = path.read_bytes()
        else:
            return 0
    except OSError:
        return 0
    if not data or b"\0" in data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def parse_numstat(text):
    counts = {}
    fields = text.split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        added, deleted, path = fields[i].split("\t", 2)
        if path == "":
            # A rename: the old and new names are the next two fields.
            path = fields[i + 2]
            i += 2
        counts[path] = (int(added) if added != "-" else 0,
                        int(deleted) if deleted != "-" else 0)
        i += 1
    return counts


def parse_name_status(text):
    statuses = {}
    fields = text.split("\0")
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i][0]
        path = fields[i + 1]
        i += 2
        if status == "R":
            path = fields[i]
            i += 1
        statuses[path] = status
    return statuses


def touched_files(repo, branch, merge_sha, worktree=None, cap=None,
                  timeout=GIT_TIMEOUT):
    cap = MAX_FILES if cap is None else cap
    live = (not merge_sha and branch and worktree is not None
            and Path(worktree).is_dir())
    if live:
        cwd = worktree
        base, head = worktree_range(worktree)
        # No `head`: the diff runs on to the working tree.
        range_args = (base,)
    else:
        cwd = repo
        base, head = run_range(repo, branch, merge_sha)
        range_args = (base, head)
    outputs = []
    for mode in ("--numstat", "--name-status"):
        code, out = git(cwd, "diff", mode, "-z", f"--diff-filter={DIFF_FILTER}",
                        *range_args, timeout=timeout)
        if code != 0:
            raise RuntimeError(f"git diff {mode} {base}..{head} failed"
                               f" with {code}:\n{out}")
        outputs.append(out)
    counts = parse_numstat(outputs[0])
    statuses = parse_name_status(outputs[1])
    if live:
        for path, added in untracked_files(worktree, timeout=timeout).items():
            counts[path] = added
            statuses[path] = "A"
    files = [TouchedFile(path=path, status=statuses.get(path, "M"),
                         added=added, deleted=deleted)
             for path, (added, deleted) in sorted(counts.items())]
    return TouchedFiles(
        base=base, head=head, files=files[:cap],
        total_added=sum(f.added for f in files),
        total_deleted=sum(f.deleted for f in files),
        truncated=len(files) > cap)

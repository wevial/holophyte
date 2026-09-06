"""The files a run touched, read from git: what `GET /runs/N/files` answers.

The store records a run's branch and, once it merged, the merge commit on
main; git holds which paths changed and by how many lines. This module
turns the two store columns into a commit range and asks the target's
checkout -- never a task worktree -- for `git diff --numstat` and
`git diff --name-status` over it, merged by path. It runs git through
`gates.run_capped()` so a wedged repository cannot hang the daemon: every
git call is under `GIT_TIMEOUT` and dies with its process group.

The range is the run's own history. A merged run (a recorded `mergeSha`)
is its merge commit's first parent to the merge commit: exactly what the
`--no-ff` landing added to main. Any other run with a branch is the merge
base of `main` and that branch to the branch head: what the branch has
that main does not, unaffected by what main gained since. `serve.py` maps
the outcomes here to HTTP: `RangeError` is its 409, a `TimeoutExpired` its
504.

Run the tests: python3 -m unittest discover -s tests -p 'test_serve*' -v
"""
from __future__ import annotations

from dataclasses import dataclass, field

from holophyte.gates import run_capped

# The cap on each git call. A diff over one branch is milliseconds; a repo
# on a stalled network mount is the case this exists for.
GIT_TIMEOUT = 30
# How many files the answer lists before `truncated` is set. Well past any
# one ticket's honest diff; a generated-tree commit is the case it caps.
MAX_FILES = 200
# The status letters served. `--diff-filter` with the same set on both
# diffs so they name the same paths; anything else (copies, type changes)
# is out of the run's story.
DIFF_FILTER = "ADMR"
MAIN = "main"


class RangeError(Exception):
    """No commit range can be found for the run: it has neither a branch
    nor a merge sha, or the ref it names no longer exists."""


@dataclass(frozen=True)
class TouchedFile:
    path: str
    status: str
    added: int
    deleted: int


@dataclass(frozen=True)
class TouchedFiles:
    """The answer: the range as full shas and the files sorted by path,
    with the totals over every file the diff named, not only the ones
    listed -- a truncated list still says how big the run was."""

    base: str
    head: str
    files: list[TouchedFile] = field(default_factory=list)
    total_added: int = 0
    total_deleted: int = 0
    truncated: bool = False


def git(repo, *args, timeout=GIT_TIMEOUT):
    """`git ARGS` in `repo` under the cap: `(returncode, output)`, output
    being stdout and stderr together as `run_capped()` captures them."""
    return run_capped(["git", *args], repo, timeout)


def resolve(repo, rev, missing):
    """`rev` as a full commit sha, or `RangeError(missing)` when git cannot
    resolve it -- the branch was deleted by hand, the merge is gone.

    A branch is named as `refs/heads/NAME`, never bare: a bare name falls
    through git's ref search to a same-name tag, so a deleted branch with
    a leftover tag would answer instead of raising.
    """
    code, out = git(repo, "rev-parse", "--verify", "--quiet",
                    f"{rev}^{{commit}}")
    if code != 0:
        raise RangeError(missing)
    return out.strip()


def run_range(repo, branch, merge_sha):
    """The `(base, head)` shas for a run from its store columns.

    A recorded merge wins over the branch: the branch may have been deleted
    after landing, or moved on, while the merge commit is what main holds.
    """
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


def parse_numstat(text):
    """`git diff --numstat -z` as `{path: (added, deleted)}`.

    Each record is `added TAB deleted TAB path NUL`, except a rename, which
    is `added TAB deleted TAB NUL old NUL new NUL` and is keyed by `new`.
    A binary file's counts are `-` and count as 0 and 0.
    """
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
    """`git diff --name-status -z` as `{path: letter}`.

    Each record is `status NUL path NUL`; a rename is `Rnnn NUL old NUL new
    NUL`, served as `R` under `new`. Only the first letter is kept.
    """
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


def touched_files(repo, branch, merge_sha, cap=None, timeout=GIT_TIMEOUT):
    """The `TouchedFiles` of a run, from its store columns and `repo`,
    listing the first `cap` files (`MAX_FILES` when None) by path.

    Raises `RangeError` when no range can be found and
    `subprocess.TimeoutExpired` when a git call outlives `timeout`; a git
    failure past that (a corrupt object, say) is a `RuntimeError` naming
    the command, since a diff over two resolved commits has no expected
    way to fail.
    """
    cap = MAX_FILES if cap is None else cap
    base, head = run_range(repo, branch, merge_sha)
    outputs = []
    for mode in ("--numstat", "--name-status"):
        code, out = git(repo, "diff", mode, "-z", f"--diff-filter={DIFF_FILTER}",
                        base, head, timeout=timeout)
        if code != 0:
            raise RuntimeError(f"git diff {mode} {base}..{head} failed"
                               f" with {code}:\n{out}")
        outputs.append(out)
    counts = parse_numstat(outputs[0])
    statuses = parse_name_status(outputs[1])
    files = [TouchedFile(path=path, status=statuses.get(path, "M"),
                         added=added, deleted=deleted)
             for path, (added, deleted) in sorted(counts.items())]
    return TouchedFiles(
        base=base, head=head, files=files[:cap],
        total_added=sum(f.added for f in files),
        total_deleted=sum(f.deleted for f in files),
        truncated=len(files) > cap)

"""What a revert may undo: the merged run, its files, its dependents."""
import json
import re
import subprocess
from typing import NamedTuple

MAIN = "origin/main"
OPEN_STATUSES = ("needs_spec", "ready", "in_flight", "blocked_on_deps",
                 "blocked_on_operator")


class Refused(Exception):
    pass


class Merge(NamedTuple):
    key: str
    title: str
    run_id: int
    sha: str
    files: tuple
    verify: tuple


class Dependent(NamedTuple):
    key: str
    sha: str
    paths: tuple


def git(repo, *args):
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True)
    return done.returncode, done.stdout


def changed_files(repo, sha):
    _, line = git(repo, "rev-list", "--parents", "-n", "1", sha)
    parents = line.split()[1:]
    if parents:
        _, names = git(repo, "diff", "--no-renames", "--name-only", "-z",
                       parents[0], sha)
    else:
        _, names = git(repo, "diff-tree", "--root", "-r", "--no-renames",
                       "--no-commit-id", "--name-only", "-z", sha)
    return tuple(name for name in names.split("\0") if name)


def merged_run(conn, repo, key):
    ticket = conn.execute(
        "SELECT id, title, verificationCommands FROM tickets"
        " WHERE linearIdentifier = ?", (key,)).fetchone()
    if ticket is None:
        raise Refused(f"{key}: no such ticket in the store; nothing to revert")
    row = conn.execute(
        "SELECT id, mergeSha FROM runs WHERE ticketId = ? AND outcome = 'merged'"
        " AND mergeSha IS NOT NULL ORDER BY id DESC LIMIT 1",
        (ticket[0],)).fetchone()
    if row is None:
        raise Refused(f"{key}: no run of it merged; nothing to revert")
    run_id, recorded = row
    code, full = git(repo, "rev-parse", "--verify", "-q",
                     f"{recorded}^{{commit}}")
    if code != 0:
        raise Refused(f"{key}: its merge {recorded} is not a commit in"
                      f" {repo}; nothing to revert")
    sha = full.strip()
    if git(repo, "merge-base", "--is-ancestor", sha, MAIN)[0] != 0:
        raise Refused(f"{key}: its merge {sha} is not on {MAIN}; nothing to"
                      " revert")
    return Merge(key, ticket[1], run_id, sha, changed_files(repo, sha),
                 tuple(json.loads(ticket[2] or "[]")))


def glob_pattern(glob):
    parts = re.split(r"(\*\*/|/\*\*$|\*\*|\*|\?)", glob)
    tokens = {"**/": "(?:.*/)?", "/**": "/.*", "**": ".*", "*": "[^/]*",
              "?": "[^/]"}
    return re.compile("".join(tokens.get(part, re.escape(part))
                              for part in parts))


def refused_paths(files, globs):
    patterns = [glob_pattern(glob) for glob in globs]
    return [path for path in files
            if any(pattern.fullmatch(path) for pattern in patterns)]


def open_revert(conn, merge):
    row = conn.execute(
        "SELECT linearIdentifier FROM tickets WHERE instr(body, ?) > 0"
        f" AND status IN ({', '.join('?' * len(OPEN_STATUSES))})"
        " AND COALESCE(boardColumn, '') != 'canceled'"
        " ORDER BY id DESC LIMIT 1",
        (f"Reverts: {merge.key} at ", *OPEN_STATUSES)).fetchone()
    return None if row is None else row[0]


def dependents(conn, repo, merge):
    _, listed = git(repo, "rev-list", "--reverse", f"{merge.sha}..{MAIN}")
    later = listed.split()
    by_sha = {}
    for key, sha in conn.execute(
            "SELECT t.linearIdentifier, r.mergeSha FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId"
            " WHERE r.outcome = 'merged' AND r.mergeSha IS NOT NULL"
            " AND t.linearIdentifier != ? ORDER BY r.id", (merge.key,)):
        code, full = git(repo, "rev-parse", "--verify", "-q",
                         f"{sha}^{{commit}}")
        if code == 0:
            by_sha[full.strip()] = key
    touched = set(merge.files)
    found = []
    for sha in later:
        if sha not in by_sha:
            continue
        shared = tuple(path for path in changed_files(repo, sha)
                       if path in touched)
        if shared:
            found.append(Dependent(by_sha[sha], sha, shared))
    return found


def dependent_lines(found):
    if not found:
        return ["none"]
    return [f"{one.key} {one.sha} {', '.join(one.paths)}" for one in found]

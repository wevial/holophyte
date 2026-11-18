"""What a revert may undo: the merged run, its files, its dependents."""
import json
import re
import subprocess
from typing import NamedTuple

MAIN = "origin/main"
LOCAL_MAIN = "main"
FILED_NOTE = re.compile(r"^filed (\S+): ")
OPEN_STATUSES = ("needs_spec", "ready", "in_flight", "blocked_on_deps",
                 "blocked_on_operator")


class Refused(Exception):
    pass


class Merge(NamedTuple):
    key: str
    title: str
    run_id: int
    sha: str
    parents: int
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


def commit_sha(repo, ref):
    code, full = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return full.strip() if code == 0 else None


def parent_shas(repo, sha):
    return git(repo, "rev-list", "--parents", "-n", "1", sha)[1].split()[1:]


def changed_files(repo, sha):
    parents = parent_shas(repo, sha)
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
    sha = commit_sha(repo, recorded)
    if sha is None:
        raise Refused(f"{key}: its merge {recorded} is not a commit in"
                      f" {repo}; nothing to revert")
    if git(repo, "merge-base", "--is-ancestor", sha, MAIN)[0] != 0:
        raise Refused(f"{key}: its merge {sha} is not on {MAIN}; nothing to"
                      " revert")
    return Merge(key, ticket[1], run_id, sha, len(parent_shas(repo, sha)),
                 changed_files(repo, sha),
                 tuple(json.loads(ticket[2] or "[]")))


def glob_part(part):
    tokens = {"**/": "(?:.*/)?", "/**": "/.*", "**": ".*", "*": "[^/]*",
              "?": "[^/]"}
    if part in tokens:
        return tokens[part]
    if part.startswith("[") and part.endswith("]") and len(part) > 2:
        negated = part[1] in "!^"
        members = part[1 + negated:-1].replace("\\", "\\\\")
        return f"(?!/)[{'^' if negated else ''}{members}]"
    return re.escape(part)


def glob_pattern(glob):
    parts = re.split(r"(\*\*/|/\*\*$|\*\*|\*|\?|\[!?\]?[^\]/]*\])", glob)
    return re.compile("".join(glob_part(part) for part in parts))


def refused_paths(files, globs):
    patterns = [glob_pattern(glob) for glob in globs]
    return [path for path in files
            if any(pattern.fullmatch(path) for pattern in patterns)]


def open_revert(conn, board, merge):
    row = conn.execute(
        "SELECT linearIdentifier FROM tickets WHERE substr(title, 1, ?) = ?"
        " AND instr(char(10) || body, ?) > 0"
        f" AND status IN ({', '.join('?' * len(OPEN_STATUSES))})"
        " AND COALESCE(boardColumn, '') != 'canceled'"
        " ORDER BY id DESC LIMIT 1",
        (len(title_prefix(merge.key)), title_prefix(merge.key),
         f"\nReverts: {merge.key} at ", *OPEN_STATUSES)).fetchone()
    if row is not None:
        return row[0]
    filed = [match[1] for (note,) in conn.execute(
        "SELECT i.note FROM interventions i JOIN runs r ON r.id = i.runId"
        " JOIN tickets t ON t.id = r.ticketId WHERE t.linearIdentifier = ?"
        " AND i.\"action\" = 'revert' ORDER BY i.id DESC", (merge.key,))
        if (match := FILED_NOTE.match(note or ""))]
    if not filed:
        return None
    states = board.states(filed)
    return next((key for key in filed
                 if states.get(key, {}).get("state") == "open"), None)


def title_prefix(key):
    return f"[Revert] Undo {key}: "


def dependents(conn, repo, merge):
    heads = [ref for ref in (MAIN, LOCAL_MAIN)
             if commit_sha(repo, ref) is not None]
    _, listed = git(repo, "rev-list", "--reverse", *heads, f"^{merge.sha}")
    if not listed.split():
        return []
    by_sha = {}
    for key, sha in conn.execute(
            "SELECT t.linearIdentifier, r.mergeSha FROM runs r"
            " JOIN tickets t ON t.id = r.ticketId"
            " WHERE r.outcome = 'merged' AND r.mergeSha IS NOT NULL"
            " AND t.linearIdentifier != ? ORDER BY r.id", (merge.key,)):
        full = commit_sha(repo, sha)
        if full is not None:
            by_sha[full] = key
    touched = set(merge.files)
    found = []
    for sha in listed.split():
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

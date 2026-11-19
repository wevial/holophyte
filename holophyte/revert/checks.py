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
    def __init__(self, reason, paths=()):
        super().__init__(reason)
        self.paths = tuple(paths)


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


def listed(repo, *args):
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True)
    if done.returncode != 0:
        raise Refused(f"git {args[0]} failed in {repo}, so the paths a"
                      f" revert would touch are unknown:"
                      f" {done.stderr.strip() or done.returncode}")
    return done.stdout


def fetch_origin(repo):
    if "origin" not in git(repo, "remote")[1].split():
        return None
    done = subprocess.run(["git", "fetch", "-q", "origin"], cwd=repo,
                          capture_output=True, text=True)
    return None if done.returncode == 0 else (done.stderr.strip()
                                              or str(done.returncode))


def commit_sha(repo, ref):
    code, full = git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return full.strip() if code == 0 else None


def parent_shas(repo, sha):
    return listed(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]


def changed_files(repo, sha):
    parents = parent_shas(repo, sha)
    if parents:
        names = listed(repo, "diff", "--no-renames", "--name-only", "-z",
                       "--ignore-submodules=none", parents[0], sha)
    else:
        names = listed(repo, "diff-tree", "--root", "-r", "--no-renames",
                       "--no-commit-id", "--name-only", "-z",
                       "--ignore-submodules=none", sha)
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


POSIX_CLASSES = {"alnum": "a-zA-Z0-9", "alpha": "a-zA-Z", "digit": "0-9",
                 "lower": "a-z", "upper": "A-Z", "space": " \\t\\n\\r\\f\\v",
                 "xdigit": "0-9a-fA-F"}
POSIX_CLASS = re.compile(r"\[:([a-z]+):\]")


def class_members(members):
    def posix(match):
        if match[1] not in POSIX_CLASSES:
            raise re.error(f"unknown character class [:{match[1]}:]")
        return POSIX_CLASSES[match[1]]
    escaped = re.sub(r"[\\\[]", lambda m: "\\" + m[0], members)
    return POSIX_CLASS.sub(posix, escaped.replace("\\[:", "[:"))


def any_char(_char):
    return True


def not_slash(char):
    return char != "/"


def glob_steps(part):
    tokens = {"**/": [("dirs", any_char)], "/**": [("one", "/".__eq__),
                                                   ("star", any_char)],
              "**": [("star", any_char)], "*": [("star", not_slash)],
              "?": [("one", not_slash)]}
    if part in tokens:
        return tokens[part]
    negated = part[1:2] in ("!", "^")
    if part.startswith("[") and part.endswith("]") and len(part) > 2 + negated:
        members = class_members(part[1 + negated:-1])
        member = re.compile(f"(?!/)[{'^' if negated else ''}{members}]",
                            re.DOTALL)
        return [("one", lambda char: member.fullmatch(char) is not None)]
    if part.startswith("\\") and len(part) == 2:
        return [("one", part[1].__eq__)]
    return [("one", char.__eq__) for char in part]


class Glob:
    def __init__(self, steps):
        self.moves, self.skips = [[]], [[]]
        for kind, test in steps:
            here = len(self.moves) - 1
            if kind == "dirs":
                inside = self.node()
                self.moves[here].append((test, inside))
                self.moves[inside].append((test, inside))
            after = self.node()
            if kind == "one":
                self.moves[here].append((test, after))
            elif kind == "star":
                self.moves[here].append((test, here))
                self.skips[here].append(after)
            else:
                self.skips[here].append(after)
                for start in (here, inside):
                    self.moves[start].append(("/".__eq__, after))

    def node(self):
        self.moves.append([])
        self.skips.append([])
        return len(self.moves) - 1

    def closure(self, states):
        pending = list(states)
        while pending:
            for after in self.skips[pending.pop()]:
                if after not in states:
                    states.add(after)
                    pending.append(after)
        return states

    def fullmatch(self, path):
        states = self.closure({0})
        for char in path:
            states = self.closure({after for state in states
                                   for test, after in self.moves[state]
                                   if test(char)})
            if not states:
                return False
        return len(self.moves) - 1 in states


def glob_pattern(glob):
    parts = re.split(r"(\\.|\*\*/|/\*\*$|\*\*|\*|\?"
                     r"|\[!?\]?(?:\[:[a-z]+:\]|[^\]/])*\])", glob)
    return Glob([step for part in parts for step in glob_steps(part)])


def refused_paths(files, globs):
    patterns = [glob_pattern(glob) for glob in globs]
    return [path for path in files
            if any(pattern.fullmatch(path) for pattern in patterns)]


def open_revert(conn, board, merge):
    mirrored = [key for (key,) in conn.execute(
        "SELECT linearIdentifier FROM tickets WHERE substr(title, 1, ?) = ?"
        " AND instr(char(10) || body, ?) > 0"
        f" AND status IN ({', '.join('?' * len(OPEN_STATUSES))})"
        " ORDER BY id DESC",
        (len(title_prefix(merge.key)), title_prefix(merge.key),
         f"\nReverts: {merge.key} at ", *OPEN_STATUSES))]
    filed = mirrored + [match[1] for (note,) in conn.execute(
        "SELECT i.note FROM interventions i JOIN runs r ON r.id = i.runId"
        " JOIN tickets t ON t.id = r.ticketId WHERE t.linearIdentifier = ?"
        " AND i.\"action\" = 'revert' ORDER BY i.id DESC", (merge.key,))
        if (match := FILED_NOTE.match(note or ""))]
    states = board.states(filed) if filed else {}
    opened = next((key for key in filed
                   if states.get(key, {}).get("state") == "open"), None)
    if opened is not None:
        return opened
    issues = getattr(board, "open_issues", board.listing)()
    return next((issue["id"] for issue in issues
                 if (issue.get("title") or "").startswith(
                     title_prefix(merge.key))), None)


def title_prefix(key):
    return f"[Revert] Undo {key}: "


def dependents(conn, repo, merge):
    heads = [ref for ref in (MAIN, LOCAL_MAIN)
             if commit_sha(repo, ref) is not None]
    later = listed(repo, "rev-list", "--reverse", "--ancestry-path", *heads,
                   f"^{merge.sha}").split()
    if not later:
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

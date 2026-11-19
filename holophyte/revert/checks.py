"""What a revert may undo: the merged run, its files, its dependents."""
import json
import re
import subprocess
from re import _constants as sre
from re import _parser as sre_parse
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


def glob_part(part):
    tokens = {"**/": "(?:.*/)?", "/**": "/.*", "**": ".*", "*": "[^/]*",
              "?": "[^/]"}
    if part in tokens:
        return tokens[part]
    if part.startswith("[") and part.endswith("]") and len(part) > 2:
        negated = part[1] in "!^"
        members = class_members(part[1 + negated:-1])
        return f"(?!/)[{'^' if negated else ''}{members}]"
    if part.startswith("\\") and len(part) == 2:
        return re.escape(part[1])
    return re.escape(part)


class Unwalkable(Exception):
    pass


def char_test(op, arg):
    if op is sre.LITERAL:
        return lambda char: ord(char) == arg
    if op is sre.NOT_LITERAL:
        return lambda char: ord(char) != arg
    if op is sre.ANY:
        return lambda _char: True
    if op is not sre.IN:
        raise Unwalkable(op)
    negated = bool(arg) and arg[0][0] is sre.NEGATE
    singles, spans = set(), []
    for kind, value in arg[negated:]:
        if kind is sre.LITERAL:
            singles.add(value)
        elif kind is sre.RANGE:
            spans.append(value)
        else:
            raise Unwalkable(kind)
    return lambda char: (ord(char) in singles or any(
        low <= ord(char) <= high for low, high in spans)) != negated


class Glob:
    def __init__(self, regex):
        self.regex = re.compile(regex, re.DOTALL)
        self.moves, self.skips = [[]], [[]]
        try:
            self.end = self.build(sre_parse.parse(regex, re.DOTALL), 0)
        except Unwalkable:
            self.end = None

    def node(self):
        self.moves.append([])
        self.skips.append([])
        return len(self.moves) - 1

    def skip(self, here, after, guard=None):
        self.skips[here].append((guard, after))

    def build(self, items, here):
        for op, arg in items:
            here = self.step(op, arg, here)
        return here

    def step(self, op, arg, here):
        if op is sre.SUBPATTERN:
            return self.build(arg[-1], here)
        if op is sre.MAX_REPEAT:
            low, high, body = arg
            for _ in range(low):
                here = self.build(body, here)
            if high == sre.MAXREPEAT:
                loop = self.node()
                self.skip(here, loop)
                self.skip(self.build(body, loop), loop)
                return loop
            for _ in range(high - low):
                after = self.node()
                self.skip(here, after)
                self.skip(self.build(body, here), after)
                here = after
            return here
        if op is sre.ASSERT_NOT and arg[0] == 1 and len(arg[1]) == 1:
            test = char_test(*arg[1][0])
            after = self.node()
            self.skip(here, after, lambda ahead: ahead is None
                      or not test(ahead))
            return after
        test = char_test(op, arg)
        after = self.node()
        self.moves[here].append((test, after))
        return after

    def closure(self, states, ahead):
        pending = list(states)
        while pending:
            for guard, after in self.skips[pending.pop()]:
                if after not in states and (guard is None or guard(ahead)):
                    states.add(after)
                    pending.append(after)
        return states

    def fullmatch(self, path):
        if self.end is None:
            return self.regex.fullmatch(path) is not None
        states = self.closure({0}, path[:1] or None)
        for at, char in enumerate(path):
            states = self.closure({after for state in states
                                   for test, after in self.moves[state]
                                   if test(char)}, path[at + 1:at + 2] or None)
            if not states:
                return False
        return self.end in states


def glob_pattern(glob):
    parts = re.split(r"(\\.|\*\*/|/\*\*$|\*\*|\*|\?"
                     r"|\[!?\]?(?:\[:[a-z]+:\]|[^\]/])*\])", glob)
    return Glob("".join(glob_part(part) for part in parts))


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

import hashlib
import re
import subprocess
from pathlib import Path

import store
import ticket_header
import ticket_template
from holophyte.review.landmarks import SPAN_RE, MainTree

VERIFY_HEADING = "## Verify command(s)"
TOKEN_RE = re.compile(r"\S+")
DOTTED_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


class Chain:
    def __init__(self, path):
        self.path = path
        self.hops = []
        self.deleted = None
        self.split = None

    @property
    def touched(self):
        return bool(self.hops) or self.deleted or self.split

    def follow(self, commit, rows):
        sources = [row for row in rows if row[1] == self.path]
        moves = [row for row in sources if row[0][0] in "RC"]
        if len(moves) > 1:
            self.split = (commit, sorted(row[2] for row in moves))
        elif any(row[0][0] == "R" for row in moves):
            self.hops.append((commit, self.path, moves[0][2]))
            self.path = moves[0][2]
        elif any(row[0] == "D" for row in sources):
            self.deleted = commit
        return self.deleted is None and self.split is None


def body_written_at(conn, ticket_id, body):
    written = None
    for at, text in conn.execute(
            "SELECT at, body FROM ticketRevisions WHERE ticketId = ?"
            " ORDER BY revision DESC", (ticket_id,)):
        if text != body:
            break
        written = at
    return written


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false",
                           *args], capture_output=True, text=True,
                          errors="replace")


def renames_since(repo, written_ms):
    base = _git(repo, "rev-list", "--first-parent", "-1",
                f"--before=@{written_ms // 1000}", "main").stdout.strip()
    listed = _git(repo, "log", "--first-parent", "--diff-merges=first-parent",
                  "--reverse", "-M", "-C", "--name-status", "--format=%x00%h",
                  f"{base}..main" if base else "main")
    commits = []
    for block in listed.stdout.split("\0")[1:]:
        sha, *lines = block.strip("\n").split("\n")
        commits.append((sha, [line.split("\t") for line in lines if line]))
    return commits


def _chain(path, commits):
    chain = Chain(path)
    for sha, rows in commits:
        if not chain.follow(sha, rows):
            break
    return chain


def _module_file(module, commits):
    parts = module.split(".")
    for end in range(len(parts), 0, -1):
        chain = _chain("/".join(parts[:end]) + ".py", commits)
        if chain.touched:
            return chain, parts[end:]
    return Chain(module.replace(".", "/") + ".py"), []


def _missing_module(tree, module):
    parts = module.split(".")
    return not any("/".join(parts[:end]) + ".py" in tree.files
                   for end in range(len(parts), 0, -1)) and (
        "/".join(parts) + "/__init__.py" not in tree.files)


def stale_modules(body, tree):
    modules = []
    for command in ticket_template.parse(body).verify_commands:
        for tokens in ticket_template._shell_commands(command):
            for module in ticket_template._unittest_modules(tokens):
                if module not in modules and _missing_module(tree, module):
                    modules.append(module)
    return modules


def _why_unmapped(written, chain, tree):
    if chain.split is not None:
        sha, places = chain.split
        return (f"`{written}` went to more than one place in {sha}: "
                + ", ".join(f"`{place}`" for place in places))
    if chain.deleted is not None:
        via = "".join(f" (as `{old}`)" for _, old, _ in chain.hops[-1:])
        return f"`{written}` was deleted in {chain.deleted}{via}"
    if not chain.hops:
        return f"`{written}` has no rename since the body was written"
    if chain.path not in tree.files:
        return (f"`{written}` was renamed to `{chain.path}` in"
                f" {chain.hops[-1][0]}, which is not on main")
    return None


def _hops(chain):
    return ", then ".join(f"to `{new}` in {sha}" for sha, _, new in chain.hops)


def _dotted(path):
    dotted = path.removesuffix(".py").replace("/", ".")
    return dotted if path.endswith(".py") and DOTTED_RE.fullmatch(dotted) else None


def map_landmarks(repo, paths, modules, commits):
    tree = MainTree(repo)
    spans, tokens, mapped, unmapped, touched = {}, {}, [], [], False
    for written in paths:
        chain = _chain(str(Path(written)), commits)
        touched = touched or bool(chain.touched)
        why = (f"`{written}` is a directory, which is not mapped"
               if written.endswith("/") else _why_unmapped(written, chain, tree))
        if why:
            unmapped.append(why)
            continue
        spans[written] = tokens[written] = chain.path
        mapped.append(f"`{written}` → `{chain.path}` (renamed"
                      f" {_hops(chain)})")
    for module in modules:
        chain, rest = _module_file(module, commits)
        touched = touched or bool(chain.touched)
        why = _why_unmapped(module, chain, tree)
        new = None if why else _dotted(chain.path)
        if why or new is None:
            unmapped.append(why or f"`{module}` moved to `{chain.path}`,"
                                   " which is not a Python module")
            continue
        tokens[module] = ".".join([new, *rest])
        mapped.append(f"`{module}` → `{tokens[module]}` (its file renamed"
                      f" {_hops(chain)})")
    return spans, tokens, mapped, unmapped, touched


def _span(match, spans):
    text = match.group(1)
    head, sep, tail = text.partition("::")
    new = spans.get(text) or (sep and spans.get(head) and spans[head] + sep + tail)
    return f"`{new}`" if new else match.group(0)


def rewrite(body, spans, tokens):
    _, header = ticket_header.split(body)
    lines = body.splitlines(keepends=True)
    fences = [i for i, line in enumerate(lines)
              if line.rstrip("\r\n") == ticket_header.FENCE]
    skip = range(fences[0], fences[1] + 1) if header and len(fences) > 1 else ()
    section, fenced = None, False
    for i, line in enumerate(lines):
        if i in skip:
            continue
        if line.startswith("## "):
            section = line.strip()
        if ticket_template.FENCE_RE.match(line.strip()):
            fenced = not fenced
        elif fenced and section == VERIFY_HEADING:
            lines[i] = TOKEN_RE.sub(lambda m: tokens.get(m.group(), m.group()),
                                    line)
        else:
            lines[i] = SPAN_RE.sub(lambda m: _span(m, spans), line)
    return "".join(lines)


def record(conn, ticket_id, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    store.record_note(conn, ticket_id, "relandmark", text,
                      f"relandmark:{digest}")


def refresh(repo, conn, parked, paths, still_stale):
    ticket_id, identifier, _, body = parked[:4]
    written = body_written_at(conn, ticket_id, body)
    if written is None:
        return None, None
    tree = MainTree(repo)
    spans, tokens, mapped, unmapped, touched = map_landmarks(
        repo, paths, stale_modules(body, tree), renames_since(repo, written))
    if not touched:
        return None, None
    if not unmapped:
        new = rewrite(body, spans, tokens)
        unmapped = [f"after the rewrite, {reason}"
                    for reason in still_stale(new)]
        if not unmapped:
            return new, (f"Refreshed {identifier} from main's rename history"
                         " since the body was written; each landmark it"
                         " named moved there:\n\n"
                         + "\n".join(f"* {line}" for line in mapped))
    return None, (f"Not refreshed: main's rename history since the body was"
                  f" written does not map every landmark {identifier} names,"
                  " so it stays parked:\n\n"
                  + "\n".join(f"* {line}" for line in unmapped))

import hashlib
import re
import shlex
import subprocess
from pathlib import Path

import store
from holophyte.review.landmarks import SPAN_RE, declared_new
from holophyte.tickets import ticket_header, ticket_template

VERIFY_HEADING = "## Verify command(s)"
TOKEN_RE = re.compile(
    r"""(?:\\.|[^\s;&|()'"\\]|'[^']*'|"(?:\\.|[^"\\])*")+""")
DOTTED_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
REFRESHED = "Refreshed"
RENAMED_BRIEF = ("Main renamed paths this ticket named when it was written;"
                 " the ticket above names them where main holds them now:")


class Chain:
    def __init__(self, path, holds=None):
        self.path = path
        self.holds = holds
        self.hops = []
        self.deleted = None
        self.split = None
        self.copied = False
        self.directory = False

    @property
    def touched(self):
        return bool(self.hops or self.copied) or self.deleted or self.split

    def follow(self, commit, rows):
        sources = [row for row in rows if row[1] == self.path]
        if not sources:
            return self._follow_directory(commit, rows)
        moves = [row for row in sources if row[0][0] in "RC"]
        if len(moves) > 1:
            self.split = (commit, sorted(row[2] for row in moves))
        elif any(row[0][0] == "R" for row in moves):
            self.hops.append((commit, self.path, moves[0][2]))
            self.path = moves[0][2]
        elif any(row[0] == "D" for row in sources):
            self.deleted = commit
        self.copied = self.copied or bool(moves)
        return self.deleted is None and self.split is None

    def _follow_directory(self, commit, rows):
        prefix = self.path + "/"
        under = [row for row in rows if row[1].startswith(prefix)]
        moves = [row for row in under if row[0][0] in "RC"]
        places = {row[2]: _moved_under(row[1].removeprefix(prefix), row[2])
                  for row in moves}
        landed = set(places.values())
        if None in landed and all(row[2].startswith(prefix) for row in moves):
            moves, landed = [], set()
        if None in landed or len(landed) > 1:
            self.split = (commit, sorted({place or destination for
                                          destination, place in places.items()}))
        elif any(row[0][0] == "R" for row in moves):
            (new,) = landed
            self.hops.append((commit, self.path, new))
            self.path, self.directory = new, True
        elif (any(row[0] == "D" for row in under)
              and not self.holds(commit, self.path)):
            self.deleted = commit
        self.copied = self.copied or bool(moves)
        return self.deleted is None and self.split is None


def _moved_under(relative, destination):
    new = destination.removesuffix("/" + relative)
    return new if new != destination and new else None


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


def _read(repo, *args):
    done = _git(repo, *args)
    if done.returncode != 0:
        raise ValueError(f"git {args[0]} failed, so main's rename history"
                         f" was not read: {done.stderr.strip()}")
    return done.stdout


def renames_since(repo, written_ms):
    base = _read(repo, "rev-list", "--first-parent", "-1",
                 f"--before=@{written_ms // 1000}", "main").strip()
    listed = _read(repo, "log", "--first-parent", "--diff-merges=first-parent",
                   "--reverse", "-M", "-C", "--name-status", "--format=%x00%h",
                   f"{base}..main" if base else "main")
    commits = []
    for block in listed.split("\0")[1:]:
        sha, *lines = block.strip("\n").split("\n")
        commits.append((sha, [line.split("\t") for line in lines if line]))
    return commits


def _holder(repo):
    return lambda commit, path: bool(
        _read(repo, "ls-tree", "--name-only", commit, "--", path).strip())


def _chain(path, commits, holds):
    chain = Chain(path, holds)
    for sha, rows in commits:
        if not chain.follow(sha, rows):
            break
    return chain


def _module_file(module, commits, holds):
    parts = module.split(".")
    for end in range(len(parts), 0, -1):
        chain = _chain("/".join(parts[:end]) + ".py", commits, holds)
        if chain.touched:
            return chain, parts[end:]
    return Chain(module.replace(".", "/") + ".py"), []


def _missing_module(tree, module, declarations):
    parts = module.split(".")
    files = ["/".join(parts[:end]) + ".py" for end in range(len(parts), 0, -1)]
    files.append("/".join(parts) + "/__init__.py")
    return not any(f in tree.files or declared_new(f, declarations)
                   for f in files)


def stale_modules(body, tree):
    t = ticket_template.parse(body)
    declarations = ticket_template._new_paths(t)
    modules = []
    for command in t.verify_commands:
        for tokens in ticket_template._shell_commands(command):
            for module in ticket_template._unittest_modules(tokens):
                if (module not in modules
                        and _missing_module(tree, module, declarations)):
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
    if chain.path not in (tree.trees if chain.directory else tree.files):
        return (f"`{written}` was renamed to `{chain.path}` in"
                f" {chain.hops[-1][0]}, which is not on main")
    return None


def _hops(chain):
    return ", then ".join(f"to `{new}` in {sha}" for sha, _, new in chain.hops)


def _dotted(path):
    dotted = path.removesuffix(".py").replace("/", ".")
    return dotted if path.endswith(".py") and DOTTED_RE.fullmatch(dotted) else None


def map_landmarks(tree, paths, modules, commits):
    spans, tokens, folders, mapped, unmapped, touched = {}, {}, {}, [], [], False
    holds = _holder(tree.repo)
    for written in paths:
        chain = _chain(str(Path(written)), commits, holds)
        touched = touched or bool(chain.touched)
        why = _why_unmapped(written, chain, tree)
        if not why and written.endswith("/") and not chain.directory:
            why = f"`{written}` names a directory, but main renamed a file there"
        if why:
            unmapped.append(why)
            continue
        new = chain.path + "/" if written.endswith("/") else chain.path
        spans[written] = tokens[written] = new
        if chain.directory:
            folders[str(Path(written))] = chain.path
        mapped.append(f"`{written}` → `{new}` (renamed {_hops(chain)})")
    for module in modules:
        chain, rest = _module_file(module, commits, holds)
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
    return spans, tokens, folders, mapped, unmapped, touched


def _under(path, folders, quote=str):
    bare = re.sub(r"^(?:\./)+", "", path)
    for old in sorted(folders, key=len, reverse=True):
        if bare == old or bare.startswith(old + "/"):
            return quote(folders[old]) + bare[len(old):]
    return None


def _span(match, spans, folders):
    text = match.group(1)
    head, sep, tail = text.partition("::")
    new = (spans.get(text)
           or (sep and spans.get(head) and spans[head] + sep + tail)
           or _under(text, folders))
    return f"`{new}`" if new else match.group(0)


def _token(word, tokens, folders):
    try:
        value = shlex.split(word)
    except ValueError:
        return word
    if len(value) != 1:
        return word
    raw = None if value[0] in tokens else _under(word, folders, shlex.quote)
    new = tokens.get(value[0]) or _under(value[0], folders)
    if raw is not None or new is None:
        return raw or word
    if word == f"'{value[0]}'" and "'" not in new:
        return f"'{new}'"
    if word == f'"{value[0]}"' and not set(new) & set('$`"\\!'):
        return f'"{new}"'
    return shlex.quote(new)


def rewrite(body, spans, tokens, folders):
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
            lines[i] = TOKEN_RE.sub(lambda m: _token(m.group(), tokens, folders),
                                  line)
        else:
            lines[i] = SPAN_RE.sub(lambda m: _span(m, spans, folders), line)
    return "".join(lines)


def record(conn, ticket_id, text):
    digest = hashlib.sha256(text.encode()).hexdigest()
    store.record_note(conn, ticket_id, "relandmark", text,
                      f"relandmark:{digest}")


def refresh(tree, conn, parked, paths, still_stale):
    ticket_id, identifier, _, body = parked[:4]
    written = body_written_at(conn, ticket_id, body)
    if written is None:
        return None, None
    spans, tokens, folders, mapped, unmapped, touched = map_landmarks(
        tree, paths, stale_modules(body, tree),
        renames_since(tree.repo, written))
    if not touched:
        return None, None
    if not unmapped:
        new = rewrite(body, spans, tokens, folders)
        unmapped = [f"after the rewrite, {reason}"
                    for reason in still_stale(new)] + [
            f"after the rewrite, `{module}` is still not on main"
            for module in stale_modules(new, tree)]
        if not unmapped:
            return new, (f"{REFRESHED} {identifier} from main's rename history"
                         " since the body was written; each landmark it"
                         " named moved there:\n\n"
                         + "\n".join(f"* {line}" for line in mapped))
    return None, (f"Not refreshed: main's rename history since the body was"
                  f" written does not map every landmark {identifier} names,"
                  " so it stays parked:\n\n"
                  + "\n".join(f"* {line}" for line in unmapped))


def renamed_brief(conn, ticket_id):
    note = conn.execute(
        "SELECT at, text FROM ticketNotes WHERE ticketId = ?"
        " AND kind = 'relandmark' ORDER BY at DESC, id DESC LIMIT 1",
        (ticket_id,)).fetchone()
    if note is None or not note[1].startswith(REFRESHED + " "):
        return ""
    if conn.execute(
            "SELECT 1 FROM ticketRevisions WHERE ticketId = ? AND at >= ?"
            " AND author != 'factory'", (ticket_id, note[0])).fetchone():
        return ""
    bullets = [line for line in note[1].splitlines() if line.startswith("* ")]
    return "\n\n" + "\n".join([RENAMED_BRIEF, *bullets])

#!/usr/bin/env python3
import re
import shlex
import subprocess
import sys
from pathlib import Path

TEMPLATE_ORDER = [
    "Summary", "What / Why / How", "Reproduce", "Mock-up", "In scope",
    "Out of scope",
    "Acceptance criteria", "Verify command(s)", "Contract checks", "Evidence",
    "Implementation notes", "Story", "Estimate & dependencies",
    "Open questions",
]
OPTIONAL_SECTIONS = {"Reproduce", "Mock-up", "Contract checks", "Evidence",
                     "Story"}
SECTION_ORDER = [s for s in TEMPLATE_ORDER if s not in OPTIONAL_SECTIONS]
MAX_ESTIMATE_MIN = 90
MAX_CRITERIA = 10
MAX_IN_SCOPE = 6
ADVISORY_PREFIX = "advisory: "
WHOLE_SUITE_ADVISORY = "verify command discovers the whole unit suite"
SCHEMA_VERSION_ADVISORY = "literal schema version"
FILING_REFUSED = tuple(ADVISORY_PREFIX + a for a in (WHOLE_SUITE_ADVISORY,
                                                     SCHEMA_VERSION_ADVISORY))
# A literal schema version goes stale once another ticket bumps it first.
SCHEMA_VERSION_RE = re.compile(
    r"\b(?i:schema version)\s+`?\d"
    r"|\bSCHEMA_VERSION`?(?:\s+to\s+|\s*=\s*)`?\d")
# Advisory only: "read and write the cache" is one deliverable.
SCOPE_CHAINING = (" and ", ";", ", then ")
# Not attached to a path, so ".venv/bin/python" does not match.
BARE_INTERPRETER_RE = re.compile(r"(?<![\w./-])(python3?|pip)(?![\w.-])")
# The reviewer sees a clean export of the candidate, not main after the merge.
OPERATOR_WITNESS_PHRASES = (
    "after the merge", "once merged", "on the writer host", "operator",
    "visual pass", "when viewed", "by hand",
)
# A leading dot is a path too: dot-directories are what gets gitignored.
PATH_TOKEN_RE = re.compile(r"^(?:\./)?[\w.][\w.\-]*(?:/[\w.\-]+)*/?$")
# Every real repository ignores .venv, and the harness provisions it.
VENV_PATH_PREFIX = ".venv/"
FILE_EXT_RE = re.compile(r"\.[a-z][a-z0-9]{0,9}$")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# "e.g." / "i.e." read as a stem plus a one-letter extension; they are prose.
ABBREVIATION_RE = re.compile(r"^(?:\w\.)+\w$")
VENV_ACTIVATE_RE = re.compile(r"(?:^|[\s;&|])(?:\.|source)\s+\.venv/bin/activate\b")
H1_RE = re.compile(r"^#\s+(.*?)\s*$")
H2_RE = re.compile(r"^##\s+(.*?)\s*$")
# A cap that recognized only some markers would be bypassable by typing "+".
BULLET = r"[-*+]"
UNCHECKED_RE = re.compile(rf"^{BULLET}\s+\[ \]\s*(.*)$")
CHECKED_RE = re.compile(rf"^{BULLET}\s+\[[xX]\]\s*(.*)$")
LIST_ITEM_RE = re.compile(rf"^(?:{BULLET}|\d+[.)])\s+(.*)$")
# Linear rewrites "**What:**" as "**What: **" on every body patch.
BOLD_KEY_RE = re.compile(
    r"^(?:\*\*)?(What|Why|How|UI change|Blast radius):(?:[ \t]*\*\*)?\s*(.*)$")
UI_CHANGE_VALUES = ("major", "minor")
BLAST_RADIUS_VALUES = ("high", "medium")
MOCKUP_URL_RE = re.compile(
    r"https://claude\.ai/(?:code/)?artifact/[\w-]+"
    r"|https://lotuspod(?:\.[A-Za-z0-9-]+)+/[\w.-]+\.html")
URL_TOKEN_RE = re.compile(r"(?<![\w+.-])[A-Za-z][\w+.-]*:[^\s*`>)\]\"']\S*")
APPROVED_RE = re.compile(r"^(?:[-*+]\s+)?Approved \d{4}-\d{2}-\d{2}:\s*\S")
EVIDENCE_STATES_FOR_MOCKUP = 3
ESTIMATE_RE = re.compile(r"^Estimate:\s*(\d+)\s*min\s*·\s*Depends on:\s*(.+)$")
ORCHESTRATION_RE = re.compile(r"^Orchestration:\s*(.*?)\s*$")
ORCHESTRATION_MODES = ("off", "subagents", "workflow")
LINEAR_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}|<[^<>\n\s][^<>\n]*>")
# Linear wraps file names and ticket ids in links on save; only links unwrap.
MD_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\((?:<[^<>\n]*>|[^()\s]*)\)")
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
FENCE_RE = re.compile(r"^```([^\n]*)$")
# Literal only: no regex, no shell.
CONTRACT_RE = re.compile(r"^(\S+?):\s*(.*)$")
OPEN_QUESTIONS_NONE = ("- None", "* None", "+ None")


def _clean(s):
    return re.sub(r"\s+", " ", s).strip()


def _list_items(body):
    out = []
    for line in body.splitlines():
        m = LIST_ITEM_RE.match(line.strip())
        if m:
            out.append(_clean(m.group(1)))
    return out


def _list_item_blocks(body):
    out, entry, blank = [], None, False
    for line in body.splitlines():
        m = LIST_ITEM_RE.match(line.strip())
        if m:
            entry = [m.group(1)]
            out.append(entry)
        elif entry and line.strip() and (not blank or line[:1].isspace()):
            entry.append(line.strip())
        elif line.strip():
            entry = None
        blank = not line.strip()
    return [_clean(" ".join(lines)) for lines in out]


def _evidence_states(body):
    states = []
    for line in COMMENT_RE.sub("", body).splitlines():
        text = line.strip()
        if not text:
            continue
        item = re.match(rf"^(?:{BULLET}|\d+[.)])(?:\s+(.*))?$", text)
        states.append((item.group(1) or "") if item else text)
    return states


def _criteria(body):
    """"other" entries count toward MAX_CRITERIA, so no list marker slips the cap."""
    unchecked, checked, other, boxes = [], [], [], []
    for line in body.splitlines():
        s = line.strip()
        item = LIST_ITEM_RE.match(s)
        if not item:
            continue
        done, todo = CHECKED_RE.match(s), UNCHECKED_RE.match(s)
        if todo:
            unchecked.append(_clean(todo.group(1)))
            boxes.append(unchecked[-1])
        elif done:
            checked.append(_clean(done.group(1)))
            boxes.append(checked[-1])
        else:
            other.append(_clean(item.group(1)))
    return unchecked, checked, other, boxes


def _fenced_lines(body):
    out, in_fence = [], False
    for line in body.splitlines():
        s = line.strip()
        if FENCE_RE.match(s):
            in_fence = not in_fence
            continue
        if in_fence and s:
            out.append(s)
    return out


def _fence_advisories(t):
    advisories = []
    for section, label in (("Verify command(s)", "verify"),
                           ("Contract checks", "contract checks")):
        in_fence = False
        for line in t.sections.get(section, "").splitlines():
            match = FENCE_RE.match(line.strip())
            if match:
                tag = match.group(1).strip()
                if not in_fence and tag:
                    advisories.append(
                        f"{ADVISORY_PREFIX}{label} fence carries a language tag "
                        f"({tag}); the factory ignores it")
                in_fence = not in_fence
    return advisories


def _verify_commands(body):
    return [s for s in _fenced_lines(body)
            if not s.startswith("Rules:") and not s.startswith("- ")]


def _contract_checks(body):
    """A line with no colon is ("", line), so validate() names it, not drops it."""
    checks = []
    for line in _fenced_lines(body):
        if line.startswith("Rules:"):
            break
        m = CONTRACT_RE.match(line)
        checks.append((m.group(1), m.group(2).strip()) if m else ("", line))
    return checks


def _deps(raw):
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) == 1 and parts[0].lower() == "none":
        return []
    return parts


class Ticket:
    def __init__(self):
        self.title = ""
        self.order = []
        self.sections = {}
        self.stray_h1s = []
        self.summary = ""
        self.what = self.why = self.how = ""
        self.ui_change = self.blast_radius = None
        self.mockup = ""
        self.in_scope = []
        self.out_of_scope = []
        self.acceptance = []
        self.acceptance_done = []
        self.acceptance_other = []
        self.acceptance_boxes = []
        self.verify_commands = []
        self.contract_checks = []
        self.evidence_states = []
        self.reproduce = ""
        self.notes = []
        self.estimate_min = None
        self.depends_on = None
        self.orchestration = None
        self.open_questions_none = False


def parse(text):
    t = Ticket()
    lines = MD_LINK_RE.sub(r"\1", text).splitlines()
    first_h1 = next((i for i, ln in enumerate(lines) if H1_RE.match(ln)), None)
    if first_h1 is None:
        body_start = 0
    else:
        t.title = H1_RE.match(lines[first_h1]).group(1)
        t.stray_h1s = [H1_RE.match(ln).group(1) for ln in lines[first_h1 + 1:]
                       if H1_RE.match(ln)]
        body_start = first_h1 + 1

    cur, buf = None, []
    for ln in lines[body_start:]:
        h2 = H2_RE.match(ln)
        if h2:
            if cur is not None:
                _keep(t, cur, "\n".join(buf))
            cur, buf = h2.group(1).strip(), []
        elif cur is not None:
            buf.append(ln)
    if cur is not None:
        _keep(t, cur, "\n".join(buf))

    t.summary = _clean(t.sections.get("Summary", ""))
    kv = {}
    for ln in t.sections.get("What / Why / How", "").splitlines():
        m = BOLD_KEY_RE.match(ln.strip())
        if m:
            kv[m.group(1)] = _clean(m.group(2))
    t.what, t.why, t.how = kv.get("What", ""), kv.get("Why", ""), kv.get("How", "")
    t.ui_change, t.blast_radius = kv.get("UI change"), kv.get("Blast radius")
    t.reproduce = COMMENT_RE.sub("", t.sections.get("Reproduce", "")).strip()
    t.mockup = COMMENT_RE.sub("", t.sections.get("Mock-up", "")).strip()
    t.in_scope = _list_items(t.sections.get("In scope", ""))
    t.out_of_scope = _list_items(t.sections.get("Out of scope", ""))
    (t.acceptance, t.acceptance_done, t.acceptance_other,
     t.acceptance_boxes) = _criteria(t.sections.get("Acceptance criteria", ""))
    t.verify_commands = _verify_commands(t.sections.get("Verify command(s)", ""))
    t.contract_checks = _contract_checks(t.sections.get("Contract checks", ""))
    t.evidence_states = _evidence_states(t.sections.get("Evidence", ""))
    t.notes = _list_items(t.sections.get("Implementation notes", ""))
    est = None
    for ln in t.sections.get("Estimate & dependencies", "").splitlines():
        m = ESTIMATE_RE.match(ln.strip())
        est = est or m
        mode = ORCHESTRATION_RE.match(ln.strip())
        if mode and t.orchestration is None:
            t.orchestration = mode.group(1)
    t.estimate_min = int(est.group(1)) if est else None
    t.depends_on = _deps(est.group(2)) if est else None
    oq = COMMENT_RE.sub("", t.sections.get("Open questions", ""))
    t.open_questions_none = _clean(oq) in OPEN_QUESTIONS_NONE
    return t


def _keep(t, name, body):
    t.order.append(name)
    t.sections.setdefault(name, body)


def _labeled_texts(t):
    yield "title", t.title
    yield "Summary", t.summary
    for label, v in (("What:", t.what), ("Why:", t.why), ("How:", t.how)):
        if v:
            yield label, v
    if t.reproduce:
        yield "Reproduce", t.reproduce
    if t.mockup:
        yield "Mock-up", t.mockup
    lists = (("In scope", t.in_scope), ("Out of scope", t.out_of_scope),
             ("Acceptance criteria", t.acceptance),
             ("Implementation notes", t.notes), ("Evidence", t.evidence_states))
    for label, items in lists:
        for i, item in enumerate(items, 1):
            yield f"{label} #{i}", item
    for cmd in t.verify_commands:
        yield "verify command", cmd
    for path, literal in t.contract_checks:
        yield "contract check", f"{path}: {literal}"


def _has_nonrelative_path(cmd):
    scrubbed = re.sub(r'"[^"]*"', '""', cmd)
    for piece in re.split(r"[=\s]", scrubbed):
        if piece.startswith("/") or piece.startswith("~/"):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", piece):
            return True
    return False


def _bare_interpreter(cmd):
    for m in BARE_INTERPRETER_RE.finditer(cmd):
        if not VENV_ACTIVATE_RE.search(cmd[:m.start()]):
            return m.group(1)
    return None


def contract_path_problem(path):
    """Shared with the verify gate so the ticket-time and run-time rules agree."""
    if not path:
        return "declaration must read 'relative/path: expected literal'"
    if (path.startswith("/") or path.startswith("~")
            or re.match(r"^[A-Za-z]:[\\/]", path)):
        return "path must be relative to the repo root"
    if ".." in path.split("/"):
        return "path must stay inside the repo (no '..' segments)"
    return None


def path_candidates(text):
    text = MARKDOWN_LINK_RE.sub(r"\1", text)
    seen = []
    for raw in re.split(r"[\s=]+", text):
        token = raw.strip("`'\"(),;:!?<>[]{}")
        token = token.rstrip(".")
        if not token or "://" in token:
            continue
        if not PATH_TOKEN_RE.match(token) or ABBREVIATION_RE.match(token):
            continue
        if token.startswith(VENV_PATH_PREFIX):
            continue
        if "/" not in token and not FILE_EXT_RE.search(token):
            continue
        if token not in seen:
            seen.append(token)
    return seen


def _witnessable_texts(t):
    """Checked-off criteria count too: skipping them would shift the numbers."""
    for i, ac in enumerate(t.acceptance_boxes, 1):
        yield f"Acceptance criteria #{i}", ac
    for cmd in t.verify_commands:
        yield "verify command", cmd
    for path, _literal in t.contract_checks:
        yield "contract check", path


def _gitignored(repo, token):
    """None when git could not answer (exit 128)."""
    r = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--",
                        token], capture_output=True, text=True)
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def _gitignored_path_problems(t, repo):
    problems = []
    unchecked = False
    for label, text in _witnessable_texts(t):
        for token in path_candidates(text):
            ignored = _gitignored(repo, token)
            if ignored:
                problems.append(f"gitignored path in {label}: {token}")
            elif ignored is None:
                unchecked = True
    if unchecked:
        problems.append(f"{ADVISORY_PREFIX}could not check paths against "
                        f"{repo}: git check-ignore failed there (not a "
                        f"repository?)")
    return problems


SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".json", ".md",
    ".toml", ".yaml", ".yml", ".txt", ".html", ".css", ".scss",
    ".sh", ".bash", ".sql", ".rs", ".go", ".c", ".h", ".cpp",
    ".java", ".rb", ".vue", ".svelte", ".xml", ".ini", ".cfg",
}


def _repo_paths(text):
    return [p for p in path_candidates(text)
            if "/" in p or Path(p).suffix in SOURCE_EXTENSIONS]


def _prose_paths(text):
    for span in re.finditer(r"`([^`\n]+)`", text):
        paths = _repo_paths(span.group(1))
        if len(paths) == 1 and span.group(1) == paths[0]:
            yield span, paths[0]


def _mask_code_spans(text, fill=" "):
    return re.sub(r"`[^`\n]+`", lambda m: fill * len(m.group()), text)


def _sentence_before(masked, end):
    return re.split(r"[.!?](?:\s|$)|\n\s*(?:\n|[-*+] )", masked[:end])[-1]


PREPOSITIONS = frozenset(
    "aboard about above across after against along alongside amid among"
    " around as at atop before behind below beneath beside besides between"
    " beyond by concerning despite during except excluding following for from"
    " in including inside into like near of off on onto opposite out outside"
    " over past per regarding respecting round since than through throughout"
    " till to toward towards under underneath unlike until unto upon versus"
    " via with within without".split())
NEW_GAP_RE = re.compile(
    r"\s*(?P<words>(?:\w[\w-]*\s+)*(?:\w[\w-]*)?)\s*[,:(]?\s*"
    r"(?:\0+\s*(?:,\s*(?:(?:and|or)\s+)?|(?:and|or)\s+))*", re.I)


def _declarable(path, directory):
    return bool(PATH_TOKEN_RE.fullmatch(path)
                and (_repo_paths(path) or directory))


def _governing_new(text, marked, start, end):
    news = list(re.finditer(r"\bnew\b", marked[start:end], re.I))
    gap = news and NEW_GAP_RE.fullmatch(marked, start + news[-1].end(), end)
    if not gap:
        return None
    words = gap.group("words").lower().split()
    if len(words) > 3 or PREPOSITIONS.intersection(words):
        return None
    directory = any(re.fullmatch(r"director(?:y|ies)", w) for w in words)
    listed = re.finditer(r"`([^`\n]+)`", text[gap.start():end])
    if not all(_declarable(m.group(1), directory) for m in listed):
        return None
    return directory


def _new_paths(t):
    files, directories = set(), set()
    for text in t.sections.values():
        masked = _mask_code_spans(text)
        marked = _mask_code_spans(text, "\0")
        for span in re.finditer(r"`([^`\n]+)`", text):
            path = span.group(1)
            if not PATH_TOKEN_RE.fullmatch(path):
                continue
            sentence = _sentence_before(masked, span.start())
            directory = _governing_new(text, marked,
                                       span.start() - len(sentence),
                                       span.start())
            if directory is not None and _declarable(path, directory):
                normalized = str(Path(path))
                files.add(normalized)
                if path.endswith("/") or directory:
                    directories.add(normalized)
    return files, directories


def _outside(repo, path):
    try:
        return not (repo / path).resolve().is_relative_to(repo.resolve())
    except (OSError, RuntimeError):
        return True


def _available(repo, path, declarations):
    """Containment is checked before existence or exemptions, new files included."""
    if _outside(repo, path):
        return False
    files, directories = declarations
    normalized = str(Path(path))
    return ((repo / path).exists() or normalized in files
            or any(normalized.startswith(d + "/") for d in directories)
            or _gitignored(repo, path) is True)


def _shell_commands(command):
    """Tokenize only; never execute a verify command."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    commands, current = [], []
    for token in tokens:
        if token and all(c in ";&|()" for c in token):
            commands.append(current)
            current = []
        else:
            current.append(token)
    return commands + [current]


def _unittest_args(tokens):
    for i in range(len(tokens) - 1):
        if tokens[i:i + 2] == ["-m", "unittest"]:
            return tokens[i + 2:]
    return None


def _unittest_modules(tokens):
    args = iter(_unittest_args(tokens) or ())
    for arg in args:
        if arg == "discover":
            return
        if arg in ("-k", "--locals"):
            if arg == "-k":
                next(args, None)
            continue
        if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", arg):
            yield arg


def _discovers_whole_suite(tokens):
    args = _unittest_args(tokens) or []
    if "discover" not in args:
        return False
    return not any(arg.startswith(("-p", "--pattern"))
                   for arg in args[args.index("discover") + 1:])


def _suite_advisories(t):
    return [f"{ADVISORY_PREFIX}{WHOLE_SUITE_ADVISORY}; "
            f"name the focused test modules (discover -s tests -p "
            f"'test_x.py') — the full suite runs as a pull request check: "
            f"{cmd}"
            for cmd in t.verify_commands
            if any(_discovers_whole_suite(c) for c in _shell_commands(cmd))]


def _schema_version_advisories(t):
    return [f"{ADVISORY_PREFIX}{SCHEMA_VERSION_ADVISORY} in '{section}'; "
            f"say 'one above main's SCHEMA_VERSION' — another ticket may bump "
            f"the schema first: {line.strip()}"
            for section, body in t.sections.items()
            for line in COMMENT_RE.sub("", body).splitlines()
            if SCHEMA_VERSION_RE.search(line)]


def _ui_change_problems(t):
    if t.ui_change is None or t.ui_change.lower() in UI_CHANGE_VALUES:
        return []
    return [f"'**UI change:**' must read major or minor, not {t.ui_change!r}"]


def _blast_radius_problems(t):
    if t.blast_radius is None or t.blast_radius.lower() in BLAST_RADIUS_VALUES:
        return []
    return [f"'**Blast radius:**' must read high or medium, not {t.blast_radius!r}"]


def _mockup_urls(text):
    urls = []
    for token in URL_TOKEN_RE.findall(text):
        url = token.rstrip(".,;:!?`*>\"'")
        if url.endswith(")") and "(" not in url:
            url = url[:-1].rstrip(".,;:!?`*>\"'")
        urls.append(url)
    return urls


def _mockup_problems(t):
    if "Mock-up" not in t.order:
        return []
    urls = _mockup_urls(t.mockup)
    approvals = [line for line in t.mockup.splitlines()
                 if APPROVED_RE.match(line.strip())]
    problems = [f"'## Mock-up' link is not a claude.ai artifact or a Lotuspod "
                f"page: {url}"
                for url in urls if not MOCKUP_URL_RE.fullmatch(url)]
    if not urls:
        problems.append("'## Mock-up' has no link to the approved mock-up; write "
                        "the URL bare, since a markdown link keeps only its text")
    elif len(urls) > 1:
        problems.append(f"'## Mock-up' holds {len(urls)} links; give exactly one")
    if len(approvals) != 1:
        problems.append(f"'## Mock-up' needs exactly one 'Approved YYYY-MM-DD: "
                        f"what' line; it has {len(approvals)}")
    return problems


def _mockup_advisories(t):
    states = [state for state in t.evidence_states if state]
    if (len(states) < EVIDENCE_STATES_FOR_MOCKUP or t.ui_change is not None
            or "Mock-up" in t.order):
        return []
    return [f"{ADVISORY_PREFIX}'Evidence' lists {len(states)} states and no "
            f"'**UI change:**' line; declare '**UI change:** major' and link "
            f"the approved design under '## Mock-up', or declare "
            f"'**UI change:** minor'"]


def _discover_pattern(tokens):
    """The pattern names a file in the -s start directory, not the repository root."""
    for i in range(len(tokens) - 2):
        if tokens[i:i + 3] != ["-m", "unittest", "discover"]:
            continue
        start, found = ".", None
        args = enumerate(tokens[i + 3:], i + 3)
        for index, arg in args:
            long = arg.startswith("--")
            name, eq, value = arg.partition("=") if long else (arg, "", "")
            if name not in ("-s", "--start-directory", "-p", "--pattern"):
                continue
            if not eq:
                index, value = next(args, (None, None))
            if value is None:
                break
            if name in ("-s", "--start-directory"):
                start = value
            else:
                found = index, value
        if found:
            return found[0], str(Path(start) / found[1])
        return None
    return None


def _verify_paths(command):
    commands = _shell_commands(command)
    if not commands:
        return _repo_paths(command)
    paths = []
    for tokens in commands:
        pattern = _discover_pattern(tokens)
        if pattern:
            index, path = pattern
            tokens = tokens[:index] + [path] + tokens[index + 1:]
        paths += _repo_paths(" ".join(tokens))
    return list(dict.fromkeys(paths))


def _module_available(repo, module, declarations):
    # unittest also accepts package names and qualified class/method names.
    parts = module.split(".")
    for end in range(len(parts), 0, -1):
        stem = "/".join(parts[:end])
        if _available(repo, stem + ".py", declarations):
            return True
    stem = "/".join(parts)
    return _available(repo, stem + "/__init__.py", declarations)


def _path_problem(repo, path, declarations, label, prefix=ADVISORY_PREFIX):
    """A missing path is advisory by default: prose names files a candidate creates."""
    if _outside(repo, path):
        return f"path is outside the repository in {label}: {path}"
    if not _available(repo, path, declarations):
        return f"{prefix}path does not exist in {label}: {path}"
    return None


def _repository_problems(t, repo):
    repo = Path(repo)
    declarations = _new_paths(t)
    problems = []
    texts = [(f"Acceptance criteria #{i}", text)
             for i, text in enumerate(t.acceptance_boxes, 1)]
    texts.append(("Implementation notes", t.sections.get("Implementation notes", "")))
    for label, text in texts:
        for path in dict.fromkeys(path for _, path in _prose_paths(text)):
            problems.append(_path_problem(repo, path, declarations, label))
    for command in t.verify_commands:
        for path in _verify_paths(command):
            problems.append(_path_problem(repo, path, declarations,
                                          "verify command", prefix=""))
        for tokens in _shell_commands(command):
            for module in _unittest_modules(tokens):
                if not _module_available(repo, module, declarations):
                    problems.append(f"{ADVISORY_PREFIX}unittest module does not "
                                    f"exist in verify command: {module}")
    return [problem for problem in problems if problem]


def _script_arguments(tokens):
    args = iter(tokens)
    executable = next(args, "")
    while executable == "env" or re.match(r"^[A-Za-z_]\w*=", executable):
        executable = next(args, "")
    if Path(executable).name == "ticket_template.py":
        return list(args)
    if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(executable).name):
        return []
    for arg in args:
        if arg == "-m":
            return list(args) if next(args, "") == "ticket_template" else []
        if arg == "-c":
            return []
        if arg in ("-W", "-X"):
            next(args, None)
        elif not arg.startswith("-"):
            return list(args) if Path(arg).name == "ticket_template.py" else []
    return []


def _blank_template_problems(t):
    problems = []
    for command in t.verify_commands:
        for tokens in _shell_commands(command):
            names = [Path(token).name for token in _script_arguments(tokens)]
            if "ticketTemplate.md" in names:
                problems.append("verify command runs the validator on "
                                "ticketTemplate.md: the blank template can never "
                                f"validate: {command}")
    return problems


def _operator_witness_advisories(t):
    out = []
    for i, ac in enumerate(t.acceptance_boxes, 1):
        low = ac.lower()
        if any(phrase in low for phrase in OPERATOR_WITNESS_PHRASES):
            out.append(f"{ADVISORY_PREFIX}criterion {i} reads as an operator "
                       f"or post-merge witness; the reviewer can only witness "
                       f"the candidate branch")
    return out


def validate(t, repo=None):  # noqa: C901 -- one pass over every rule; split at a rule registry
    """Valid iff blocking(validate(t)) is empty; `repo` enables repository checks."""
    p = []
    if not t.title:
        p.append("missing H1 title ('# ...' on the first heading line)")
    if t.stray_h1s:
        p.append(f"{len(t.stray_h1s)} extra H1 heading(s); exactly one allowed")

    seen_first = {}
    for idx, name in enumerate(t.order):
        seen_first.setdefault(name, idx)
    # Only sections present are compared, so an absent optional one opens no gap.
    present = [n for n in TEMPLATE_ORDER if n in seen_first]
    for a, b in zip(present, present[1:]):
        if seen_first[a] >= seen_first[b]:
            p.append(
                f"sections out of template order: '## {a}' must come before '## {b}'"
            )
    for name in TEMPLATE_ORDER:
        n = t.order.count(name)
        if n == 0 and name not in OPTIONAL_SECTIONS:
            p.append(f"missing section '## {name}'")
        elif n > 1:
            p.append(f"duplicate section '## {name}' ({n}x)")
    for u in sorted(set(t.order) - set(TEMPLATE_ORDER)):
        p.append(f"unknown section '## {u}' (not in ticketTemplate.md)")

    for label, text in _labeled_texts(t):
        m = PLACEHOLDER_RE.search(text)
        if m:
            p.append(f"unfilled template placeholder in {label}: {m.group(0)}")

    if len(t.evidence_states) > 6:
        p.append("'Evidence' has more than 6 states; the limit is 6")
    if "Reproduce" in t.order and not t.reproduce:
        p.append("'Reproduce' is empty; give the steps and where the "
                 "behaviour was seen, or omit the section")
    if "Evidence" in t.order and not t.evidence_states:
        p.append("'Evidence' is empty; list states or omit the section")
    for index, state in enumerate(t.evidence_states, 1):
        if not state:
            p.append(f"Evidence state #{index} is empty")

    if not t.summary:
        p.append("'Summary' is empty")
    if not t.what:
        p.append("'**What:**' line missing under 'What / Why / How'")
    if not t.how:
        p.append("'**How:**' line missing under 'What / Why / How' "
                 "('Why' is optional)")
    if not t.in_scope:
        p.append("'In scope' has no entries")
    elif len(t.in_scope) > MAX_IN_SCOPE:
        p.append(f"'In scope' has {len(t.in_scope)} entries; the cap is "
                 f"{MAX_IN_SCOPE} — split the ticket")
    if not t.out_of_scope:
        p.append("'Out of scope' has no entries")

    if not t.acceptance and not t.acceptance_done:
        p.append("'Acceptance criteria' has no '- [ ] Given/when/then' items")
    elif not t.acceptance:
        p.append("all acceptance criteria are already checked off")
    for i, ac in enumerate(t.acceptance, 1):
        if not ac:
            p.append(f"acceptance criterion #{i} is empty")
    for item in t.acceptance_other:
        p.append(f"acceptance criterion is not a '- [ ] ...' checkbox: {item}")
    n_criteria = (len(t.acceptance) + len(t.acceptance_done)
                  + len(t.acceptance_other))
    if n_criteria > MAX_CRITERIA:
        p.append(f"'Acceptance criteria' has {n_criteria} items; the cap is "
                 f"{MAX_CRITERIA} — split the ticket")

    if not t.verify_commands:
        p.append("'Verify command(s)' has no runnable command lines inside "
                 "a ``` fence")
    for cmd in t.verify_commands:
        if _has_nonrelative_path(cmd):
            p.append(f"verify command uses a non-relative path "
                     f"(template rule: relative only): {cmd}")

    if "Contract checks" in t.order and not t.contract_checks:
        p.append("'Contract checks' section has no 'relative/path: expected "
                 "literal' declarations inside a ``` fence")
    for path, literal in t.contract_checks:
        problem = contract_path_problem(path)
        if problem:
            p.append(f"contract check {problem}: {path or literal}")
        elif not literal:
            p.append(f"contract check has an empty expected literal: {path}")

    if t.estimate_min is None:
        p.append("'Estimate & dependencies' must read "
                 "'Estimate: N min · Depends on: <ticket IDs or \"none\">'")
    else:
        if t.estimate_min > MAX_ESTIMATE_MIN:
            p.append(f"estimate is {t.estimate_min} min; the cap is "
                     f"{MAX_ESTIMATE_MIN} min — split the ticket")
        for d in t.depends_on:
            if not LINEAR_ID_RE.match(d):
                p.append(f"'Depends on' entry is not a ticket ID or \"none\": {d}")

    if t.orchestration is not None and t.orchestration not in ORCHESTRATION_MODES:
        p.append(f"'Estimate & dependencies' line 'Orchestration: "
                 f"{t.orchestration}' must name one of "
                 f"{', '.join(ORCHESTRATION_MODES)}")

    if not t.open_questions_none:
        p.append("'Open questions' must read exactly '- None' before the "
                 "ticket enters the pickable queue")

    chained = next((m for m in SCOPE_CHAINING if m in t.what.lower()), None)
    if chained:
        p.append(f"{ADVISORY_PREFIX}'**What:**' chains scope on {chained!r}; "
                 f"consider splitting it into one ticket per deliverable")
    for cmd in t.verify_commands:
        token = _bare_interpreter(cmd)
        if token:
            p.append(f"{ADVISORY_PREFIX}verify command uses bare {token!r} "
                     f"(template rule: activate the venv or use "
                     f".venv/bin/{token} if the project has one): {cmd}")
    p.extend(_blank_template_problems(t))
    p.extend(_fence_advisories(t))
    p.extend(_suite_advisories(t))
    p.extend(_schema_version_advisories(t))
    p.extend(_operator_witness_advisories(t))
    p.extend(_ui_change_problems(t))
    p.extend(_blast_radius_problems(t))
    p.extend(_mockup_problems(t))
    p.extend(_mockup_advisories(t))
    if repo is not None:
        p.extend(_gitignored_path_problems(t, repo))
        p.extend(_repository_problems(t, repo))
    return p


def blocking(problems):
    return [pr for pr in problems if not pr.startswith(ADVISORY_PREFIX)]


def filing_refusals(problems):
    return [pr for pr in problems
            if not pr.startswith(ADVISORY_PREFIX)
            or pr.startswith(FILING_REFUSED)]


USAGE = "usage: python3 ticket_template.py [--repo PATH] TICKET.md [...]"
HELP = "help"


def _parse_args(argv):
    repo, paths = None, []
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("-h", "--help"):
            return HELP
        if arg == "--repo":
            if not args:
                return None
            repo = args.pop(0)
        elif arg.startswith("--repo="):
            repo = arg[len("--repo="):]
        else:
            paths.append(arg)
    if not paths or repo == "":
        return None
    return repo, paths


def main(argv):
    if any(arg.partition("=")[0] == "--story" for arg in argv):
        import story_template
        return story_template.main(argv)
    parsed = _parse_args(argv)
    if parsed == HELP:
        print(USAGE)
        return 0
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return 2
    repo, paths = parsed
    invalid = 0
    for path in paths:
        text = Path(path).read_text()
        ticket = parse(text)
        problems = validate(ticket, repo=repo)
        if repo is not None:
            from holophyte.review.freshness import landmark_reasons
            prefix = ADVISORY_PREFIX if ticket.depends_on else ""
            problems += [prefix + reason
                         for reason in landmark_reasons(repo, text)]
            from holophyte.config.project import Project
            from holophyte.isolation.launcher import container_docker_problems
            problems += container_docker_problems(
                Project.locate(repo, adopt=False), ticket)
        blockers = blocking(problems)
        if blockers:
            invalid += 1
            print(f"{path}: INVALID")
        else:
            print(f"{path}: OK")
        advisories = [a for a in problems if a.startswith(ADVISORY_PREFIX)]
        if repo is None:
            advisories.append(f"{ADVISORY_PREFIX}repository check "
                              f"skipped: pass --repo PATH to check named "
                              f"paths against the project repository")
        for pr in blockers + advisories:
            print(f"  - {pr}")
    return 1 if invalid else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

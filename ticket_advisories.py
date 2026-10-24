"""Verify-block and criterion advisories that never block a filing."""
import functools
import io
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import ticket_template as tt

COUNT_RE = re.compile(
    r"\bRan \d+ tests?\b"
    r"|\b(?:failures|errors|passed|failed)=\d+\b"
    r"|(?<![\w#.-])\d+ (?:tests? )?"
    r"(?:pass(?:ed|ing)?|fail(?:ed|s|ing|ures)?)\b")
ORDINAL_WORDS = frozenset((
    "attempt", "case", "check", "code", "criteria", "criterion", "exit",
    "item", "line", "phase", "round", "run", "stage", "status", "step", "test"))
ID_CHAIN_RE = re.compile(r"(?:\b([A-Za-z]*)\d+\s*(?:,|\band\b|\bor\b)\s*)+$")
GLOB_TOKEN_RE = re.compile(r"[\w.*?/\[\]-]*[*?][\w.*?/\[\]-]*")
NON_PATH_OPTIONS = ("-k", "-m")
LAUNCHERS = ("env", "exec", "time")
PYTHON_RE = re.compile(r"(?:.*/)?python[\d.]*")
LINT_VALUE_OPTIONS = frozenset((
    "--select", "--ignore", "--extend-select", "--extend-ignore", "--fixable",
    "--unfixable", "--extend-fixable", "--per-file-ignores",
    "--extend-per-file-ignores", "--exclude", "--extend-exclude", "--config",
    "--output-format", "--output-file", "-o", "--target-version",
    "--line-length", "--cache-dir", "--stdin-filename"))
CD_COMMANDS = ("cd", "pushd")
SAME_COUNT = ("say the same count as main instead; a hard-coded count goes "
              "stale as main grows")
GLOB_CHARS = ("*", "?")


def _discovers_whole_suite(tokens):
    args = tt._unittest_args(tokens) or []
    if "discover" not in args:
        return False
    return not any(arg.startswith(("-p", "--pattern"))
                   for arg in args[args.index("discover") + 1:])


def _suite_advisories(t):
    return [f"{tt.ADVISORY_PREFIX}{tt.WHOLE_SUITE_ADVISORY}; "
            f"name the focused test modules (discover -s tests -p "
            f"'test_x.py') — the full suite runs as a pull request check: "
            f"{cmd}"
            for cmd in t.verify_commands
            if any(_discovers_whole_suite(c) for c in tt._shell_commands(cmd))]


def _names_an_item(before):
    chain = ID_CHAIN_RE.search(before)
    if chain:
        if chain.group(1):
            return True
        before = before[:chain.start()]
    word = re.search(r"(\w+)\W*$", before)
    return bool(word) and word.group(1).lower() in ORDINAL_WORDS


def _counts(text):
    return [match.group() for match in COUNT_RE.finditer(text)
            if not _names_an_item(text[:match.start()])]


def _count_advisories(t):
    texts = [(f"criterion {i}", text)
             for i, text in enumerate(t.acceptance_boxes, 1)]
    texts += [("verify command", cmd) for cmd in t.verify_commands]
    advisories = []
    for label, text in texts:
        counts = _counts(text)
        if counts:
            quoted = ", ".join(f"'{count}'" for count in counts)
            advisories.append(f"{tt.ADVISORY_PREFIX}{label} hard-codes a test "
                              f"count ({quoted}); {SAME_COUNT}: {text}")
    return advisories


def _tracked(repo):
    try:
        r = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"],
                           capture_output=True, text=True)
    except OSError:
        return []
    return [f for f in r.stdout.split("\0") if f] if r.returncode == 0 else []


def _glob_re(pattern):
    body = re.escape(pattern).replace(r"\*\*", ".*")
    return re.compile(body.replace(r"\*", "[^/]*").replace(r"\?", "[^/]"))


def _base(pattern):
    parts = Path(pattern).parts
    fixed = [p for p in parts[:-1] if not any(c in p for c in GLOB_CHARS)]
    return str(Path(*fixed)) if len(fixed) == len(parts) - 1 else "."


def _selections(tokens, tracked):
    """(selection, base directory, covered files) for each wide token."""
    pattern = tt._discover_pattern(tokens)
    if pattern:
        start, glob = Path(pattern[1]).parent, _glob_re(Path(pattern[1]).name)
        prefix = "" if str(start) == "." else f"{start}/"
        yield pattern[1], str(start), [
            f for f in tracked
            if f.startswith(prefix) and glob.fullmatch(f.rsplit("/", 1)[-1])]
        return
    if tokens[:1] and tokens[0] in CD_COMMANDS:
        return
    for previous, token in zip([""] + tokens, tokens):
        token = token.removeprefix("./")
        if previous not in NON_PATH_OPTIONS and GLOB_TOKEN_RE.fullmatch(token):
            glob = _glob_re(token)
            yield token, _base(token), [f for f in tracked if glob.fullmatch(f)]
    for path in tt._repo_paths(" ".join(tokens)):
        directory = path.removeprefix("./").rstrip("/")
        yield path, directory, [f for f in tracked
                                if f.startswith(directory + "/")]


def _wide_verify_advisories(t, repo):
    named = [p.removeprefix("./")
             for _, p in tt._prose_paths(t.sections.get("In scope", ""))]
    if not named or not t.verify_commands:
        return []
    tracked = _tracked(repo)
    advisories = []
    for cmd in t.verify_commands:
        for tokens in tt._shell_commands(cmd):
            for selection, base, covered in _selections(tokens, tracked):
                hits = [f for f in covered if f in named]
                if (not hits or len(hits) == len(covered) or any(
                        base == p.rstrip("/") or base.startswith(p.rstrip("/") + "/")
                        for p in named)):
                    continue
                advisories.append(
                    f"{tt.ADVISORY_PREFIX}verify command selects {selection}, "
                    f"which covers {len(covered)} files; In scope names "
                    f"{len(hits)} of them; run the named files instead: {cmd}")
    return advisories


def _ruff_arguments(tokens):
    while tokens and (tokens[0] in LAUNCHERS or "=" in tokens[0]):
        tokens = tokens[1:]
    if tokens and (tokens[0] == "ruff" or tokens[0].endswith("/ruff")):
        return tokens[1:]
    if tokens and PYTHON_RE.fullmatch(tokens[0]) and tokens[1:3] == ["-m", "ruff"]:
        return tokens[3:]
    return None


def _lints_whole_repository(tokens):
    rest = _ruff_arguments(tokens) or []
    if rest[:1] != ["check"]:
        return False
    paths = [a for before, a in zip(rest, rest[1:])
             if not a.startswith("-") and before not in LINT_VALUE_OPTIONS]
    return all(p in (".", "./") for p in paths)


def _main_sha(repo):
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify",
                            "--quiet", "main"], capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


@functools.lru_cache(maxsize=None)
def _main_lint_fails(repo, sha, ruff):
    with tempfile.TemporaryDirectory() as export:
        try:
            archive = subprocess.run(["git", "-C", repo, "archive",
                                      "--format=tar", sha], capture_output=True)
            if archive.returncode != 0:
                return False
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                tar.extractall(export, filter="data")
            r = subprocess.run([ruff, "check", "--no-cache", "--quiet", "."],
                               cwd=export, capture_output=True, timeout=60)
        except (OSError, tarfile.TarError, subprocess.TimeoutExpired):
            return False
    return r.returncode == 1


def _lint_advisories(t, repo):
    commands = [cmd for cmd in t.verify_commands
                if any(_lints_whole_repository(c) for c in tt._shell_commands(cmd))]
    ruff = shutil.which("ruff")
    if not commands or ruff is None:
        return []
    sha = _main_sha(repo)
    if sha is None or not _main_lint_fails(str(Path(repo).resolve()), sha, ruff):
        return []
    return [f"{tt.ADVISORY_PREFIX}repository-wide lint fails on main "
            f"({sha[:12]}): {cmd}; lint the changed files instead "
            f"(ruff check PATH …)" for cmd in commands]


def advisories(t, repo=None):
    found = _suite_advisories(t) + _count_advisories(t)
    if repo is not None:
        found += _wide_verify_advisories(t, repo)
        found += _lint_advisories(t, repo)
    return found

import ast
import posixpath
import re
import shlex
from pathlib import PurePosixPath
from typing import NamedTuple

from holophyte.loop.gates import sh, split_and_clauses

UNITTEST_RUN = re.compile(r"-m\s+unittest(?:\s+discover)?(?=\s|$)")
PYTEST_RUN = re.compile(r"(?:-m\s+pytest|(?<![\w.-])pytest)(?=\s|$)")
ARGS_END = re.compile(r"[|;&<>]|$")
QUIET = re.compile(r"(?<!\S)(?:-q+|--quiet)(?=\s|$)")
INSTALL = re.compile(r"\bpip3?\b")
UNITTEST_SUMMARY = re.compile(r"^Ran (\d+) tests? in ", re.M)
PYTEST_SUMMARY = re.compile(r"^=+ ([^=\n]*?) in [\d.]+s\b[^=\n]*=+\s*$", re.M)
PYTEST_RAN = re.compile(r"\b(\d+) (?:passed|failed|errors?|xfailed|xpassed)\b")
UNITTEST_RESULT = re.compile(r"^([\w.]+) \(([\w.]+)\)\n?[^\n]*? \.\.\. (.*)$", re.M)
PYTEST_RESULT = re.compile(
    r"^(\S+\.py)::(?:(\S+)::)?(\w+)(?:\[\S*\])? ([A-Z]+)(?: \((.*?)\))?\s+\[",
    re.M)
PYTEST_SKIP = re.compile(r"^SKIPPED \[\d+\] (\S+?\.py)(?::\d+)?: (.*)$", re.M)
MODULE_FIXTURE, CLASS_FIXTURE = "setUpModule", "setUpClass"
MODULE_SKIPPED = ["unittest", "loader", "ModuleSkipped"]
HUNK = re.compile(r"^@@ -\S+ \+(\d+)")
ADDED_TEST = re.compile(r"^\+\s*(?:async\s+)?def (test\w*)\(")
NOT_COLLECTED = "not collected by the verify commands"
ROOT_MARK = "holophyte-probe-root:"
ROOT_LINE = re.compile(rf"^{ROOT_MARK}(\S*)$", re.M)
CHDIR = re.compile(r"(?<![\w-])(?:cd|pushd|popd)(?![\w-])")
PLAIN_ROOT = re.compile(r"[A-Za-z_]\w*(?:/[A-Za-z_]\w*)*")
DISCOVER_VALUED = {"-s": "start", "--start-directory": "start",
                   "-t": "top", "--top-level-directory": "top",
                   "-p": None, "--pattern": None, "-k": None,
                   "--durations": None}


class Test(NamedTuple):
    path: str
    cls: object
    name: str

    def id(self):
        return "::".join(part for part in self if part)


class Result(NamedTuple):
    module: str
    cls: object
    name: str
    skipped: bool
    reason: object


def added_tests(wt, base, sha):
    diff = sh(["git", "diff", "--unified=0", f"{base}...{sha}", "--", "*.py"],
              cwd=wt)
    tests, path, line = [], None, 0
    for text in diff.splitlines():
        if text.startswith("+++ "):
            path = text[6:] if text.startswith("+++ b/") else None
        elif hunk := HUNK.match(text):
            line = int(hunk.group(1))
        elif text.startswith("+") and path:
            if match := ADDED_TEST.match(text):
                tests.append((path, line, match.group(1)))
            line += 1
    return [Test(path, _class_at(wt, sha, path, line), name)
            for path, line, name in tests]


def _class_at(wt, sha, path, line):
    try:
        tree = ast.parse(sh(["git", "show", f"{sha}:{path}"], cwd=wt))
    except SyntaxError:
        return None
    return next((node.name for node in ast.walk(tree)
                 if isinstance(node, ast.ClassDef)
                 and any(getattr(child, "lineno", None) == line
                         for child in node.body)), None)


def _verbose(clause, run, flags):
    matches = list(run.finditer(clause))
    for match in reversed(matches):
        end = ARGS_END.search(clause, match.end()).start()
        args = QUIET.sub("", clause[match.end():end])
        clause = clause[:match.end()] + flags + args + clause[end:]
    return clause, bool(matches)


def _discover_root(args):
    found, positional, words = {}, [], iter(args)
    for word in words:
        flag, equals, value = word.partition("=")
        if flag in DISCOVER_VALUED:
            value = value if equals else next(words, "")
            found[DISCOVER_VALUED[flag] or flag] = value
        elif word[:2] in ("-s", "-t") and len(word) > 2:
            found[DISCOVER_VALUED[word[:2]]] = word[2:]
        elif not word.startswith("-"):
            positional.append(word)
    start = found.get("start") or (positional[:1] or ["."])[0]
    return found.get("top") or (positional[2:3] or [start])[0]


def _root_prefix(clause, moved):
    match, *more = UNITTEST_RUN.finditer(clause)
    if moved or more or not match.group().endswith("discover"):
        return ""
    end = ARGS_END.search(clause, match.end()).start()
    try:
        root = posixpath.normpath(_discover_root(
            shlex.split(clause[match.end():end])))
    except ValueError:
        return ""
    return root.replace("/", ".") if PLAIN_ROOT.fullmatch(root) else ""


def probe_command(verify_cmd):
    clauses, probed, moved = [], False, False
    for line in (verify_cmd or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        for clause in split_and_clauses(line) or [line]:
            moved = moved or bool(CHDIR.search(clause))
            if not INSTALL.search(clause):
                if UNITTEST_RUN.search(clause):
                    root = f"{ROOT_MARK}{_root_prefix(clause, moved)}"
                    clauses.append(f"echo {shlex.quote(root)}")
                clause, units = _verbose(clause, UNITTEST_RUN, " -v")
                clause, pytests = _verbose(clause, PYTEST_RUN, " -vv -rs")
                probed = probed or units or pytests
            clauses.append(clause.strip())
    return " ; ".join(["export COLUMNS=1000", *clauses]) if probed else None


def _reason(status):
    try:
        return str(ast.literal_eval(status.removeprefix("skipped ")))
    except (ValueError, SyntaxError):
        return status.removeprefix("skipped ")


def _module(path):
    return ".".join(PurePosixPath(path).with_suffix("").parts)


def _in_module(module, result):
    return f".{module}".endswith(f".{result.module}")


def _unittest_result(name, dotted, status):
    parts, skipped = dotted.split("."), status.startswith("skipped ")
    reason = _reason(status) if skipped else None
    if parts[:3] == MODULE_SKIPPED:
        return Result(name, None, MODULE_FIXTURE, skipped, reason)
    if name == MODULE_FIXTURE:
        return Result(dotted, None, MODULE_FIXTURE, skipped, reason)
    if name == CLASS_FIXTURE:
        return Result(".".join(parts[:-1]), parts[-1], CLASS_FIXTURE,
                      skipped, reason)
    return Result(".".join(parts[:-2]), parts[-2], name, skipped, reason)


def _rooted(result, prefix):
    return result._replace(module=f"{prefix}.{result.module}") if prefix else result


def _results(output):
    segments = ROOT_LINE.split(output)
    results = [_rooted(_unittest_result(*found), prefix)
               for prefix, segment in zip(["", *segments[1::2]],
                                          segments[::2])
               for found in UNITTEST_RESULT.findall(segment)]
    for path, cls, name, status, reason in PYTEST_RESULT.findall(output):
        results.append(Result(_module(path),
                              cls.split("::")[-1] if cls else None, name,
                              status == "SKIPPED", reason or None))
    return results


def _ran_unseen(output):
    unittest = sum(1 for name, _, _ in UNITTEST_RESULT.findall(output)
                   if name not in (MODULE_FIXTURE, CLASS_FIXTURE))
    pytest = sum(1 for *_, status, _ in PYTEST_RESULT.findall(output)
                 if status != "SKIPPED")
    return (sum(map(int, UNITTEST_SUMMARY.findall(output))) > unittest
            or sum(int(count) for summary in PYTEST_SUMMARY.findall(output)
                   for count in PYTEST_RAN.findall(summary)) > pytest)


def _same_file(path, other):
    return f"/{path}".endswith(f"/{other}") or f"/{other}".endswith(f"/{path}")


def _fixture_reason(test, mine, output):
    for fixture in ((test.cls, CLASS_FIXTURE), (None, MODULE_FIXTURE)):
        reason = next((result.reason for result in mine
                       if result[1:3] == fixture and result.skipped), None)
        if reason:
            return reason
    return next((reason for where, reason in PYTEST_SKIP.findall(output)
                 if _same_file(test.path, where)), None)


def skipped_on_base(output, added):
    summarised = UNITTEST_SUMMARY.search(output) or PYTEST_SUMMARY.search(output)
    if not summarised or _ran_unseen(output):
        return None
    results = _results(output)
    found = []
    for test in added:
        module = _module(test.path)
        mine = [result for result in results if _in_module(module, result)]
        own = [result for result in mine
               if result[1:3] == (test.cls, test.name)] or [
            result for result in mine if result.name == test.name]
        if any(not result.skipped for result in own):
            return None
        found.append((test.id(), next(
            (result.reason for result in own if result.reason), None)
            or _fixture_reason(test, mine, output) or NOT_COLLECTED))
    return found


def brief(skipped):
    listed = "\n".join(f"- {test}: {reason}" for test, reason in skipped)
    return ("The tests this candidate adds did not run where the evidence "
            "check runs: each was skipped or never collected, so they show "
            f"nothing about the base.\n{listed}\n\nThe new tests were skipped"
            " where the evidence check runs, so add a reproducing test that "
            "runs without that gate: no skip condition, no environment the "
            "ticket's verify commands do not provide, collected by those "
            "commands, and exercising the path the ticket reports.")

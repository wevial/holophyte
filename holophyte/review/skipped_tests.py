import ast
import re
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


class Test(NamedTuple):
    path: str
    cls: object
    name: str

    def id(self):
        return "::".join(part for part in self if part)


class Result(NamedTuple):
    stem: str
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


def probe_command(verify_cmd):
    clauses, probed = [], False
    for line in (verify_cmd or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        for clause in split_and_clauses(line) or [line]:
            if not INSTALL.search(clause):
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


def _unittest_result(name, dotted, status):
    parts, skipped = dotted.split("."), status.startswith("skipped ")
    reason = _reason(status) if skipped else None
    if name == MODULE_FIXTURE or parts[:3] == MODULE_SKIPPED:
        return Result(parts[-1], None, MODULE_FIXTURE, skipped, reason)
    if name == CLASS_FIXTURE:
        return Result(parts[-2], parts[-1], CLASS_FIXTURE, skipped, reason)
    return Result(parts[-3] if len(parts) > 2 else "", parts[-2], name,
                  skipped, reason)


def _results(output):
    results = [_unittest_result(*found)
               for found in UNITTEST_RESULT.findall(output)]
    for path, cls, name, status, reason in PYTEST_RESULT.findall(output):
        results.append(Result(PurePosixPath(path).stem,
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


def _fixture_reason(test, results, output):
    stem = PurePosixPath(test.path).stem
    for fixture in ((stem, test.cls, CLASS_FIXTURE),
                    (stem, None, MODULE_FIXTURE)):
        reason = next((result.reason for result in results
                       if result[:3] == fixture and result.skipped), None)
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
        stem = PurePosixPath(test.path).stem
        own = [result for result in results
               if result[:3] == (stem, test.cls, test.name)] or [
            result for result in results
            if (result.stem, result.name) == (stem, test.name)]
        if any(not result.skipped for result in own):
            return None
        found.append((test.id(), next(
            (result.reason for result in own if result.reason), None)
            or _fixture_reason(test, results, output) or NOT_COLLECTED))
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

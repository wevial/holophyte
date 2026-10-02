import ast
import re
import shlex
from pathlib import PurePosixPath

from holophyte.loop.gates import sh, split_and_clauses

UNITTEST_RUN = re.compile(r"-m\s+unittest(?:\s+discover)?(?=\s|$)")
PYTEST_RUN = re.compile(r"(?:-m\s+pytest|(?<![\w.-])pytest)(?=\s|$)")
INSTALL = re.compile(r"\bpip3?\b")
UNITTEST_SUMMARY = re.compile(r"^Ran (\d+) tests? in ", re.M)
PYTEST_SUMMARY = re.compile(r"^=+ ([^=\n]*?) in [\d.]+s\b[^=\n]*=+\s*$", re.M)
PYTEST_RAN = re.compile(r"\b(\d+) (?:passed|failed|errors?|xfailed|xpassed)\b")
UNITTEST_RESULT = re.compile(r"^(\w+) \(([\w.]+)\)\n?[^\n]*? \.\.\. (.*)$", re.M)
PYTEST_RESULT = re.compile(
    r"^(\S+\.py)::(?:\S+::)?(\w+)(?:\[\S*\])? ([A-Z]+)(?: \((.*?)\))?\s+\[", re.M)
PYTEST_SKIP = re.compile(r"^SKIPPED \[\d+\] (\S+?\.py)(?::\d+)?: (.*)$", re.M)
FIXTURES = ("setUpModule", "setUpClass")
MODULE_SKIPPED = "unittest.loader.ModuleSkipped"
ADDED_TEST = re.compile(r"^\+\s*(?:async\s+)?def (test\w*)\(")
NOT_COLLECTED = "not collected by the verify commands"


def added_tests(wt, base, sha):
    diff = sh(["git", "diff", "--unified=0", f"{base}...{sha}", "--", "*.py"],
              cwd=wt)
    tests, path = [], None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and (match := ADDED_TEST.match(line)):
            tests.append((path, match.group(1)))
    return tests


def probe_command(verify_cmd, names):
    names = list(dict.fromkeys(names))
    unittest = " -v" + "".join(f" -k {shlex.quote(name)}" for name in names)
    pytest = " -vv -rs -k " + shlex.quote(" or ".join(names))
    clauses, probed = [], False
    for line in (verify_cmd or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        for clause in split_and_clauses(line) or [line]:
            if not INSTALL.search(clause):
                clause, units = UNITTEST_RUN.subn(
                    lambda run: run.group(0) + unittest, clause)
                clause, pytests = PYTEST_RUN.subn(
                    lambda run: run.group(0) + pytest, clause)
                probed = probed or bool(units or pytests)
            clauses.append(clause.strip())
    return " ; ".join(["export COLUMNS=1000", *clauses]) if probed else None


def _reason(status):
    try:
        return str(ast.literal_eval(status.removeprefix("skipped ")))
    except (ValueError, SyntaxError):
        return status.removeprefix("skipped ")


def _results(output):
    results = []
    for name, dotted, status in UNITTEST_RESULT.findall(output):
        skipped = status.startswith("skipped ")
        results.append((dotted.split("."), name, skipped,
                        _reason(status) if skipped else None))
    for path, name, status, reason in PYTEST_RESULT.findall(output):
        results.append(((*PurePosixPath(path).with_suffix("").parts, name),
                        name, status == "SKIPPED", reason or None))
    return results


def _ran_unseen(output):
    unittest = sum(1 for name, _, _ in UNITTEST_RESULT.findall(output)
                   if name not in FIXTURES)
    pytest = sum(1 for *_, status, _ in PYTEST_RESULT.findall(output)
                 if status != "SKIPPED")
    return (sum(map(int, UNITTEST_SUMMARY.findall(output))) > unittest
            or sum(int(count) for summary in PYTEST_SUMMARY.findall(output)
                   for count in PYTEST_RAN.findall(summary)) > pytest)


def _same_file(path, other):
    return f"/{path}".endswith(f"/{other}") or f"/{other}".endswith(f"/{path}")


def _module_reason(path, results, output):
    stem = PurePosixPath(path).stem
    for where, name, skipped, reason in results:
        fixture = name in FIXTURES and stem in where
        at_import = where[:-1] == MODULE_SKIPPED.split(".") and name == stem
        if skipped and (fixture or at_import):
            return reason
    return next((reason for where, reason in PYTEST_SKIP.findall(output)
                 if _same_file(path, where)), None)


def skipped_on_base(output, added):
    summarised = UNITTEST_SUMMARY.search(output) or PYTEST_SUMMARY.search(output)
    if not summarised or _ran_unseen(output):
        return None
    results = _results(output)
    found = []
    for path, name in added:
        stem = PurePosixPath(path).stem
        own = [(skipped, reason) for where, test, skipped, reason in results
               if test == name and stem in where[:-1]]
        if any(not skipped for skipped, _ in own):
            return None
        found.append((f"{path}::{name}", next(
            (reason for _, reason in own if reason), None)
            or _module_reason(path, results, output) or NOT_COLLECTED))
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

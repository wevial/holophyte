import re
import shlex
from pathlib import PurePosixPath

from holophyte.loop.gates import sh

UNITTEST_SUMMARY = re.compile(
    r"^Ran \d+ tests? in [^\n]*\n\s*(?:OK|FAILED|NO TESTS RAN)(?: \((.*)\))?",
    re.M)
PYTEST_SUMMARY = re.compile(r"^=+ ([^=\n]*?) in [\d.]+s\b[^=\n]*=+\s*$", re.M)
VERBOSE_RESULT = re.compile(
    r"^(test\w*) \([\w.]+\)\n?[^\n]*? \.\.\. (.*)$", re.M)
SKIP = re.compile(r"skipped '(.*)'")
ADDED_TEST = re.compile(r"^\+\s*(?:async\s+)?def (test\w*)\(")


def skip_counts(output):
    unittest = (re.search(r"\bskipped=(\d+)", match.group(1) or "")
                for match in UNITTEST_SUMMARY.finditer(output))
    pytest = (re.search(r"\b(\d+) skipped\b", match.group(1))
              for match in PYTEST_SUMMARY.finditer(output))
    return [int(found.group(1)) if found else 0
            for found in (*unittest, *pytest)]


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


def verbose_command(path, name):
    folder, module = PurePosixPath(path).parent, PurePosixPath(path).name
    return " ".join(["python3 -m unittest discover -v -s",
                     shlex.quote(str(folder)), "-p", shlex.quote(module),
                     "-k", shlex.quote(name)])


def skipped_on_base(output, added, run):
    if not added or sum(skip_counts(output)) < len(added):
        return None
    skipped = []
    for path, name in added:
        results = dict(VERBOSE_RESULT.findall(run(verbose_command(path, name))))
        skip = SKIP.fullmatch(results.get(name, ""))
        if not skip:
            return None
        skipped.append((f"{path}::{name}", skip.group(1)))
    return skipped


def brief(skipped):
    listed = "\n".join(f"- {test}: {reason}" for test, reason in skipped)
    return ("The tests this candidate adds did not run where the evidence "
            "check runs: every one was skipped, so they show nothing about "
            f"the base.\n{listed}\n\nThe new tests were skipped where the "
            "evidence check runs, so add a reproducing test that runs without"
            " that gate: no skip condition and no environment the ticket's "
            "verify commands do not provide, exercising the path the ticket "
            "reports.")

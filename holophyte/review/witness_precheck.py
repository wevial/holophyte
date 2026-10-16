"""Each criterion's named test file, checked from git and a verbose verify
rerun before the first review: it exists, the branch adds or changes it,
and the verify commands ran it."""
import json
import re
import subprocess
from fnmatch import fnmatchcase
from functools import partial

import store
from holophyte.config.reader import VERIFY_TIMEOUT
from holophyte.loop.gates import _verify_command, sh
from holophyte.loop.runs import heartbeat_while
from holophyte.redact import safe_print as print
from holophyte.review import skipped_tests
from store.working import working

SPAN = re.compile(r"`([^`\n]+)`")
TEST_FILE = re.compile(r"test_.*\.py|.*_test\.(?:py|go)"
                       r"|.*\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs)")
UNITTEST = re.compile(r"-m\s+unittest(?=\s|$)(.*)")
DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
SAME = re.compile(r"\b(?:the same (?:test file|file|test class|spec|check)"
                  r"|that test)\b", re.I)
EXISTING = re.compile(r"unchanged|existing|already|still pass|as before", re.I)
GLOB = re.compile(r"[*?[]")
FIXTURES = (skipped_tests.MODULE_FIXTURE, skipped_tests.CLASS_FIXTURE)
NOT_RUN = "witness pre-check not run: verify failed"


def _dotted_path(word):
    parts = word.split(".")
    for end, part in enumerate(parts, 1):
        if TEST_FILE.fullmatch(f"{part}.py"):
            return "/".join(parts[:end]) + ".py"
    return None


def _span_files(span):
    if unittest := UNITTEST.search(span):
        return [path for word in unittest.group(1).split()
                if DOTTED.fullmatch(word) and (path := _dotted_path(word))]
    path = span.strip().split("::")[0]
    if not path or any(char.isspace() for char in path):
        return []
    return [path] if TEST_FILE.fullmatch(path.rsplit("/", 1)[-1]) else []


def named_files(criteria):
    named, last = [], []
    for text in criteria:
        files = list(dict.fromkeys(path for span in SPAN.findall(text)
                                   for path in _span_files(span)))
        if not files and SAME.search(text):
            files = last
        last = files or last
        named.append(files)
    return named


def _missed(test, mine, output):
    own = [result for result in mine
           if (result.cls, result.name) == (test.cls, test.name)]
    if any(not result.skipped for result in own):
        return None
    return (next((result.reason for result in own if result.reason), None)
            or skipped_tests.fixture_reason(test, mine, output)
            or skipped_tests.NOT_COLLECTED)


def _ran(paths, probed):
    if probed is None or not all(path.endswith(".py") for path in paths):
        return "unchecked", []
    output, results, added = probed
    modules = [skipped_tests.module_name(path) for path in paths]
    mine = [result for result in results
            if any(skipped_tests.in_module(module, result)
                   for module in modules)]
    if not mine:
        return "not_run", []
    if all(result.skipped for result in mine if result.name not in FIXTURES):
        return "skipped", list(dict.fromkeys(
            result.reason or "no reason given"
            for result in mine if result.skipped))
    missed = [{"test": test.id(), "reason": reason} for test in added
              if test.path in paths
              and (reason := _missed(test, mine, output))]
    return ("added_skipped", missed) if missed else ("ran", [])


def _gaps(n, path, ran, detail):
    named = f"criterion {n} names `{path}`; "
    if ran == "skipped":
        return [f"{named}its tests ran only skipped in verify "
                f"({'; '.join(detail)})"]
    if ran == "not_run":
        return [f"{named}the verify commands run no test from it"]
    return [f"{named}`{missed['test']}` was skipped in verify "
            f"({missed['reason']})" for missed in detail]


def _file(n, path, existing, tree, changed, sha, probed):
    glob = bool(GLOB.search(path))
    matches = ([found for found in tree if fnmatchcase(found, path)] if glob
               else [path] if path in tree else [])
    result = {"file": path, "exists": bool(matches),
              **({"matches": matches} if glob else {})}
    if not matches:
        return result, [f"criterion {n} names `{path}`; it does not exist "
                        f"at {sha[:12]}"]
    result["changed"] = None if existing else path in changed
    gaps = ([] if result["changed"] is not False else
            [f"criterion {n} names `{path}`; this branch neither adds nor "
             "changes it"])
    result["ran"], detail = _ran(matches, probed)
    if detail:
        result["detail"] = detail
    return result, gaps + _gaps(n, path, result["ran"], detail)


def _probed(wt, base, sha, verify_cmd, wanted, probe):
    command = wanted and skipped_tests.probe_command(verify_cmd)
    if not command:
        return None
    output = probe(command)
    if not (skipped_tests.UNITTEST_SUMMARY.search(output)
            or skipped_tests.PYTEST_SUMMARY.search(output)):
        return None
    source = skipped_tests.Source(wt, sha)
    return (output, skipped_tests.parse_results(output),
            skipped_tests.added_tests(source, base))


def check(wt, base, sha, criteria, verify_cmd, probe):
    """Per criterion: its named files, each check's result, and its gaps."""
    tree = set(sh(["git", "ls-tree", "-r", "--name-only", sha],
                  cwd=wt).splitlines())
    changed = set(sh(["git", "diff", "--name-only", f"{base}...{sha}"],
                     cwd=wt).splitlines())
    named = named_files(criteria)
    wanted = any(path.endswith(".py") for files in named for path in files)
    probed = _probed(wt, base, sha, verify_cmd, wanted, probe)
    entries = []
    for n, (text, files) in enumerate(zip(criteria, named), 1):
        existing = (bool(EXISTING.search(text)) or len(files) > 1
                    or any(GLOB.search(path) for path in files))
        entry = {"criterion": n, "files": files,
                 "named": "ok" if files else "no_file", "checks": [],
                 "gaps": []}
        for path in files:
            result, gaps = _file(n, path, existing, tree, changed, sha, probed)
            entry["checks"].append(result)
            entry["gaps"] += gaps
        entries.append(entry)
    return entries


def summary(sha, entries):
    witnessed = sum(1 for entry in entries
                    if entry["files"] and not entry["gaps"])
    lines = [f"witness pre-check at {sha[:12]}: {witnessed} of "
             f"{len(entries)} criteria witnessed"]
    lines += [gap for entry in entries for gap in entry["gaps"]]
    lines += [f"criterion {entry['criterion']} names no test file"
              for entry in entries if not entry["files"]]
    return "\n".join(lines)


def _probe(project, conn, run_id, beat_s, wt, command):
    with working(conn, run_id, verify=True), \
            heartbeat_while(conn, run_id, beat_s):
        try:
            return _verify_command(project, command, wt,
                                   VERIFY_TIMEOUT)[1] or ""
        except (OSError, subprocess.TimeoutExpired):
            return ""


def record(project, conn, run_id, beat_s, wt, base_sha, sha, criteria,
           verify_cmd, ok):
    if conn is None or run_id is None:
        return
    if not ok:
        store.record_event(conn, run_id, "witness_precheck", NOT_RUN)
        return
    entries = check(wt, base_sha, sha, list(criteria or ()), verify_cmd,
                    partial(_probe, project, conn, run_id, beat_s, wt))
    text = summary(sha, entries)
    print(f"[holo2] {text}")
    store.record_event(conn, run_id, "witness_precheck", text)
    store.record_event(conn, run_id, "witness_precheck_results",
                       text.splitlines()[0], level="detail",
                       payload=json.dumps({"sha": sha, "base": base_sha,
                                           "criteria": entries}))

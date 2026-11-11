import fnmatch
import os
import re
import shlex
import subprocess
from typing import NamedTuple

from holophyte.config.config_tables import verify_config
from holophyte.loop.gates import _word_spans, run_verify, sh, split_and_clauses
from holophyte.review.blast_radius import (
    importers,
    module_name,
    module_names,
    package_dirs,
)
from holophyte.review.briefs import _changed_files, main_ref

IMPORTER_CAP = 10


_DISCOVER_OPTIONS = {"-s": "start", "--start-directory": "start",
                     "-p": "pattern", "--pattern": "pattern",
                     "-t": "top", "--top-level-directory": "top"}
_DISCOVER_POSITIONS = ("start", "pattern", "top")
_SETUP = {"export", "unset", "source", ".", "set"}
_UNREPRODUCIBLE = {"pushd", "popd"}
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")


class UnittestRun(NamedTuple):
    setup: tuple
    cwd: str
    prefix: list
    args: list


def _change_directory(cwd, raw, words):
    if len(words) != 2 or words[1] == "-" or any(c in raw[1] for c in "$~`"):
        return None
    return os.path.normpath(os.path.join(cwd, words[1]))


def unittest_runs(command):
    runs, setup, cwd = [], [], "."
    for line in command.splitlines():
        clauses = split_and_clauses(line, allow_or=True)
        if clauses is None:
            return []
        for clause in clauses:
            spans = _word_spans(clause)
            words = [word for word, _, _ in spans]
            raw = [clause[start:end].strip() for _, start, end in spans]
            if not words or words[0] in _UNREPRODUCIBLE:
                return []
            if words[0] == "cd":
                cwd = _change_directory(cwd, raw, words)
                if cwd is None:
                    return []
                setup.append(clause)
            elif words[0] in _SETUP or all(map(_ASSIGNMENT.match, raw)):
                setup.append(clause)
            for index in range(1, len(words) - 1):
                if words[index:index + 2] == ["-m", "unittest"]:
                    args = words[index + 2:]
                    cut = next((n for n, word in enumerate(args)
                                if word[:1] in "|<>" or ">" in word), len(args))
                    runs.append(UnittestRun(tuple(setup), cwd, raw[:index],
                                            args[:cut]))
                    break
    return runs


def _discovery(args):
    found, positional = {"start": ".", "pattern": "test*.py", "top": None}, []
    words = iter(args[1:] if args[:1] == ["discover"] else args)
    for word in words:
        name, eq, value = word.partition("=")
        if name in _DISCOVER_OPTIONS:
            found[_DISCOVER_OPTIONS[name]] = value if eq else next(words, None)
        elif word == "-k":
            next(words, None)
        elif not word.startswith("-"):
            positional.append(word)
    found.update(zip(_DISCOVER_POSITIONS, positional))
    return found


def _discovered(start, test, packages):
    folders = os.path.relpath(os.path.dirname(test) or ".", start).split("/")
    if folders[0] == "..":
        return False
    return folders == ["."] or all(
        os.path.normpath(os.path.join(start, *folders[:depth])) in packages
        for depth in range(1, len(folders) + 1))


def _covers(run, test, packages):
    named = [word for word in run.args if not word.startswith("-")]
    if any(word.startswith("-k") for word in run.args):
        return False
    if run.args[:1] == ["discover"] or not named:
        found = _discovery(run.args)
        start = os.path.normpath(os.path.join(run.cwd, found["start"] or "."))
        return (not os.path.isabs(start)
                and _discovered(start, test, packages)
                and fnmatch.fnmatchcase(os.path.basename(test),
                                        found["pattern"] or "test*.py"))
    return (module_name(os.path.relpath(test, run.cwd))
            in {module_name(word) for word in named})


def _top_level(runs):
    for run in runs:
        if run.cwd == runs[0].cwd and (run.args[:1] == ["discover"] or all(
                word.startswith("-") for word in run.args)):
            found = _discovery(run.args)
            return os.path.normpath(found["top"] or found["start"] or ".")
    return "."


def _module_clause(run, top, test):
    under_top = os.path.normpath(os.path.join(run.cwd, top))
    for base in (under_top, run.cwd):
        relative = os.path.relpath(test, base)
        if not (os.path.isabs(base) or relative.startswith("..")):
            prefix = (_with_path(run.prefix, top) if base != run.cwd
                      else " ".join(run.prefix))
            return f"{prefix} -m unittest {module_name(relative)}"
    return None


def _with_path(prefix, top):
    assigned = [n for n, word in enumerate(prefix)
                if word.startswith("PYTHONPATH=")]
    if not assigned:
        return (f"PYTHONPATH={shlex.quote(top)}${{PYTHONPATH:+:$PYTHONPATH}} "
                f"{' '.join(prefix)}")
    words, last = list(prefix), assigned[-1]
    value = words[last].partition("=")[2]
    words[last] = f"PYTHONPATH={shlex.quote(top)}" + (f":{value}" if value else "")
    return " ".join(words)


def _test_module(path):
    name = os.path.basename(path)
    return ((fnmatch.fnmatchcase(name, "test*.py") or name.endswith("_test.py"))
            and all(part.isidentifier() for part in module_name(path).split(".")))


def _distance(one, other):
    one, other = one.split("/")[:-1], other.split("/")[:-1]
    shared = len(os.path.commonprefix([one, other]))
    return len(one) + len(other) - 2 * shared


def _importing_tests(root, runs):
    try:
        base = sh(["git", "merge-base", main_ref(root), "HEAD"], cwd=root)
        changed = [path for path in _changed_files(root, base, "HEAD")
                   if path.endswith(".py")]
        packages = package_dirs(root, "HEAD") if changed else set()
        modules = {name: path for path in changed
                   for name in module_names(path, packages)}
        found = importers(root, "HEAD", modules) if modules else {}
    except (RuntimeError, OSError, subprocess.CalledProcessError):
        return []
    nearest = {}
    for source, files in found.items():
        for test in files:
            if (_test_module(test)
                    and not any(_covers(run, test, packages) for run in runs)):
                distance = _distance(test, source)
                nearest[test] = min(distance, nearest.get(test, distance))
    return sorted(nearest, key=lambda test: (nearest[test], test))


def run_importing_tests(project, wt, command, conn, run_id):
    runs = unittest_runs(command)
    if not runs:
        return True, None
    top = _top_level(runs)
    clauses = {}
    for test in _importing_tests(wt, runs):
        if len(clauses) < IMPORTER_CAP:
            clause = _module_clause(runs[0], top, test)
            if clause is not None:
                clauses[test] = clause
    if not clauses:
        return True, None
    tests, setup = list(clauses), runs[0].setup
    joined = " && ".join([*setup, *clauses.values()])
    ok, out = run_verify(joined, wt, timeout=verify_config(project).timeout_sec,
                         conn=conn, run_id=run_id, project=project)
    if ok:
        return True, {"source": "importers", "command": joined, "exitCode": 0,
                      "output": f"[verify] tests importing a changed module "
                                f"passed: {', '.join(tests)}\n{out}"}
    failure = getattr(out, "failure", None) or {}
    index = (failure.get("command_index") or 1) - len(setup)
    named = tests[index - 1] if 1 <= index <= len(tests) else ", ".join(tests)
    report = (f"[verify] FAILED: {named} imports a changed module and failed\n"
              f"{out}")
    return False, {"source": "importers", "command": joined, "exitCode": 1,
                   "output": report, "failure": failure or None}

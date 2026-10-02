"""KO-639: run every test module in its own process, several at a time.

Each `test_*.py` under the tests directory runs as
`python3 -m unittest discover -s TESTS -p FILE` from the directory above it
(the `discover -p` form, because some modules import sibling helpers such as
`serve_fixture` that `-m unittest tests.NAME` cannot find), in a session of
its own with a fresh temporary `HOLOPHYTE_HOME`, up to `--jobs` at a time.
The largest files start first, a cheap stand-in for the slowest.

A module still running after `--module-timeout` seconds has its process
group killed and fails as timed out.

It prints one line per module as it finishes, then the output of every
failing module (the last lines only, for one that timed out), and a summary
line naming each module that failed. It exits non-zero when any module fails,
when a module runs zero tests, when no module is found, or when a given
pattern matches no module.

Given module paths or globs (`tests/test_file_sizes.py`, `tests/test_docs*.py`),
it runs only the modules whose file names match; otherwise every module.

Run: python3 tests/run_modules.py [--jobs N] [--module-timeout SECONDS]
                                  [--dir TESTS] [MODULE ...]
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
TAIL_LINES = 40


def run_module(tests, path, timeout=None):
    """Run one module; returns `(name, seconds, tests_ran, returncode, output)`,
    with a returncode of None when it outran `timeout` seconds.

    Output goes to a file rather than a pipe, so a straggler the module left
    holding its stdout cannot stall the wait; the module's whole process
    group is killed once it exits or times out.
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="holophyte-home-") as home, \
            tempfile.TemporaryFile("w+") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "unittest", "discover",
             "-s", str(tests), "-p", path.name],
            cwd=tests.parent, env=dict(os.environ, HOLOPHYTE_HOME=home),
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        out.seek(0)
        output = out.read()
    counts = RAN.findall(output)
    ran = int(counts[-1]) if counts else 0
    return path.name, time.monotonic() - started, ran, code, output


def verdict(ran, code, timeout=None):
    if code is None:
        return f"FAILED (timed out after {timeout:g} s)"
    if code != 0 and ran:
        return f"FAILED (exit {code})"
    if ran == 0:
        return "FAILED (ran zero tests)"
    return "ok"


def select(modules, patterns):
    """The modules whose file names match a pattern, in their given order,
    and the patterns that matched none."""
    names = [Path(pattern).name for pattern in patterns]
    chosen = [path for path in modules
              if any(fnmatch.fnmatchcase(path.name, name) for name in names)]
    unmatched = [pattern for pattern, name in zip(patterns, names)
                 if not any(fnmatch.fnmatchcase(path.name, name)
                            for path in modules)]
    return chosen, unmatched


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="modules to run at once (default: CPU count)")
    parser.add_argument("--module-timeout", type=float, default=300,
                        metavar="SECONDS",
                        help="kill and fail a module still running after "
                             "this long (default: 300)")
    parser.add_argument("--dir", type=Path, default=Path(__file__).parent,
                        help="tests directory (default: this file's)")
    parser.add_argument("modules", nargs="*", metavar="MODULE",
                        help="module path or glob to run (default: every "
                             "module); only its file name is matched")
    args = parser.parse_args(argv)
    tests = args.dir.resolve()
    modules = sorted(tests.glob("test_*.py"),
                     key=lambda p: (-p.stat().st_size, p.name))
    if args.modules:
        modules, unmatched = select(modules, args.modules)
        if unmatched:
            for pattern in unmatched:
                print(f"{pattern}: matches no test_*.py module in {tests}",
                      flush=True)
            return 1
    if not modules:
        print(f"no test_*.py module found in {tests}", flush=True)
        return 1
    started = time.monotonic()
    failed, total = [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_module, tests, path, args.module_timeout)
                   for path in modules]
        for future in as_completed(futures):
            name, seconds, ran, code, output = future.result()
            total += ran
            result = verdict(ran, code, args.module_timeout)
            print(f"{name:<48} {seconds:7.1f}s  {ran:5d} tests  {result}",
                  flush=True)
            if code is None:
                output = "\n".join(output.splitlines()[-TAIL_LINES:])
            if result != "ok":
                failed.append((name, result, output))
    for name, result, output in sorted(failed):
        print(f"\n===== {name}: {result} =====\n{output.rstrip()}", flush=True)
    names = ", ".join(name for name, _, _ in sorted(failed))
    print(f"\n{len(modules)} modules, {total} tests, {len(failed)} failed, "
          f"{time.monotonic() - started:.1f}s"
          + (f"; failed: {names}" if failed else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

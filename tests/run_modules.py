"""KO-639: run every test module in its own process, several at a time.

Each `test_*.py` under the tests directory runs as
`python3 -m unittest discover -s TESTS -p FILE` from the directory above it
(the `discover -p` form, because some modules import sibling helpers such as
`serve_fixture` that `-m unittest tests.NAME` cannot find), in a session of
its own with a fresh temporary `HOLOPHYTE_HOME`, up to `--jobs` at a time.
The largest files start first, a cheap stand-in for the slowest.

It prints one line per module as it finishes, then the full output of every
failing module, and exits non-zero when any module fails, when a module runs
zero tests, or when no module is found.

Run: python3 tests/run_modules.py [--jobs N] [--dir TESTS]
"""

from __future__ import annotations

import argparse
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


def run_module(tests, path):
    """Run one module; returns `(name, seconds, tests_ran, returncode, output)`.

    Output goes to a file rather than a pipe, so a straggler the module left
    holding its stdout cannot stall the wait; the module's whole process
    group is killed once it exits.
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
        code = proc.wait()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        out.seek(0)
        output = out.read()
    counts = RAN.findall(output)
    ran = int(counts[-1]) if counts else 0
    return path.name, time.monotonic() - started, ran, code, output


def verdict(ran, code):
    if code != 0 and ran:
        return f"FAILED (exit {code})"
    if ran == 0:
        return "FAILED (ran zero tests)"
    return "ok"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="modules to run at once (default: CPU count)")
    parser.add_argument("--dir", type=Path, default=Path(__file__).parent,
                        help="tests directory (default: this file's)")
    args = parser.parse_args(argv)
    tests = args.dir.resolve()
    modules = sorted(tests.glob("test_*.py"),
                     key=lambda p: (-p.stat().st_size, p.name))
    if not modules:
        print(f"no test_*.py module found in {tests}", flush=True)
        return 1
    started = time.monotonic()
    failed, total = [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_module, tests, path) for path in modules]
        for future in as_completed(futures):
            name, seconds, ran, code, output = future.result()
            total += ran
            result = verdict(ran, code)
            print(f"{name:<48} {seconds:7.1f}s  {ran:5d} tests  {result}",
                  flush=True)
            if result != "ok":
                failed.append((name, result, output))
    for name, result, output in sorted(failed):
        print(f"\n===== {name}: {result} =====\n{output.rstrip()}", flush=True)
    print(f"\n{len(modules)} modules, {total} tests, {len(failed)} failed, "
          f"{time.monotonic() - started:.1f}s", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

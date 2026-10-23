"""KO-639: run every test module in its own process, several at a time.

Each `test_*.py` under the tests directory runs as
`python3 -m unittest discover -s TESTS -p FILE` from the directory above it
(the `discover -p` form, because some modules import sibling helpers such as
`serve_fixture` that `-m unittest tests.NAME` cannot find), in a session of
its own with a fresh temporary `HOLOPHYTE_HOME`, up to `--jobs` at a time.
The modules with the longest times recorded in `module_times.json` in the
tests directory start first; a module it does not record counts as the median
of the recorded times, and a recorded name with no module is ignored.

`--shard I/N` runs shard I of N: the selected modules split longest first,
each to the shard with the smallest recorded total so far (ties to the lower
shard), so each module is in exactly one shard. `--list` prints the modules
the run would run, one per line in run order, and runs none. `--record PATH`
writes each module's wall seconds from this run to PATH, in the times file's
format.

A module still running after `--module-timeout` seconds has its process
group killed and fails as timed out. With `--max-module-sec`, a module that
finished but ran longer than that fails as over the ceiling.

It prints one line per module as it finishes, with its wall and CPU seconds,
then the output of every failing module (the last lines only, for one that
timed out), the ten slowest modules, and a summary line naming each module
that failed. It exits non-zero when any module fails, when a module runs zero
tests, when no module is found, or when a given pattern matches no module.

Given module paths or globs (`tests/test_file_sizes.py`, `tests/test_docs*.py`),
it runs only the modules whose file names match; otherwise every module.

Run: python3 tests/run_modules.py [--jobs N] [--module-timeout SECONDS]
                                  [--max-module-sec SECONDS] [--shard I/N]
                                  [--list] [--record PATH] [--dir TESTS]
                                  [MODULE ...]
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
TAIL_LINES = 40
SLOWEST = 10
TIMES_FILE = "module_times.json"
POLL_SECONDS = 0.05


def wait_for(pid, timeout=None):
    """Reap `pid`; returns `(returncode, cpu_seconds)`, with a returncode of
    None when it outran `timeout` seconds and was left running."""
    if timeout is None:
        _, status, usage = os.wait4(pid, 0)
    else:
        deadline = time.monotonic() + timeout
        while True:
            reaped, status, usage = os.wait4(pid, os.WNOHANG)
            if reaped:
                break
            left = deadline - time.monotonic()
            if left <= 0:
                return None, None
            time.sleep(min(POLL_SECONDS, left))
    return (os.waitstatus_to_exitcode(status),
            usage.ru_utime + usage.ru_stime)


def run_module(tests, path, timeout=None):
    """Run one module; returns
    `(name, seconds, cpu_seconds, tests_ran, returncode, output)`,
    with a returncode of None when it outran `timeout` seconds.

    Output goes to a file rather than a pipe, so a straggler the module left
    holding its stdout cannot stall the wait; the module's whole process
    group is killed once it exits or times out.
    """
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="holophyte-home-") as home, \
            tempfile.TemporaryFile("w+") as out:
        env = dict(os.environ, HOLOPHYTE_HOME=home)
        index = int(env.get("GIT_CONFIG_COUNT", "0"))
        env.update({f"GIT_CONFIG_KEY_{index}": "maintenance.auto",
                    f"GIT_CONFIG_VALUE_{index}": "false",
                    "GIT_CONFIG_COUNT": str(index + 1)})
        proc = subprocess.Popen(
            [sys.executable, "-m", "unittest", "discover",
             "-s", str(tests), "-p", path.name],
            cwd=tests.parent, env=env,
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True)
        code, cpu = wait_for(proc.pid, timeout)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if code is None:
            _, cpu = wait_for(proc.pid)
        proc.returncode = code if code is not None else -signal.SIGKILL
        out.seek(0)
        output = out.read()
    counts = RAN.findall(output)
    ran = int(counts[-1]) if counts else 0
    return path.name, time.monotonic() - started, cpu, ran, code, output


def verdict(ran, code, timeout=None, seconds=0.0, ceiling=None):
    if code is None:
        return f"FAILED (timed out after {timeout:g} s)"
    if code != 0 and ran:
        return f"FAILED (exit {code})"
    if ran == 0:
        return "FAILED (ran zero tests)"
    if ceiling is not None and seconds > ceiling:
        return f"FAILED (ran {seconds:.1f} s, over the {ceiling:g} s ceiling)"
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


def recorded_times(tests, modules):
    """Each module's recorded seconds, the median standing in for one the
    times file does not name."""
    path = tests / TIMES_FILE
    recorded = json.loads(path.read_text()) if path.exists() else {}
    known = {p.name: float(recorded[p.name]) for p in modules
             if p.name in recorded}
    fallback = statistics.median(known.values()) if known else 0.0
    return {p.name: known.get(p.name, fallback) for p in modules}


def by_time(modules, times):
    return sorted(modules, key=lambda p: (-times[p.name], p.name))


def shard_of(modules, times, index, count):
    """Shard `index` (1-based) of `count`, filled longest first into the
    shard with the smallest total so far."""
    shards = [[] for _ in range(count)]
    totals = [0.0] * count
    for path in by_time(modules, times):
        least = min(range(count), key=lambda i: (totals[i], i))
        shards[least].append(path)
        totals[least] += times[path.name]
    return shards[index - 1]


def shard_arg(value):
    match = re.fullmatch(r"(\d+)/(\d+)", value)
    if not match:
        raise argparse.ArgumentTypeError(f"{value!r} is not I/N")
    index, count = int(match[1]), int(match[2])
    if count < 1 or not 1 <= index <= count:
        raise argparse.ArgumentTypeError(
            f"{value!r} needs 1 <= I <= N and N >= 1")
    return index, count


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="modules to run at once (default: CPU count)")
    parser.add_argument("--module-timeout", type=float, default=300,
                        metavar="SECONDS",
                        help="kill and fail a module still running after "
                             "this long (default: 300)")
    parser.add_argument("--max-module-sec", type=float, metavar="SECONDS",
                        help="fail a module that finished but ran longer "
                             "than this (default: no ceiling)")
    parser.add_argument("--shard", type=shard_arg, metavar="I/N",
                        help="run shard I of N, balanced by recorded times")
    parser.add_argument("--list", action="store_true",
                        help="print the modules the run would run, in run "
                             "order, and run none")
    parser.add_argument("--record", type=Path, metavar="PATH",
                        help="write each module's wall seconds to PATH")
    parser.add_argument("--dir", type=Path, default=Path(__file__).parent,
                        help="tests directory (default: this file's)")
    parser.add_argument("modules", nargs="*", metavar="MODULE",
                        help="module path or glob to run (default: every "
                             "module); only its file name is matched")
    return parser.parse_args(argv)


def chosen_modules(args, tests):
    """The modules to run in run order, or None after naming the problem."""
    modules = sorted(tests.glob("test_*.py"))
    if args.modules:
        modules, unmatched = select(modules, args.modules)
        if unmatched:
            for pattern in unmatched:
                print(f"{pattern}: matches no test_*.py module in {tests}",
                      flush=True)
            return None
    if not modules:
        print(f"no test_*.py module found in {tests}", flush=True)
        return None
    times = recorded_times(tests, modules)
    if args.shard:
        modules = shard_of(modules, times, *args.shard)
        if not modules and not args.list:
            print(f"shard {args.shard[0]}/{args.shard[1]} holds no module",
                  flush=True)
            return None
    return by_time(modules, times)


def run_all(args, tests, modules):
    """Run `modules`, printing a line for each; returns
    `(results, failed, total_tests)`."""
    results, failed, total = [], [], 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_module, tests, path, args.module_timeout)
                   for path in modules]
        for future in as_completed(futures):
            name, seconds, cpu, ran, code, output = future.result()
            results.append((name, seconds))
            total += ran
            result = verdict(ran, code, args.module_timeout, seconds,
                             args.max_module_sec)
            print(f"{name:<48} {seconds:7.1f}s {cpu:7.1f}s cpu  "
                  f"{ran:5d} tests  {result}", flush=True)
            if code is None:
                output = "\n".join(output.splitlines()[-TAIL_LINES:])
            if result != "ok":
                failed.append((name, result, output))
    return results, failed, total


def main(argv=None):
    args = parse_args(argv)
    tests = args.dir.resolve()
    modules = chosen_modules(args, tests)
    if modules is None:
        return 1
    if args.list:
        for path in modules:
            print(path.name, flush=True)
        return 0
    started = time.monotonic()
    results, failed, total = run_all(args, tests, modules)
    for name, result, output in sorted(failed):
        print(f"\n===== {name}: {result} =====\n{output.rstrip()}", flush=True)
    slowest = sorted(results, key=lambda r: (-r[1], r[0]))[:SLOWEST]
    print(f"\n{len(slowest)} slowest modules:", flush=True)
    for name, seconds in slowest:
        print(f"  {name:<48} {seconds:7.1f}s", flush=True)
    if args.record:
        args.record.write_text(json.dumps(
            {name: round(seconds, 1) for name, seconds in results},
            indent=2, sort_keys=True) + "\n")
    names = ", ".join(name for name, _, _ in sorted(failed))
    print(f"\n{len(modules)} modules, {total} tests, {len(failed)} failed, "
          f"{time.monotonic() - started:.1f}s"
          + (f"; failed: {names}" if failed else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

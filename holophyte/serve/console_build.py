from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

STAMP = "source-tree"
STEP_TIMEOUT_SEC = 300
TAIL_LINES = 20


def bun_build(outdir):
    return (("bun", "install", "--frozen-lockfile"),
            ("bun", "run", "build", str(outdir)))


def source_tree(sources):
    try:
        done = subprocess.run(["git", "rev-parse", "HEAD:./"], cwd=sources,
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def built_tree(dist):
    try:
        return (dist / STAMP).read_text().strip()
    except OSError:
        return None


def tail(text):
    return "\n".join((text or "").rstrip().splitlines()[-TAIL_LINES:])


def run_step(argv, cwd, timeout):
    step = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            errors="replace", start_new_session=True)
    try:
        output, _ = step.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(step.pid, signal.SIGKILL)
        output, _ = step.communicate()
        return f"`{' '.join(argv)}` timed out after {timeout}s", tail(output)
    if step.returncode != 0:
        return f"`{' '.join(argv)}` exited {step.returncode}", tail(output)
    return None


def build_failure(commands, sources, timeout):
    for argv in commands:
        try:
            failure = run_step(argv, sources, timeout)
        except OSError as bad:
            return f"`{' '.join(argv)}` did not start: {bad}", ""
        if failure is not None:
            return failure
    return None


def swap_in(staging, dist):
    staging.chmod(0o755)
    retired = Path(tempfile.mkdtemp(prefix=".dist-old-", dir=dist.parent))
    previous = retired / dist.name
    try:
        if dist.is_dir():
            dist.rename(previous)
        try:
            staging.rename(dist)
        except OSError:
            if previous.is_dir() and not dist.exists():
                previous.rename(dist)
            raise
    finally:
        shutil.rmtree(retired, ignore_errors=True)


def build_and_swap(sources, dist, commands, timeout):
    staging = Path(tempfile.mkdtemp(prefix=".dist-", dir=sources))
    try:
        failure = build_failure(commands(staging), sources, timeout)
        if failure is None:
            swap_in(staging, dist)
        return failure
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def refresh_console(out, dist, commands=bun_build, timeout=STEP_TIMEOUT_SEC):
    dist = Path(dist)
    sources = dist.parent
    if not (sources / "package.json").is_file():
        return False
    tree = source_tree(sources)
    built = built_tree(dist) if dist.is_dir() else None
    if dist.is_dir() and (tree is None or built == tree):
        return False
    was = "is not built" if not dist.is_dir() else \
        f"was built from {built or 'unstamped sources'}"
    print(f"[holo2] console {was}, its sources are {tree}: building",
          file=out, flush=True)
    try:
        failure = build_and_swap(sources, dist, commands, timeout)
    except OSError as bad:
        failure = f"staging the build failed: {bad}", ""
    if failure is not None:
        reason, output = failure
        kept = "the previous build" if dist.is_dir() else "no console"
        print(f"[holo2] console build failed, serving {kept}: {reason}",
              file=out, flush=True)
        if output:
            print(output, file=out, flush=True)
        return False
    print(f"[holo2] console rebuilt from {tree}", file=out, flush=True)
    return True

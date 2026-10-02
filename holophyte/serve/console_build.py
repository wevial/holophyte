from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

CONSOLE_SOURCES = Path(__file__).resolve().parents[2] / "console"
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
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    return "\n".join((text or "").rstrip().splitlines()[-TAIL_LINES:])


def build_failure(commands, sources, timeout):
    for argv in commands:
        try:
            done = subprocess.run(argv, cwd=sources, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  timeout=timeout)
        except OSError as bad:
            return f"`{' '.join(argv)}` did not start: {bad}", ""
        except subprocess.TimeoutExpired as slow:
            return (f"`{' '.join(argv)}` timed out after {timeout}s",
                    tail(slow.output))
        if done.returncode != 0:
            return (f"`{' '.join(argv)}` exited {done.returncode}",
                    tail(done.stdout))
    return None


def swap_in(staging, dist):
    staging.chmod(0o755)
    retired = Path(tempfile.mkdtemp(prefix=".dist-old-", dir=dist.parent))
    try:
        if dist.is_dir():
            dist.rename(retired / dist.name)
        staging.rename(dist)
    finally:
        shutil.rmtree(retired, ignore_errors=True)


def refresh_console(out, sources=CONSOLE_SOURCES, commands=bun_build,
                    timeout=STEP_TIMEOUT_SEC):
    sources = Path(sources)
    if not (sources / "package.json").is_file():
        return False
    dist = sources / "dist"
    tree = source_tree(sources)
    built = built_tree(dist) if dist.is_dir() else None
    if dist.is_dir() and (tree is None or built == tree):
        return False
    was = "is not built" if not dist.is_dir() else \
        f"was built from {built or 'unstamped sources'}"
    print(f"[holo2] console {was}, its sources are {tree}: building",
          file=out, flush=True)
    staging = Path(tempfile.mkdtemp(prefix=".dist-", dir=sources))
    try:
        failure = build_failure(commands(staging), sources, timeout)
        if failure is None:
            swap_in(staging, dist)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
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

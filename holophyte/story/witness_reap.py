"""A witness command's processes are stopped before its tree is removed."""
import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from holophyte.config.config_tables import story_config
from holophyte.host.supervisor_lock import pid_alive
from holophyte.redact import safe_print as print

STOP_GRACE = 30.0
POLL = 0.1
OWNER = "owner"
PROC = Path("/proc")
DELETED = " (deleted)"


def stop(proc, tree):
    if proc is not None:
        _stop_group(proc)
    stop_inside(tree)


def stop_inside(tree):
    pids = inside(tree)
    _stop_pids(pids)
    return len(pids)


def inside(tree):
    if not PROC.is_dir():
        return []
    roots = {str(tree), os.path.realpath(tree)}
    spared = _ancestors()
    return [pid for pid in _own_pids()
            if pid not in spared and _in_tree(pid, roots)]


def write_owner(scratch):
    (Path(scratch) / OWNER).write_text(f"{os.getpid()}\n")


def reap_stale(target):
    limit = story_config(target).witness_sec
    for scratch in sorted(Path(tempfile.gettempdir()).glob("witness-*")):
        if not _stale(scratch, limit):
            continue
        tree = scratch / "tree"
        stopped = stop_inside(tree)
        subprocess.run(["git", "worktree", "remove", "--force", "--force",
                        str(tree)], cwd=target.path, capture_output=True)
        shutil.rmtree(scratch, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=target.path,
                       capture_output=True)
        print(f"[holo2] reaped the leftover witness tree {scratch}:"
              f" {_processes(stopped)} stopped")


def stop_warm(path):
    stopped = stop_inside(path)
    if stopped:
        print(f"[holo2] the warm witness tree {path} had"
              f" {_processes(stopped)} left running: stopped")


def _processes(count):
    return f"{count} process" + ("" if count == 1 else "es")


def _stale(scratch, limit):
    try:
        status = scratch.lstat()
    except OSError:
        return False
    if not scratch.is_dir() or scratch.is_symlink() or status.st_uid != os.getuid():
        return False
    try:
        owner = int((scratch / OWNER).read_text().strip())
    except (OSError, ValueError):
        return time.time() - status.st_mtime > limit
    return not pid_alive(owner) or _stat(owner)[:1] == ["Z"]


def _stop_group(proc):
    if not _signal_group(proc.pid, signal.SIGTERM):
        return
    until = time.monotonic() + STOP_GRACE
    while time.monotonic() < until:
        proc.poll()
        if not _group_running(proc.pid):
            return
        time.sleep(POLL)
    _signal_group(proc.pid, signal.SIGKILL)
    proc.poll()


def _group_running(pgid):
    if not PROC.is_dir():
        return _signal_group(pgid, 0)
    return any(fields[0] != "Z" and fields[2] == str(pgid)
               for fields in map(_stat, _own_pids()) if fields)


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _stop_pids(pids):
    _signal_pids(pids, signal.SIGTERM)
    until = time.monotonic() + STOP_GRACE
    while pids and time.monotonic() < until:
        pids = [pid for pid in pids if _running(pid)]
        if pids:
            time.sleep(POLL)
    _signal_pids([pid for pid in pids if _running(pid)], signal.SIGKILL)


def _signal_pids(pids, signum):
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signum)


def _running(pid):
    fields = _stat(pid)
    return bool(fields) and fields[0] != "Z"


def _stat(pid):
    try:
        return (PROC / str(pid) / "stat").read_text().rpartition(")")[2].split()
    except OSError:
        return []


def _own_pids():
    uid = os.getuid()
    pids = []
    for entry in PROC.iterdir():
        try:
            if entry.name.isdigit() and entry.stat().st_uid == uid:
                pids.append(int(entry.name))
        except OSError:
            continue
    return pids


def _ancestors():
    spared, pid = set(), os.getpid()
    while pid > 0 and pid not in spared:
        spared.add(pid)
        try:
            pid = int(_stat(pid)[1])
        except (ValueError, IndexError):
            break
    return spared


def _in_tree(pid, roots):
    entry = PROC / str(pid)
    try:
        cwd = os.readlink(entry / "cwd").removesuffix(DELETED)
    except OSError:
        cwd = ""
    try:
        argv = (entry / "cmdline").read_bytes().decode(errors="replace")
    except OSError:
        argv = ""
    paths = [cwd, *argv.split("\0")]
    return any(_names(path, root) for path in paths for root in roots)


def _names(path, root):
    return path == root or path.endswith(root) or root + "/" in path

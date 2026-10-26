"""The host's health for the patrol: `/tmp`, leftover processes, the units
and the kernel log; each part `readable` or says why it was skipped."""
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from time import time

import store.read
from holophyte.host.sweep_report import finished_worktrees
from holophyte.loop.reexec import LOOP_UNIT, systemctl_user
from holophyte.story.witness_running import running_set

TMP_FULL_PERCENT = 80
PROC = Path("/proc")
ARGS_WIDTH = 100
DELETED = " (deleted)"
HOST_UNITS = ("holophyte-sweep.timer", "holophyte-serve.socket")
JUDGED_ACTIVE = "holophyte-sweep.timer"
KERNEL_ARGV = ["journalctl", "-k", "-p", "err", "--since", "-1h",
               "--no-pager", "-q"]
UNREAD_HINT = "not seeing messages"
JOURNAL_TIMEOUT = 20


def tmp_usage():
    path = tempfile.gettempdir()
    try:
        disk = os.statvfs(path)
    except OSError as bad:
        return {"readable": False, "detail": f"not readable ({bad})"}
    used = disk.f_blocks - disk.f_bfree
    total = used + disk.f_bavail
    percent = -(-used * 100 // total) if total else 0
    return {"readable": True, "path": path, "used_percent": percent,
            "free_bytes": disk.f_bavail * disk.f_frsize,
            "full": percent >= TMP_FULL_PERCENT}


def _is_under(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _top(path, root):
    """`root`'s child holding `path`, or None."""
    if not _is_under(path, root) or path == root:
        return None
    return os.path.join(root, path[len(root):].lstrip(os.sep).split(os.sep)[0])


def _project_trees(entry):
    """`(worktrees root, finished worktree paths)`."""
    target = entry.target
    finished = []
    if target.store_path.exists():
        try:
            conn = store.read.open_readonly(target.store_path)
            try:
                finished = [os.path.realpath(found[3])
                            for found in finished_worktrees(target, conn)]
            finally:
                conn.close()
        except sqlite3.Error:
            finished = []
    return os.path.realpath(target.worktrees), finished


class FinishedTrees:
    def __init__(self, host_projects, now):
        self.tmp = os.path.realpath(tempfile.gettempdir())
        found = [_project_trees(entry) for entry in host_projects
                 if entry.error is None]
        self.worktrees = [(root, finished) for root, finished in found]
        self.witness_live = any(running_set(entry.target, now) is not None
                                for entry in host_projects)

    def holds(self, path):
        witness = _top(path, self.tmp)
        if witness and os.path.basename(witness).startswith("witness-"):
            return not self.witness_live or not os.path.isdir(witness)
        for root, finished in self.worktrees:
            tree = _top(path, root)
            if tree is None:
                continue
            return not os.path.exists(path) or any(
                _is_under(path, done) for done in finished)
        return False


def _uptime():
    return float((PROC / "uptime").read_text().split()[0])


def _process(pid, uptime):
    base = PROC / str(pid)
    if base.stat().st_uid != os.getuid():
        return None
    cwd = os.readlink(base / "cwd").removesuffix(DELETED)
    argv = [arg for arg in (base / "cmdline").read_bytes().decode(
        errors="replace").split("\0") if arg]
    fields = (base / "stat").read_text().rpartition(")")[2].split()
    started = int(fields[19]) / os.sysconf("SC_CLK_TCK")
    return {"pid": pid, "age_sec": max(0, int(uptime - started)), "cwd": cwd,
            "args": " ".join(argv)[:ARGS_WIDTH], "argv": argv}


def leftovers(host_projects):
    if not PROC.is_dir():
        return {"readable": False, "detail": "skipped (no /proc)",
                "processes": []}
    trees = FinishedTrees(host_projects, int(time() * 1000))
    uptime, found = _uptime(), []
    for entry in PROC.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            process = _process(int(entry.name), uptime)
        except OSError:
            continue
        if process is None:
            continue
        argv = process.pop("argv")
        paths = [os.path.realpath(path) for path in (
            process["cwd"], *(arg.removesuffix(DELETED) for arg in argv
                              if os.path.isabs(arg)))]
        if any(trees.holds(path) for path in paths):
            found.append(process)
    found.sort(key=lambda process: process["pid"])
    return {"readable": True, "processes": found}


def units(names):
    if shutil.which("systemctl") is None:
        return {"readable": False,
                "detail": "skipped (systemctl is not on this host)",
                "units": [], "findings": []}
    states = []
    for unit in (*HOST_UNITS, *(LOOP_UNIT + name for name in names)):
        ok, detail = systemctl_user("is-active", unit)
        states.append({"unit": unit,
                       "state": "active" if ok else detail.splitlines()[0]})
    findings = [entry for entry in states if entry["state"] == "failed"
                or (entry["unit"] == JUDGED_ACTIVE
                    and entry["state"] != "active")]
    return {"readable": True, "units": states, "findings": findings}


def _kernel_error(line):
    return "I/O error" in line or ("nvme" in line and (
        "timeout" in line or "reset" in line))


def kernel_errors():
    try:
        done = subprocess.run(KERNEL_ARGV, capture_output=True, text=True,
                              timeout=JOURNAL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        done = None
    if done is None or done.returncode != 0 or UNREAD_HINT in done.stderr:
        return {"readable": False, "detail": "not readable", "errors": []}
    return {"readable": True,
            "errors": [line for line in done.stdout.splitlines()
                       if _kernel_error(line)]}


def host_health(host_projects, attention):
    health = {"attention": attention, "tmp": tmp_usage(),
              "leftovers": leftovers(host_projects),
              "units": units([entry.name for entry in host_projects
                              if entry.name is not None]),
              "kernel": kernel_errors()}
    health["clean"] = not (attention or health["tmp"].get("full")
                           or health["leftovers"]["processes"]
                           or health["units"]["findings"]
                           or health["kernel"]["errors"])
    return health

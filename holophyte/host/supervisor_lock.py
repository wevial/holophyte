import contextlib
import fcntl
import os
import socket
from pathlib import Path
from time import time

from holophyte.cli.report import host_label


# One supervisor per target: two would each strike a silence, faking the second.
def supervisor_lock_path(target):
    # Beside the store, not in the checkout, where `git add -A` could commit it.
    return target.holo_dir / "supervisor.lock"


class SupervisorHeld(Exception):
    def __init__(self, path, repo, pid=None, started_at=None, host=None,
                 target=None):
        self.path, self.repo, self.pid = path, repo, pid
        self.started_at, self.host = started_at, host
        if pid is None:
            what = (f"[holo2] supervisor lock {path} exists but names no"
                    " process; refusing to guess. remove it if no supervisor"
                    " is running")
        else:
            since = (f" since {started_at}" if started_at is not None else "")
            what = (f"[holo2] a supervisor is already running for {repo}:"
                    f" pid {pid} on {host_label(target, host)}{since}"
                    f" holds {path};"
                    " not starting another")
        super().__init__(what)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM is another user's process, still alive.
        return True
    return True


def read_supervisor_lock(path):
    try:
        fields = Path(path).read_text().split()
        if len(fields) == 2:
            host, pid, started_at = None, int(fields[0]), int(fields[1])
        else:
            host, pid, started_at = fields[0], int(fields[1]), int(fields[2])
    except (OSError, ValueError, IndexError):
        return None
    return pid, started_at, host


def supervisor_running(target):
    holder = read_supervisor_lock(supervisor_lock_path(target))
    if holder is None:
        return None
    pid = holder[0]
    return pid if pid_alive(pid) else None


@contextlib.contextmanager
def reclaim_turn(path):
    # Never unlinked, so every starter flocks the same inode.
    fd = os.open(f"{path}.reclaim", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def acquire_supervisor_lock(path, repo, pid=None, now=None, host=None,
                            target=None):
    path = Path(path)
    pid = os.getpid() if pid is None else pid
    now = int(time() * 1000) if now is None else now
    host = socket.gethostname() if host is None else host
    path.parent.mkdir(parents=True, exist_ok=True)

    # Create-then-check: a second starter slips through a check-then-create gap.
    def created():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{host} {pid} {now}\n")
        return True

    if created():
        return path
    with reclaim_turn(path):
        holder = read_supervisor_lock(path)
        # A lock naming no pid is ambiguous, and an ambiguous lock is never taken.
        if holder is None and path.exists():
            raise SupervisorHeld(path, repo, target=target)
        if holder is not None:
            # A dead holder crashed uncleaned; our own pid is a reused one, not a rival.
            if holder[0] != pid and pid_alive(holder[0]):
                raise SupervisorHeld(path, repo, *holder, target=target)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        # Written before the turn ends: a waiting starter never reads an empty lock.
        if created():
            return path
    holder = read_supervisor_lock(path)
    raise SupervisorHeld(path, repo, *(holder or ()), target=target)


def release_supervisor_lock(path, pid=None):
    pid = os.getpid() if pid is None else pid
    holder = read_supervisor_lock(path)
    # Ours only: one wrongly judged dead must not remove its reclaimer's lock.
    if holder is not None and holder[0] == pid:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

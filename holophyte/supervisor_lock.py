"""holophyte.supervisor_lock: one supervisor per target (KO-396).

Moved verbatim out of `holophyte/supervisor.py`: the lock's path
(`supervisor_lock_path()`), the `SupervisorHeld` refusal, the pid probe
(`pid_alive()`), the lock read (`read_supervisor_lock()`) and the
read-only liveness answer (`supervisor_running()`), then the write side --
`reclaim_turn()`'s sidecar flock, `acquire_supervisor_lock()`'s exclusive
create and `release_supervisor_lock()`'s checked unlink. The import runs
one way: `holophyte.supervisor`'s `supervise()` names the acquire and
release pair, and `holophyte.cli` the refusal and `supervisor_running()`.
"""
import contextlib
import fcntl
import os
import socket
from pathlib import Path
from time import time

from holophyte.report import host_label

# One per target, because two supervisors sweeping one store would each take
# their own sighting of every silence and manufacture between them the second
# strike the two-strike rule exists to demand of two separate silences. The
# arbitration is a lockfile beside the store, taken with an exclusive create
# (v1 TUI mining, server.ts:111-160): create-then-check, never check-then-
# create, because the gap between a check and a create is exactly where a
# second starter slips through. A lock that exists is then read: a live pid
# means a rival and this starter aborts naming it; a dead pid is a supervisor
# that crashed without cleaning up, and its lock is reclaimed; a lock that
# says neither -- empty, half-written, not ours to parse -- is ambiguous, and
# an ambiguous probe never spawns a rival. It aborts and says what it saw.


def supervisor_lock_path(target):
    """The lockfile for `target`'s supervisor, in its state directory.

    Beside the store rather than inside the target for the store's own
    reason: nothing about the target checkout should have to know the factory
    exists, and a lock inside it is dirt a task's `git add -A` could commit.
    Taken from the `Project`'s state directory so the lock cannot end up
    addressing a different directory from the store it guards.
    """
    return target.holo_dir / "supervisor.lock"


class SupervisorHeld(Exception):
    """The target already has a supervisor, or a lock this one will not take.

    `pid` is the live holder when there is one, and None when the lock could
    not be read -- the two cases the message tells apart, because the first
    is answered by doing nothing and the second by an operator looking at the
    file.
    """

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
    """Whether `pid` names a process that exists, by asking the kernel.

    Signal 0 delivers nothing and answers only whether it could have: a
    process that is gone is ESRCH, one that belongs to someone else is EPERM
    -- and EPERM is still alive, which is the answer that matters here.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_supervisor_lock(path):
    """The `(pid, started_at, host)` a lockfile names, or None if none.

    Written as `host pid started_at` on one line by
    `acquire_supervisor_lock()`. A lock an older supervisor wrote as the two
    integers alone still reads, with `host` None: it is a lock whose dead
    pid can be reclaimed, not a file somebody else wrote. Anything else --
    an empty file a crashed starter left between its create and its write,
    a file somebody else wrote -- is None, and the caller treats None as a
    lock it must not remove.
    """
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
    """The pid of the live supervisor holding `target`'s lock, or None.

    The loop asks this at startup to decide whether to start one: a lock
    naming a pid the kernel still knows is a watcher already on the job, and
    anything else -- no lock, a dead pid, a file that names no pid -- is
    None. The read only; the spawned `--supervise` takes the lock itself
    through `acquire_supervisor_lock()`, so a dead or unreadable lock is
    judged there, with its reclaim turn, not here.
    """
    holder = read_supervisor_lock(supervisor_lock_path(target))
    if holder is None:
        return None
    pid = holder[0]
    return pid if pid_alive(pid) else None


@contextlib.contextmanager
def reclaim_turn(path):
    """Hold the reclaim sidecar of the lock at `path` for the block's span.

    A blocking flock on `<path>.reclaim`, which is never unlinked so every
    starter locks the same inode. It orders reclaims only; the lock itself
    stays the exclusive create, so a starter that never has to reclaim never
    touches the sidecar.
    """
    fd = os.open(f"{path}.reclaim", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def acquire_supervisor_lock(path, repo, pid=None, now=None, host=None,
                            target=None):
    """Take the supervisor lock at `path` for `pid`; raise `SupervisorHeld`.

    `repo` is the repository the lock guards, named in the refusal so the
    operator reading it knows which target already has its supervisor.

    The lock's content is `host pid started_at`, `host` defaulting to this
    machine's hostname: a target's state directory can be read from another
    machine, and a lock naming only a pid then names a process nobody there
    can find.

    The exclusive create is the arbitration: two starters racing here both
    reach the kernel, and the kernel lets one of them through. The loser
    reads the lock the winner wrote and finds a live pid. A lock left by a
    dead supervisor is reclaimed -- unlinked, then created again through the
    same exclusive door, so a reclaim that loses a race to another starter
    loses the way any second starter does. The reclaim itself -- read the
    holder, judge it dead, unlink -- runs under an flock on a sidecar beside
    the lock, so two starters that both read the same dead pid take turns:
    the second finds, once its turn comes, the live lock the first has just
    written, and is refused by it. (An inode compared before the unlink is
    not that guard: between the comparison and the unlink a rival can have
    reclaimed and re-created the file, and the unlink then takes the rival's
    live lock.) A lock naming this very pid is the stale case too: a
    supervisor is not its own rival, and a pid comes round again. Only one
    reclaim is attempted, because a create that fails after it is a starter
    that never needed a turn, not a second stale lock.
    """
    path = Path(path)
    pid = os.getpid() if pid is None else pid
    now = int(time() * 1000) if now is None else now
    host = socket.gethostname() if host is None else host
    # The target's state directory, on first need: a supervisor can be the
    # first thing to run against a target.
    path.parent.mkdir(parents=True, exist_ok=True)

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
        if holder is None and path.exists():
            raise SupervisorHeld(path, repo, target=target)
        if holder is not None:
            if holder[0] != pid and pid_alive(holder[0]):
                raise SupervisorHeld(path, repo, *holder, target=target)
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        # Created (and written) before the turn is given up, so the starter
        # waiting for it reads a whole lock, never the empty file between a
        # create and its write.
        if created():
            return path
    holder = read_supervisor_lock(path)
    raise SupervisorHeld(path, repo, *(holder or ()), target=target)


def release_supervisor_lock(path, pid=None):
    """Remove the lock at `path` if it is `pid`'s; leave anyone else's alone.

    Checked before it is removed because the lock may not be ours any more: a
    supervisor that was wrongly judged dead has had its lock reclaimed, and
    removing the reclaimer's lock on the way out would let a third starter
    in beside it.
    """
    pid = os.getpid() if pid is None else pid
    holder = read_supervisor_lock(path)
    if holder is not None and holder[0] == pid:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

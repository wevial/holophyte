"""The serve daemon following the factory code (KO-648).

`serve()` runs one `CodeWatch` between requests: it reads the revision the
factory checkout had at startup and, every `CODE_CHECK_SEC`, the one it has
now, and raises `Moved` out of `serve_forever()` once they differ -- the
supervisor's code-moved check, read through the same `factory_revision()`,
which `serve()` hands in so the daemon's tests patch it where it is used.
`InFlight` counts the requests the daemon has read and not yet answered,
so the re-exec can wait for them: the daemon's handler threads are
daemons, which `server_close()` is not promised to join -- for at most
`DRAIN_SEC`, under the service manager's 30 s stop timeout.

`adopted_socket()` is the other half of socket activation (consolidation
stage 1): under `LISTEN_FDS=1` with a `LISTEN_PID` equal to this process's
pid, the listening socket the service manager holds is fd 3, and the
daemon serves on it instead of binding. Such a daemon exits on a code move
rather than re-executing: the manager keeps the socket, the kernel queues
what arrives meanwhile, and the next connection starts the new code.
Standard library only.
"""
from __future__ import annotations

import os
import socket
import threading
from time import monotonic

# How often, in seconds, the daemon asks whether the factory checkout it
# runs from has moved to a new commit.
CODE_CHECK_SEC = 15
# How long, in seconds, a daemon leaving for new code waits for the
# requests it is answering: under the unit's `TimeoutStopSec=30`.
DRAIN_SEC = 20
# `SD_LISTEN_FDS_START`: the first descriptor a service manager hands over.
LISTEN_FD = 3
LISTEN_KEYS = ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES")


def adopted_socket(environ=None):
    """The listening socket the service manager handed this process, or
    None when it handed none.

    Adopted only under `LISTEN_FDS` with a `LISTEN_PID` naming this very
    process: a variable inherited from a parent names the parent's socket,
    not one this process may take. The variables are unset once read, as
    `sd_listen_fds(1)` does, so nothing this daemon spawns inherits them.
    More than one descriptor is refused: `--serve` answers on one address.
    """
    environ = os.environ if environ is None else environ
    try:
        count = int(environ.get("LISTEN_FDS", ""))
        pid = int(environ.get("LISTEN_PID", ""))
    except ValueError:
        return None
    if pid != os.getpid():
        return None
    for key in LISTEN_KEYS:
        environ.pop(key, None)
    if count != 1:
        raise SystemExit(f"[holo2] the service manager handed over {count}"
                         " sockets; --serve answers on exactly one")
    return socket.socket(fileno=LISTEN_FD)


class Moved(Exception):
    """Raised inside `serve_forever()` by `CodeWatch` to unwind it."""


class CodeWatch:
    """The code-moved check, called between requests: every `interval`
    seconds it calls `read()` and raises `Moved` once that differs from
    what it read at construction. A revision `read()` cannot give (None),
    at startup or later, is printed once and never a move."""

    def __init__(self, interval, out, read, clock=monotonic):
        self.interval = interval
        self.out = out
        self.read = read
        self.clock = clock
        self.due = clock() + interval
        self.warned = False
        self.started_from = read()
        self.moved_to = None
        if self.started_from is None:
            self.warn()

    def check_now(self):
        """Make the next call read the revision whatever the interval says:
        a store stamped newer than this build is a hint the code moved."""
        self.due = self.clock()

    def __call__(self):
        if self.started_from is None or self.clock() < self.due:
            return
        current = self.read()
        self.due = self.clock() + self.interval
        if current is None:
            self.warn()
        elif current != self.started_from:
            self.moved_to = current
            raise Moved(current)

    def warn(self):
        if not self.warned:
            self.warned = True
            print("[holo2] serve cannot read the factory checkout's HEAD;"
                  " serving on without following the code", file=self.out,
                  flush=True)


class InFlight:
    """A server mix-in counting requests, not connections: the handler
    calls `begin()` once a request's headers are in and `done()` once it
    is answered, so `drain()` waits for the requests being answered and
    never for a client that connected and sent nothing -- the exec closes
    that socket with the rest. From `drain()` on, `begin()` refuses, so no
    request starts that the exec would cut off."""

    def __init__(self, *args, **kwargs):
        self.in_flight = 0
        self.draining = False
        self.settled = threading.Condition()
        super().__init__(*args, **kwargs)

    def begin(self):
        with self.settled:
            if self.draining:
                return False
            self.in_flight += 1
            return True

    def done(self):
        with self.settled:
            self.in_flight -= 1
            self.settled.notify_all()

    def drain(self, timeout=None):
        """Refuse new requests and wait up to `timeout` seconds (None: for
        ever) for those being answered; whether they all were."""
        with self.settled:
            self.draining = True
            return self.settled.wait_for(lambda: self.in_flight == 0, timeout)

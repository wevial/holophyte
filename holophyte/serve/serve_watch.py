from __future__ import annotations

import os
import socket
import threading
from time import monotonic

CODE_CHECK_SEC = 15
# Under the service unit's `TimeoutStopSec=30`.
DRAIN_SEC = 20
# `SD_LISTEN_FDS_START`.
LISTEN_FD = 3
LISTEN_KEYS = ("LISTEN_FDS", "LISTEN_PID", "LISTEN_FDNAMES")


def adopted_socket(environ=None):
    environ = os.environ if environ is None else environ
    try:
        count = int(environ.get("LISTEN_FDS", ""))
        pid = int(environ.get("LISTEN_PID", ""))
    except ValueError:
        return None
    # Inherited from a parent: the socket is the parent's, not ours.
    if pid != os.getpid():
        return None
    # Unset once read, as `sd_listen_fds(1)` does, so no child inherits them.
    for key in LISTEN_KEYS:
        environ.pop(key, None)
    if count != 1:
        raise SystemExit(f"[holo2] the service manager handed over {count}"
                         " sockets; --serve answers on exactly one")
    return socket.socket(fileno=LISTEN_FD)


class Moved(Exception):
    pass


class CodeWatch:
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
    """Handler threads are daemons, which `server_close()` need not join."""

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
        with self.settled:
            self.draining = True
            return self.settled.wait_for(lambda: self.in_flight == 0, timeout)

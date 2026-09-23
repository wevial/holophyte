"""The serve daemon following the factory code (KO-648).

`serve()` runs one `CodeWatch` between requests: it reads the revision the
factory checkout had at startup and, every `CODE_CHECK_SEC`, the one it has
now, and raises `Moved` out of `serve_forever()` once they differ -- the
supervisor's code-moved check, read through the same `factory_revision()`,
which `serve()` hands in so the daemon's tests patch it where it is used.
`InFlight` counts the requests the daemon has read and not yet answered,
so the re-exec can wait for them: the daemon's handler threads are
daemons, which `server_close()` is not promised to join.
Standard library only.
"""
from __future__ import annotations

import threading
from time import monotonic

# How often, in seconds, the daemon asks whether the factory checkout it
# runs from has moved to a new commit.
CODE_CHECK_SEC = 15


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

    def drain(self):
        with self.settled:
            self.draining = True
            self.settled.wait_for(lambda: self.in_flight == 0)

"""holophyte.deadline: the bound the host sweep puts on its network calls.

A host sweep run (`holophyte.sweep_host`) reconciles every project under
one deadline, a share of it per project, and no timeout can cut a Linear
read already in flight. So the run asks before each unit of network work
instead: `check()` stands before a trip is acted on, before each ticket's
pull request read, before each board ask and before a loop's route is
probed, and raises `DeadlineReached` once the bound `bounded()` set has
passed or a stop was asked for. Outside `bounded()` it does nothing, so the
loop and the project-form supervisor, which share those call sites, are
never cut.

`DeadlineReached` derives from `BaseException` so the `except Exception`
that turns a transport failure into one printed line does not swallow it.
Every `check()` sits where no transaction is open and the unit before it is
whole, so a cut leaves nothing half written. Standard library only.
"""
import contextlib
import time
from contextvars import ContextVar

_BOUND = ContextVar("sweep_deadline", default=None)


class DeadlineReached(BaseException):
    """The bound `bounded()` set has passed, or a stop was asked for; the
    message names the call that was not made."""


@contextlib.contextmanager
def bounded(end, stop=None):
    """Bound the block's `check()` calls by `end`, a `time.monotonic()`
    instant, and by `stop`, a `threading.Event` a signal handler sets."""
    token = _BOUND.set((end, stop))
    try:
        yield
    finally:
        _BOUND.reset(token)


def check(what):
    """Raise `DeadlineReached` naming `what` when the bound is spent."""
    bound = _BOUND.get()
    if bound is None:
        return
    end, stop = bound
    if stop is not None and stop.is_set():
        raise DeadlineReached(f"stop requested before {what}")
    if time.monotonic() >= end:
        raise DeadlineReached(f"share spent before {what}")

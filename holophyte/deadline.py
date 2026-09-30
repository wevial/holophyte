"""The host sweep's bound on its network calls; outside `bounded()` it does nothing."""
import contextlib
import time
from contextvars import ContextVar

_BOUND = ContextVar("sweep_deadline", default=None)


class DeadlineReached(BaseException):
    """A BaseException, so a transport's `except Exception` does not swallow it."""


class CallRefused(Exception):
    """An ordinary Exception: its call site treats it as the service being down."""


class Bound:
    def __init__(self, end, stop):
        self.end, self.stop, self.refused = end, stop, []

    def spent(self, what):
        if self.stop is not None and self.stop.is_set():
            return f"stop requested before {what}"
        if time.monotonic() >= self.end:
            return f"share spent before {what}"
        return None


@contextlib.contextmanager
def bounded(end, stop=None):
    """`end` is a `time.monotonic()` instant; `stop` an Event a signal handler sets."""
    bound = Bound(end, stop)
    token = _BOUND.set(bound)
    try:
        yield bound
    finally:
        _BOUND.reset(token)


def check(what):
    bound = _BOUND.get()
    reason = bound.spent(what) if bound is not None else None
    if reason is not None:
        raise DeadlineReached(reason)


def spent():
    bound = _BOUND.get()
    return bound is not None and bound.spent("") is not None


def admit(what):
    bound = _BOUND.get()
    reason = bound.spent(what) if bound is not None else None
    if reason is not None:
        bound.refused.append(reason)
        raise CallRefused(reason)

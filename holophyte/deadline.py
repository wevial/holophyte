"""holophyte.deadline: the bound the host sweep puts on its network calls.

A host sweep run (`holophyte.sweep_host`) reconciles every project under
one deadline, a share of it per project, and no timeout can cut a Linear
read already in flight. So the run asks before each network call instead,
in two ways that differ in what a cut leaves behind.

`check()` stands at the start of each unit of network work: before a trip
is acted on, before each ticket's pull request read, before each board ask
and before a loop's route is probed. It raises `DeadlineReached` once the
bound `bounded()` set has passed or a stop was asked for. It sits where no
transaction is open and the unit before it is whole, so the cut leaves
nothing half written. `DeadlineReached` derives from `BaseException` so the
`except Exception` that turns a transport failure into one printed line
does not swallow it.

`guarded()` wraps the board provider a unit calls once it has begun: a
close-out or a merge landed off a slow read writes the store first and then
tells Linear in several calls. Past the bound each of those calls is
refused with `CallRefused`, an ordinary `Exception`, so every call site
does what it does when Linear is down -- a `warning` row, a stale board,
the store right -- and the unit's remaining store writes still land. The
bound records each refusal, and the sweep treats a project that had one
as cut.

Outside `bounded()` both do nothing, so the loop and the project-form
supervisor, which share those call sites, are never cut. Standard library
only.
"""
import contextlib
import time
from contextvars import ContextVar

_BOUND = ContextVar("sweep_deadline", default=None)


class DeadlineReached(BaseException):
    """The bound `bounded()` set has passed, or a stop was asked for; the
    message names the call that was not made."""


class CallRefused(Exception):
    """A board call `guarded()` did not make because the bound had passed:
    to its call site, a call that failed."""


class Bound:
    """One `bounded()` block: its end, its stop event and why each board
    call it refused was refused."""

    def __init__(self, end, stop):
        self.end, self.stop, self.refused = end, stop, []

    def spent(self, what):
        """Why `what` may not be made now, or None."""
        if self.stop is not None and self.stop.is_set():
            return f"stop requested before {what}"
        if time.monotonic() >= self.end:
            return f"share spent before {what}"
        return None


@contextlib.contextmanager
def bounded(end, stop=None):
    """Bound the block's `check()` calls and guarded board calls by `end`,
    a `time.monotonic()` instant, and by `stop`, a `threading.Event` a
    signal handler sets. Yields the `Bound`."""
    bound = Bound(end, stop)
    token = _BOUND.set(bound)
    try:
        yield bound
    finally:
        _BOUND.reset(token)


def check(what):
    """Raise `DeadlineReached` naming `what` when the bound is spent."""
    bound = _BOUND.get()
    reason = bound.spent(what) if bound is not None else None
    if reason is not None:
        raise DeadlineReached(reason)


def spent():
    """Whether a bound is set and spent now."""
    bound = _BOUND.get()
    return bound is not None and bound.spent("") is not None


def guarded(provider):
    """`provider` with every method call refused once the bound is spent;
    None stays None."""
    return None if provider is None else _Guarded(provider)


class _Guarded:
    """A board provider whose calls ask the bound first."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        value = getattr(self._inner, name)
        if not callable(value):
            return value

        def call(*args, **kwargs):
            bound = _BOUND.get()
            reason = (bound.spent(f"the board's {name}")
                      if bound is not None else None)
            if reason is not None:
                bound.refused.append(reason)
                raise CallRefused(reason)
            return value(*args, **kwargs)
        return call

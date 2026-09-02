"""Bounded polling for the tests that time a real subprocess tree.

A helper, not a test module: discovery never imports it. The test files put
`tests/` on `sys.path` themselves, the way they do for `fake_agent`.
"""
import time


def wait_for(condition, timeout, interval=0.05):
    """Poll `condition()` until it is true or `timeout` seconds have passed.

    Returns the last value of `condition()`: a caller waiting for a file to
    appear asserts the result true, and one proving a file never appears lets
    the whole window elapse and asserts it false. The window is a wall-clock
    deadline, not an iteration count, so a loaded machine that starves the
    poller still gets the full window -- which is the reason the callers poll
    instead of sleeping a fixed time and looking once.
    """
    deadline = time.monotonic() + timeout
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)

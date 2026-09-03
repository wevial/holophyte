"""Failure-time diagnostics for the tests that time a real process tree.

A helper, not a test module, like `waiting`: discovery never imports it. The
two escaped-child tests (`test_the_cap_still_reaps_the_command_s_process_group`
in `test_verify_gate.py`, `test_the_cap_takes_the_command_s_children_down_with_it`
in `test_factory_config.py`) have each failed once under heavy concurrent load
and passed on every rerun, and a bare "a child outlived the cap" cannot say
why. When the escaped marker appears these helpers describe the moment: how
late the kill ran, which branch `reap_group` took, what the surviving process
tree looked like, and which shell `/bin/sh` is on this host. The one cost on
a passing run is a single `ps` inside the watched `killpg` call, taken then
because that is the only moment the tree is certain to be alive: once the
capped call returns, the shell's pipe-holding `sleep` has been reaped and the
child that wrote the marker has usually exited with it.
"""
import errno
import os
import subprocess
import time
from unittest.mock import patch

from waiting import wait_for

PS_COLUMNS = "pid,pgid,sess,stat,command"


class KillWatch:
    """Record the `os.killpg` call `reap_group` makes, without changing it.

    Use as a context manager around the capped run. Each call is recorded
    with the monotonic time since the watch began (the launch, near enough),
    whether `marker` already existed when the kill ran, the `ps` lines that
    mention `marker` at that instant (pid, pgid, session, state, command),
    and whether the real call returned or raised (and with which errno). The
    real call always runs; a raise is re-raised so `reap_group` takes its
    `proc.kill()` fallback exactly as it would unwatched.

    Read the record against the two hypotheses for an escaped child: a late
    call that succeeded with the marker already present is scheduler delay
    between `communicate()` raising and `killpg` running; a timely `ESRCH` or
    `EPERM` followed by the marker means the fallback ended only the shell.
    """

    def __init__(self, marker):
        self.marker = marker
        self.calls = []
        self.started = None
        self._real = os.killpg
        self._patch = patch.object(os, "killpg", self._record)

    def __enter__(self):
        self.started = time.monotonic()
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()
        return False

    def _record(self, pgid, sig):
        call = {"at": time.monotonic() - self.started,
                "pgid": pgid, "sig": sig,
                "marker_present": self.marker.exists(),
                "snapshot": process_snapshot(self.marker)}
        self.calls.append(call)
        try:
            self._real(pgid, sig)
        except OSError as exc:
            call["error"] = "%s (errno %s)" % (
                errno.errorcode.get(exc.errno, "?"), exc.errno)
            raise
        call["error"] = None

    def describe(self):
        if not self.calls:
            return "killpg: never called"
        lines = []
        for call in self.calls:
            lines.append(
                "killpg(%s, %s) at +%.3fs: %s; escaped marker %s at the call"
                % (call["pgid"], call["sig"], call["at"],
                   "returned" if call["error"] is None else
                   "raised " + call["error"],
                   "already present" if call["marker_present"] else "absent"))
            lines.append("processes mentioning the marker at that call (ps -o %s):"
                         "\n%s" % (PS_COLUMNS, call["snapshot"]))
        return "\n".join(lines)


def process_snapshot(marker):
    """`ps` lines whose command mentions `marker`, or a note that it failed."""
    try:
        ps = subprocess.run(["ps", "-A", "-o", PS_COLUMNS], capture_output=True,
                            text=True, timeout=5)
        lines = ps.stdout.splitlines()
        header = lines[:1]
        hits = [line for line in lines[1:] if str(marker) in line]
        if ps.returncode != 0:
            raise RuntimeError("ps exited %d: %s" % (ps.returncode, ps.stderr))
        if not hits:
            return "no live process mentions %s" % marker
        return "\n".join(header + hits)
    except Exception as exc:  # the snapshot must never mask the real failure
        return "process snapshot could not be taken: %r" % (exc,)


def resolved_shell():
    try:
        return os.path.realpath("/bin/sh")
    except OSError as exc:
        return "could not resolve /bin/sh: %r" % (exc,)


def assert_no_escaped_child(marker, window, watch=None, elapsed=None, cap=None):
    """Fail, with a snapshot, if `marker` appears within `window` seconds.

    `elapsed` is how long the capped call took end to end and `cap` its
    timeout, so the message can give the kill latency as `elapsed - cap`;
    `watch` is the `KillWatch` that saw the kill and holds the snapshot from
    the moment of it, the one that can still name the escaped process with
    its group and session. The snapshot taken here, when the marker appears,
    usually finds the writer already gone and says so. All three are
    optional and reported only when given. Passing costs the polling window
    alone.
    """
    if not wait_for(marker.exists, window):
        return
    parts = ["a child of the timed-out command outlived the cap"]
    if elapsed is not None and cap is not None:
        parts.append("kill latency: the capped call took %.3fs against a %.3fs "
                     "cap, so %.3fs went to reaping" % (elapsed, cap, elapsed - cap))
    parts.append(watch.describe() if watch is not None else "killpg: not watched")
    parts.append("processes mentioning the marker when it appeared (ps -o %s):\n%s"
                 % (PS_COLUMNS, process_snapshot(marker)))
    parts.append("/bin/sh resolves to %s" % resolved_shell())
    raise AssertionError("\n".join(parts))

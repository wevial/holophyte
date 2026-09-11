"""The self re-exec: how a factory process replaces itself with a fresh one.

Two processes restart themselves on the factory's own code moving under
them: the loop after merging a change to the factory itself, and the
supervisor when the checkout it was started from is no longer the one on
disk. Both replace the process image with the command line they were
launched with -- never a module reloaded -- and both do it through a seam a
test can patch, so `reexec_self()` takes the caller's `EXEC` rather than
owning one: the loop's tests patch `holophyte.loop.EXEC`, the supervisor's
`holophyte.supervisor.EXEC`, and neither ever execs the test runner.

The other way a factory process is started: `start_loop()` asks the user
service manager for the target's `holophyte-loop@` unit, the one call the
daemon's `launch-loop` action and the supervisor's sweep share (KO-376), so
a loop the supervisor starts for work its sweep made is started exactly as
the operator's console click starts one. Standard library only.
"""
import os
import shutil
import subprocess
import sys

# The deploy unit templates `systemctl --user` addresses, each with the
# `[serve] name` instance appended; `systemctl` gets `SYSTEMCTL_TIMEOUT`
# seconds to answer.
LOOP_UNIT = "holophyte-loop@"
SUPERVISOR_UNIT = "holophyte-supervise@"
SYSTEMCTL_TIMEOUT = 20


def reexec_command():
    """The `(program, argv)` that restarts this process as it was launched.

    `sys.orig_argv` is the exact original command line, so interpreter flags
    (-u above all: without it a tee'd log goes block-buffered and looks hung)
    survive the restart; without it the interpreter and `sys.argv` stand in.
    `os.execv` does not search PATH and `orig_argv[0]` is whatever the
    operator typed -- usually the bare `python3` -- so the program is
    resolved the way the shell did; a name PATH cannot find falls back to
    the interpreter actually running this code.
    """
    argv = list(sys.orig_argv) or [sys.executable, *sys.argv]
    program = argv[0]
    if os.sep not in program:
        program = shutil.which(program) or sys.executable
    return program, argv


def reexec_self(reason, exec_, out=None):
    """Print `[holo2] <reason>: <argv>` and replace the process through
    `exec_`. Returns only when a test's `exec_` does.

    flush=True: execv replaces the process image without running Python's
    buffered-stdout flush, so under a redirected (block-buffered) stdout the
    line would be lost. Whatever the caller must write before the exec -- a
    store row, a released lock -- it writes before calling, because nothing
    can be written after a failed one.
    """
    program, argv = reexec_command()
    print(f"[holo2] {reason}: {argv}", file=out or sys.stdout, flush=True)
    exec_(program, argv)


def systemctl_user(verb, unit):
    """`systemctl --user VERB UNIT`: `(ok, detail)`, where `detail` says
    what happened in one line either way.

    Never raises for what `systemctl` does: exiting non-zero is `ok`
    False with its stderr (or stdout, or the exit status) as the detail,
    being absent from this host or outliving `SYSTEMCTL_TIMEOUT` is the
    same shape saying which. A caller that asked for a thing is told what
    happened and decides what that means for it -- the daemon answers the
    request with it, the supervisor prints it and sweeps on.
    """
    argv = ["systemctl", "--user", verb, unit]
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT)
    except FileNotFoundError:
        return False, "systemctl is not on this host"
    except subprocess.TimeoutExpired:
        return False, f"systemctl did not answer within {SYSTEMCTL_TIMEOUT}s"
    if done.returncode == 0:
        return True, f"{' '.join(argv)} exited 0"
    return False, ((done.stderr or done.stdout or "").strip()
                   or f"{' '.join(argv)} exited {done.returncode}")


def start_loop(unit_name):
    """Start the loop unit of the target whose `[serve] name` is
    `unit_name`: `(unit, ok, detail)` from `systemctl_user()`."""
    unit = LOOP_UNIT + unit_name
    ok, detail = systemctl_user("start", unit)
    return unit, ok, detail

"""The self re-exec: how a factory process replaces itself with a fresh one.

Two processes restart themselves on the factory's own code moving under
them: the loop after merging a change to the factory itself, and the
supervisor when the checkout it was started from is no longer the one on
disk. Both replace the process image with the command line they were
launched with -- never a module reloaded -- and both do it through a seam a
test can patch, so `reexec_self()` takes the caller's `EXEC` rather than
owning one: the loop's tests patch `holophyte.loop.EXEC`, the supervisor's
`holophyte.supervisor.EXEC`, and neither ever execs the test runner.
Standard library only.
"""
import os
import shutil
import sys


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

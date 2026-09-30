import os
import shutil
import subprocess
import sys

LOOP_UNIT = "holophyte-loop@"
SUPERVISOR_UNIT = "holophyte-supervise@"
SWEEP_UNIT = "holophyte-sweep.service"
SYSTEMCTL_TIMEOUT = 20


def reexec_command():
    """`orig_argv` keeps interpreter flags (`-u`); `execv` does not search PATH."""
    argv = list(sys.orig_argv) or [sys.executable, *sys.argv]
    program = argv[0]
    if os.sep not in program:
        program = shutil.which(program) or sys.executable
    return program, argv


def reexec_self(reason, exec_, out=None):
    program, argv = reexec_command()
    # flush: execv replaces the process image without flushing stdout.
    print(f"[holo2] {reason}: {argv}", file=out or sys.stdout, flush=True)
    exec_(program, argv)


def systemctl_user(verb, unit, *options):
    """`(ok, detail)`; never raises for what `systemctl` does."""
    argv = ["systemctl", "--user", verb, *options, unit]
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
    unit = LOOP_UNIT + unit_name
    ok, detail = systemctl_user("start", unit)
    return unit, ok, detail

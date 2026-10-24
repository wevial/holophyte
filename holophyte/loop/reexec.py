import os
import re
import shutil
import subprocess
import sys

LOOP_UNIT = "holophyte-loop@"
SUPERVISOR_UNIT = "holophyte-supervise@"
SWEEP_UNIT = "holophyte-sweep.service"
SYSTEMCTL_TIMEOUT = 20
SYSTEMD_UNIT_VARS = ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID",
                     "NOTIFY_SOCKET", "MANAGERPID")


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


def systemd_runner():
    return os.environ.get("INVOCATION_ID") and shutil.which("systemd-run")


def _escape(match):
    return "".join(f"_{byte:02x}" for byte in match.group().encode())


def transient_unit(kind, name, ident):
    return f"holophyte-{kind}-{re.sub(r'[^A-Za-z0-9.]', _escape, name)}-{ident}"


def start_transient_unit(runner, unit, log, argv):
    ran = subprocess.run(
        [runner, "--user", "--collect", "--quiet", "--unit", unit,
         f"--working-directory={os.getcwd()}",
         f"--property=StandardOutput=append:{log}",
         f"--property=StandardError=append:{log}",
         *(f"--setenv={key}" for key in os.environ if key not in SYSTEMD_UNIT_VARS),
         *argv],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if ran.returncode != 0:
        raise OSError(f"systemd-run exited {ran.returncode}:"
                      f" {ran.stderr.strip()}")

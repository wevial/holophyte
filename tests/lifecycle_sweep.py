"""The sweep command of `tests/test_deploy_lifecycle.py`'s renamed units.

It is `factory.py --supervise --once` unchanged, the host sweep of the
checkout the unit names as its `WorkingDirectory`, with two additions the
lifecycle test reads, both in `HOLOPHYTE_LIFECYCLE_DIR`:

- every start appends `PID EPOCH` to `starts`, before anything else, so the
  test counts the runs systemd started, refused ones included;
- right after the run has taken the home lock and written `started` to
  `sweep.json`, it waits while `hold` exists: a run stuck as a slow network
  call would be, still holding the lock, and deaf to SIGTERM as a call in
  flight is.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.getcwd())

import holophyte.sweep_host as sweep_host  # noqa: E402
from holophyte.cli import cli  # noqa: E402

SCRATCH = Path(os.environ["HOLOPHYTE_LIFECYCLE_DIR"])
HOLD = SCRATCH / "hold"


def held_begin(*args, **kwargs):
    begin(*args, **kwargs)
    while HOLD.exists():
        time.sleep(0.2)


if __name__ == "__main__":
    with open(SCRATCH / "starts", "a") as out:
        out.write(f"{os.getpid()} {time.time()}\n")
    begin = sweep_host._begin
    sweep_host._begin = held_begin
    sys.exit(cli(["--supervise", "--once"]))

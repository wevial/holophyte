"""The Holophyte factory as a package.

`factory.py` is the entry point: it imports `cli` from here and calls it.
The code moved here one section at a time (phase 2 of the plan): `target`
(where a target's state lives, the `Target` value), `config` (`config.toml`
and every table it can set), `gates` (the verify gate and the failure
classes), `agents` (agent routes and the `agent()` call), `review` (parsing
a review's findings and verdict), `findings` (rendering `FINDINGS.md`),
`report` (the estimate-vs-actual table), `runs` (the store seam: opening it,
phases and rounds), `board` (the Linear mirror and escalation),
`supervisor` (the stale-run sweep, its lock and its loop), `loop` (worktree
setup and reuse, `run_task`, `main`, `report`, the re-exec) and `cli` (the
argument parser and mode dispatch).
"""

import sys as _sys

# The factory reports through `print`: operator log lines on stdout, with the
# supervisor already re-exec'd under `python3 -u` so a log file sees them as
# they happen. Under any other pipe stdout is block-buffered and drains at
# exit, *after* whatever stderr said last — which is also how the tickets'
# frozen verify pipeline (`... 2>&1 | tail -1 | grep -q '^OK'`) came to read
# a test's leaked line where it expected unittest's `OK`. Line-buffering keeps
# the two streams in order wherever the package is imported; a TTY already is.
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(line_buffering=True)
del _sys

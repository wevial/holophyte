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

# Every line the factory says is a `[holo2]` progress print on stdout; the
# verdicts a ticket's verify line greps for arrive on stderr. When stdout is
# a pipe -- an operator's `tee`, a service journal, or a verify line's
# `2>&1 | tail -1` -- Python block-buffers it, so progress lands late and the
# suite's `OK` is followed by the flush. Line-buffer it so each line lands
# when it is said, in order with stderr.
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(line_buffering=True)
del _sys

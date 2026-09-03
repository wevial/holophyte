#!/usr/bin/env python3
"""holo2: a minimal software factory.

Loop:
  0. Open the v2 store (WAL-mode SQLite, a sibling file of the target repo).
  1. Claim the first ready ticket from the board (a `provider.Provider`,
     Linear by default), mirror it into the store and take the project's
     run lease before any branch exists; the lease goes back when the run
     ends, merged or not.
  2. Spawn an implementer agent (goal-based) on a branch.
  3. Spawn a read-only reviewer agent on the committed result.
  4. If findings: implementer fixes, one narrow re-review, and a fix round for
     its findings too. Max 2 review rounds.
  5. If neither round approved: one terminal adjudication run — PASS or FAIL on
     the final state, no new findings, no further fixes.
  6. On approval or a terminal PASS: merge to main, check off the task, repeat.

`--report` runs none of the above: it prints the store's estimate-vs-actual
table for the target and exits, so the timing the runs recorded is a query
rather than a grep over FINDINGS.md. `--requeue KO-n --note TEXT` puts a
ticket whose run failed back in the queue, recording the intervention that
says why, and exits. `--sweep` runs none of it either: it
reads the live runs and says which have tripped a mechanical condition -- a
dead heartbeat or a blown time box -- without touching one. `--sweep --act`
adds the acting: each tripped run is failed and its leases released through
the same close-out step 6's failures take, so a crashed run stops holding the
queue. Its branch and worktree are left where they are, for a human.
`--supervise` is that acting sweep on a timer: one long-lived process per
target, held to one by a lockfile, sweeping every minute until it is told to
stop.
"""
import sys

from holophyte.cli import cli

if __name__ == "__main__":
    sys.exit(cli())

# The loop starts its own supervisor

**Status:** accepted · 2026-09-04 · merged as KO-248

## Context

The supervisor must be a separate process (a thread dies with the loop),
but it was also a separate command that the operator had to remember.

## Decision

At startup the loop checks the target's supervisor lock and, if nobody
holds it, spawns a detached `--supervise`. `[loop] spawn_supervisor =
false` for operators who run it under systemd.

## Consequences

`factory.py TARGET` is the whole command. The supervisor outlives the loop
on purpose.

## Tickets

KO-248, merged 2026-09-04.

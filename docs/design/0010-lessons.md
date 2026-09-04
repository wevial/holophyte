# Learning from recurring findings

**Status:** proposed · 2026-09-04

## Context

Lint rules and ratchets (ruff, C901 at 12) are permanent and mechanical;
AGENTS.md is read by the implementer before every job. Both are fed by
hand. Nothing notices when the reviewer raises the same kind of finding on
three different tickets.

## Proposal

The store already fingerprints findings. `--report --lessons` lists
finding families recurring across tickets. A per-target `LESSONS.md` is
included in the implementer's context (`[agents] context = [...]`).
Promotion stays human: a lesson becomes a lint rule, an AGENTS.md line, or
a validator advisory when the operator says so.

## Tickets

To file.

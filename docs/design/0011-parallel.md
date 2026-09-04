# Per-ticket leases and parallel loops

**Status:** proposed · 2026-09-04

## Context

The lease is per project (`projects.activeRunId`), so one run at a time.
Linear `blocks` relations are the only dependency the machine checks.
Worktrees are already isolated per run.

## Proposal

Lease per ticket (the column exists); N loop processes against one
project; a merge lock so merges into `main` serialise; the merge gate
merges `main` into the branch and re-verifies before merging. Conflicts
refuse to a human as they do now. File-overlap prediction is skipped.

## Consequences

Operable only with the attention queue in place, so it follows the
frontend. Multi-host parallelism needs [note 1](0001-store.md)'s second
store.

## Tickets

To file after the frontend.

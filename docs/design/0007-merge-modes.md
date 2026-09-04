# Merge modes: local or PR, with a round cap

**Status:** proposed · 2026-09-04

## Context

Today the loop merges `--no-ff` into `main` itself and never pushes. Some
repositories want GitHub review bots to see the change first, which means
a PR, which means pushing a branch.

## Proposal

A `[merge]` table per target: `mode = local | pr`. In `pr` mode the loop
pushes the branch, opens a PR carrying the ticket body and its FINDINGS
entry, then runs a shepherd loop until terminal: fetch unresolved review
threads, verdict each on merit, fix the accepted ones, reply with what
changed and the sha, resolve, wait for CI and new threads, repeat. Each
pass is a `reviewRounds` row with route `github:<bot>`, so FINDINGS and the
ledger show it like a Codex round. `pr_rounds = 5` caps the passes; past
it the run parks for the operator with the open threads listed. Rejects
and genuine questions are never auto-replied; they park too.

"The factory never pushes" becomes "the factory never pushes `main`".

## Consequences

A second merge path with its own tests; a GitHub token on the writer
host; the standing decision in the roadmap is amended.

## Tickets

To file after [note 8](0008-approval-and-checks.md), which it shares the
parking mechanism with.

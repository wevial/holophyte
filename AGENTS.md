# Holophyte Agent Guide

Holophyte is a minimal, Linear-driven software factory. `main` is the only
integration point; each claimed ticket is implemented in an isolated sibling
worktree and merged only after mechanical verification and independent review.

## Scope and execution

- Work one ready ticket at a time. Treat its approved Linear body as a frozen
  contract: scope may narrow, never expand.
- Before running `factory.py`, read its argument contract. Do not pass a
  convenience flag such as `--help` to a script that interprets its first
  argument as a repository path.
- Make implementation changes only in the task worktree/branch. Keep the main
  checkout untouched until the factory's merge gate succeeds.
- Preserve the ticket's exact relative-path, non-interactive verify commands.
  They must pass before review and again before merge.
- Commit a reviewable candidate before independent review. An implementer must
  never review or approve its own candidate.
- Keep one independent review plus at most one narrow re-review. Record review
  findings and their `ADDRESS`, `FOLLOW_UP`, or `DECLINE` adjudications in
  `FINDINGS.md` and the Linear ticket ledger.
- Do not treat a worker's report as proof. Inspect the diff and reproduce the
  ticket's verification commands before reporting completion.

## Tests and verification

- Test observable behavior, meaningful boundaries, and reproduced regressions;
  do not test private implementation details merely to inflate coverage.
- **Do not add tautological unit tests.** A test is invalid if it only restates
  the implementation under test, mirrors its branches/formula, asserts a value
  the test itself constructed without an independent oracle, or passes equally
  if the required behavior is absent.
- Prefer one to three focused tests for the main behavior. A behavior change or
  bug fix should show a meaningful RED before the smallest useful GREEN change.
- Add extra coverage only for material safety/destructive boundaries or a real
  demonstrated regression. Documentation, templates, and static configuration
  use proportional structural or smoke verification rather than artificial
  unit-test ceremony.
- Treat zero-test discovery and opaque shell failures as failures, not green
  verification. Verification must make the failed command and actionable output
  visible.

## Repository conventions

- Use the canonical ticket structure in `ticketTemplate.md` and validate it
  with `ticket_template.py` when ticket-template behavior changes.
- Keep credentials only in local environment/configuration; never commit or log
  secrets.
- Keep `FINDINGS.md` append-only as execution evidence. Do not delete history
  to make a run appear clean.
- Model/harness routing is an explicit factory policy: live-probe the exact
  configured CLI/provider/model path before dispatching a task, and do not
  silently substitute a model or harness when a route fails.

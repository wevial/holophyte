# Human approval, manual checks, and a checker role

**Status:** proposed · 2026-09-04

## Context

Three runs on 2026-09-03 failed because criteria asked the reviewer to
witness things it cannot see: a screen, a gitignored file, another
machine. Some checks need a human; more of them need a browser.

## Proposal

`[merge] approve = auto | human`. `human` parks the run in
`awaiting_merge_approval` (a phase that exists and is never entered),
puts the ticket in `blocked_on_operator` with "merge?" as its question,
and `/attention` surfaces it; `--approve KO-n` releases it, as does
merging the PR by hand in `pr` mode.

An optional `## Manual checks` section in the ticket template. Its lines
are run by a **checker**, a fourth role beside implementer, reviewer and
adjudicator: an ephemeral agent with browser use (Codex with a small
model at high effort is the first candidate) in a throwaway environment,
producing evidence (screenshots, a transcript) attached to the ledger and
the PR. Only a line marked `human:` stops the run for a person, and it is
highlighted in the PR body and in "needs you".

## Consequences

The reviewer's contract is unchanged; the checker absorbs what the
reviewer cannot witness. The drawer gains its first real write.

## Tickets

To file: `approve = human` and `--approve`; the checker role; the template
section.

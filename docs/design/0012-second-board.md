# A second board behind the provider protocol

**Status:** proposed · 2026-09-04

## Context

`Provider` is five methods: `claim_next`, `fetch_task`, `set_state`,
`comment`, `team`. Linear implements it; a file directory implements it
for tests. `--file-ticket` and `--update` still call Linear directly.

## Proposal

`[board] kind = linear | file | …`. Move filing and updating behind the
protocol. Then a Hermes kanban, GitHub Issues, or "our own" (the file
provider plus a page over the daemon) are each a small module.

## Consequences

Standing decision 2 in the roadmap says wait for the second real
consumer; the seam being ready is what makes waiting cheap.

## Tickets

To file when a second board is real.

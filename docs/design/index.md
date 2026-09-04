# Design notes

One note per idea, in the order they came up. Each carries a status, and
the status is the whole point: **accepted** means it is how the factory
works or is ticketed to be, **proposed** means it has a shape but no
decision, **open** means the shape itself is still being argued. Nothing
here is sorted into a bin before it has to be.

The standing decisions in the [roadmap](../roadmap.md) predate this
section and remain in force; new ones land here.

| # | Note | Status |
| --- | --- | --- |
| 1 | [SQLite stays the store; Convex is an optional second one](0001-store.md) | accepted |
| 2 | [One machine first; the tailnet is an operator choice](0002-single-machine.md) | accepted |
| 3 | [Branch prefix is configurable](0003-branch-prefix.md) | accepted |
| 4 | [The loop starts its own supervisor](0004-supervisor-spawn.md) | accepted |
| 5 | [Frontend before the Rust port; v0 served by the daemon](0005-frontend-before-rust.md) | accepted |
| 6 | [Docs on Cloudflare Pages](0006-docs-hosting.md) | accepted |
| 7 | [Merge modes: local or PR, with a round cap](0007-merge-modes.md) | proposed |
| 8 | [Human approval, manual checks, and a checker role](0008-approval-and-checks.md) | proposed |
| 9 | [The ledger lives in the store](0009-ledger.md) | proposed |
| 10 | [Learning from recurring findings](0010-lessons.md) | proposed |
| 11 | [Per-ticket leases and parallel loops](0011-parallel.md) | proposed |
| 12 | [A second board behind the provider protocol](0012-second-board.md) | proposed |
| 13 | [The console's shape](0013-console.md) | open |

## Writing one

Copy the shape below. Keep it under a screen; link the tickets that carry
it out rather than restating them. Change the status line when the status
changes and say why in one sentence under it.

```
# Title

**Status:** proposed · 2026-09-04

## Context
## Decision (or Proposal)
## Consequences
## Tickets
```

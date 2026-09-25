# A second board behind the provider protocol

**Status:** accepted · 2026-09-25 (proposed 2026-09-04)

Accepted because stage 0 of Phase 3 built the seam: `--file-ticket` now
files through the board it is handed, and Linear is one board among two.

## Context

The protocol was five methods, and `--file-ticket` and `--update` called
Linear directly, so a second board meant a second filing path.

## Decision

`provider.Board` is the seam. Beside the loop's members it has `file()`,
`update()` and `stored_body()`: create a ticket, revise it, read back
what the board stored. `board_for()` is the one place that builds a
project's board, reading `[board] kind` (`linear`, the default; `native`
refused until it ships); `[board] mode` (`mirror` or `store`) is
accepted beside it. `LinearBoard` and `FileProvider` both implement it,
and one conformance suite holds them alike. The rest of Phase 3 is the
"boards and the store" design, §2 and §10.

## Consequences

A new board is one class and one `kind` value; the loop and
`--file-ticket` do not change. The exit-2 line still says "as stored by
Linear" whatever the board; its wording waits for the native board.

## Tickets

KO-730, KO-731, KO-732, KO-734.

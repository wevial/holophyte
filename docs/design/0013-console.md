# The console's shape

**Status:** open · 2026-09-04

## Context

[Note 5](0005-frontend-before-rust.md) settles that v0 is a page served
by the daemon, read-only first. What it looks like is not settled. Five
mocks exist, all on the same real data:

- an interactive layout mock with toggles for layout, nav, density, run
  detail and writes, plus a component gallery;
- a "shift board": one-word verdict, needs-you inbox, the floor, a shipped
  ledger, dials;
- four alternatives: a monospace night-shift control room, a kanban board,
  an editorial broadsheet with a six-hour timeline, and a phone-width tile
  stack.

Ko's reaction, 2026-09-04: none of the five is it; something dashboard-like,
possibly a combination of the first mock's structure with pieces of the
others, "for the ability to see into what is going on"; and a sense of
overthinking it before a proper brainstorm.

## What is settled

The page reads from `/status`, `/runs`, `/attention` and the ledger; it
answers "what needs me" before anything else; it shows the live run's
phase, time box and heartbeat; it lists recent runs with their rounds,
findings and merge sha; writes come later behind the token.

## What is not

Layout, density, tone, whether the first screen is a verdict, an inbox, a
board or a timeline. Decide after the brainstorm; until then the drawer is
the console.

## Tickets

None. Do not ticket the page before this note is accepted.

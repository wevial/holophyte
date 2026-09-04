# One machine first; the tailnet is an operator choice

**Status:** accepted · 2026-09-04

## Context

The factory ran across two machines (a writer host and the operator's
seat) joined by a Tailscale tailnet, and the docs were written that way
round. The code never references the network: the only places it appears
are the address given to `--serve` and the URLs in the drawer's config.

## Decision

Holophyte is a single-machine tool that happens to work across machines.
Docs are written single-machine first with no real addresses; the
two-machine setup is a second page. `--serve PORT` defaults to loopback.
Review bots, tailnets and hosted stores are optional layers around a core
that is one machine, one store, one loop.

## Consequences

A docs rewrite and a ten-minute code change. The daemon's "no auth, bind
address is the boundary" rule reads correctly on both setups.

## Tickets

To file: single-machine docs; `--serve PORT` loopback default.

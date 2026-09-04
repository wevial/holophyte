# Frontend before the Rust port; v0 served by the daemon

**Status:** accepted · 2026-09-04

## Context

KO-168 plans a Rust port module by module once the Python seams stop
moving; its own trigger is a few weeks without structural change. Ko asked
whether to port before or after a frontend.

## Decision

Frontend first. A frontend consumes the daemon's JSON, which a Rust daemon
would have to honour anyway, so it is untouched by the port. Building the
client is what shapes the API (`/attention` exists because the drawer
needed it). Rust waits until the JSON and the schema stop moving.

v0 is a page served by the daemon at `/`, read-only over `/status`,
`/runs`, `/attention` and the ledger, with no build step, so it works on
one machine with nothing installed. Writes come behind a serve token.

## Tickets

Depends on [note 13](0013-console.md) for the page's shape.

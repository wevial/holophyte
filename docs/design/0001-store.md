# SQLite stays the store; Convex is an optional second one

**Status:** accepted · 2026-09-04

## Context

One target has one SQLite file in WAL mode with `BEGIN IMMEDIATE` claims;
one target is served by one host. That gives crash recovery, offline use
and a zero-infrastructure single-machine install. Ko asked whether Convex
could hold the state instead, for concurrency across machines.

## Decision

SQLite remains the default and only required store. Concurrency on one
host is already solved; what Convex would add is several writer hosts on
one project through serializable mutations, plus reactive queries for a
frontend. That is wanted only once multi-host parallelism is wanted. When
it is, the store becomes a protocol with two implementations held to one
conformance suite, the way `Provider` already is. Secrets for the second
store live in the environment, never in `config.toml`.

## Consequences

The frontend is designed against the daemon's JSON, not the store, so it
does not care which store is behind it. Multi-host durability meanwhile
is file replication (Litestream or rsync), not centralisation.

## Tickets

None yet; depends on [note 11](0011-parallel.md).

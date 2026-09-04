# Branch prefix is configurable

**Status:** accepted · 2026-09-04

## Context

Task branches are `task/<ticket>-<title>`, where `<ticket>` is the Linear
identifier lowercased (`ko-227`). The `ko` is the team prefix, not a word.

## Decision

`[worktree] branch_prefix`, default `task`. Everything after the slash is
unchanged, because the ticket id is what makes a preserved branch traceable.

## Tickets

To file.

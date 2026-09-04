# The ledger lives in the store

**Status:** proposed · 2026-09-04

## Context

The store holds every round, finding, fingerprint and intervention.
FINDINGS.md is a rendered window over it. The narrative (why a finding was
declined, why a contract was revised) exists only in Linear comments the
operator writes by hand.

## Proposal

A `ledger` table the loop writes at each close-out from what it already
knows: rounds, adjudications, operator steps. The Linear comment becomes a
projection of that row, as ticket status already is. FINDINGS.md becomes
`[report] findings = window | off`, kept on for public repositories. The
daemon serves the ledger per run.

## Consequences

The narrative survives without the operator; a frontend can show it; the
file stops being load-bearing.

## Tickets

To file.

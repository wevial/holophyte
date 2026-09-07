# {{TITLE}}

## Summary

<Describe the outcome in one or two sentences.>

## What / Why / How

**What:** <What observable behavior or capability are we delivering?>

**Why:** <What problem does this solve, for whom? Optional for personal projects.>

**How:** <Intended technical direction, important constraints, and existing patterns to reuse — without over-specifying incidental implementation details.>

## In scope

<!-- Scope caps, enforced by ticket_template.py: max 3 entries here, 5 acceptance criteria, 30 min estimate. Every list entry counts, whatever its marker. Split anything larger. -->

- <Behavior, surface, route, data, or integration included in this ticket.>

## Out of scope

- <Adjacent work or future enhancement that is explicitly excluded.>

## Acceptance criteria

- [ ] Given <starting condition>, when <action>, then <observable result>.
- [ ] <Only meaningful success, failure, empty, or permission outcomes.>
- [ ] <Existing behavior that must remain unchanged, when relevant.>

> Rules the reviewer enforces (each learned from a failed run):
> 1. Every behavioral claim needs a mechanical witness: a "default X" / "sorted
>    by Y" criterion must be pinned by a test or check that asserts the
>    observable order/behavior, written "(a test in FILE witnesses X)" —
>    presence alone lets the claim silently regress (KO-138).
> 2. A criterion is a closed list of named cases, never "every" or "any" over
>    an unstated set — the reviewer cannot witness an open set (run 138).
> 3. "The suite and ruff pass" is never a criterion: the verify block already
>    enforces it and the reviewer cannot reproduce it in the container
>    (runs 92 and 93).
> 4. A grep pattern in the verify block never starts with a slash; write `[/]`
>    for a literal one (`grep -q 'GET [/]peers'`) — the validator reads a
>    quoted pattern beginning with a slash as an absolute path and refuses it.
> 5. After filing, the body changes only through `--file-ticket --update`, so
>    the stored contract and the validated file never drift apart.

## Verify command(s)

```
<Exact runnable command(s). Exit code 0 = pass. The factory runs these
mechanically before review and before merge; they must be non-interactive
and depend only on the repo plus its declared toolchain.>

Rules:
- Run from the repo/worktree root; use RELATIVE paths only (never `cd` to an
  absolute repo path — the factory executes these inside a per-task worktree).
- Assert BEHAVIOR (a command succeeds, output is valid, tests pass), not
  implementation details (specific filenames, internal structure). Where the
  code lives is the implementer's call if behavior holds.
- Keep each command deterministic and idempotent.
- Never assume an ambient interpreter: bare `python`, `python3`, or `pip`
  resolve to whatever is on PATH, which on a factory host may be another
  project's venv (holophyte-bugs.md #5). A command that needs the project
  venv must activate it first (`. .venv/bin/activate && ...`) or use the
  venv's interpreter path (`.venv/bin/python -m ...`).
```

## Contract checks

<!-- OPTIONAL: keep only if this ticket has a literal value that must not
     drift (a port, a URL, a version). Delete the whole section otherwise. -->

```
<relative/path/to/file>: <exact literal that must appear in that file>

Rules:
- One declaration per line: a RELATIVE repo path, a colon, then the literal.
- The literal is compared verbatim as a substring — no globs, no regex, no
  shell. The gate fails naming the path and the literal when it is absent.
```

## Implementation notes

- <Known constraints, dependencies, risks, rollout concerns, or useful code landmarks.>

## Estimate & dependencies

Estimate: N min · Depends on: <ticket IDs or "none">

> Machine-checkable dependencies are Linear **blocks** relations — the loop
> enforces only those. Anything outside Linear (DNS, human review, hardware)
> gates via triage: the ticket stays in Backlog until resolved, then moves to
> Todo. Keep this line in sync with the relations for human readers.

## Open questions

- None  <!-- must read exactly this before the ticket enters the pickable queue -->

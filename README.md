# holo2

A minimal software factory. Two small Python files, no frameworks.

Tickets live in a Linear project; `main` is the only integration point.

Usage: `python3 factory.py /srv/dev/holo2test`

## The loop

1. Claim the first ready ticket — non-terminal and unblocked (Linear
   `blocks` relations are the only machine-checked dependencies).
2. Cut a per-task branch in a sibling worktree (`<repo>.worktrees/`), so
   the main checkout stays untouched.
3. Implementer agent (opencode, write access) implements and commits,
   under a wall-clock budget from the ticket's estimate (default 20 min).
4. Verify gate: the ticket's mechanical verify command must pass before
   each review round and again before merge.
5. Read-only reviewer agent (Claude) reviews the diff against the task;
   findings go back to the implementer for one fix round. Max 2 review
   rounds.
6. On approval: `--no-ff` merge to `main`, worktree and branch cleaned
   up, ticket → Done.
7. On failure (budget blown, no commits, verify stuck, 2 failed rounds):
   the loop stops and leaves the branch + worktree behind for a human;
   the ticket stays In Progress. A no-commit task is discarded outright —
   there is nothing to preserve.

## Files

- `factory.py` — the loop.
- `linear_provider.py` — Linear GraphQL client: claim/complete/comment,
  ready-ticket and blocker resolution.
- `ticketTemplate.md` — ticket shape. Verify commands go in the
  "Verify command(s)" section (exit 0 = pass, relative paths only);
  estimate is the budget in minutes.
- `strman.py` — small string utilities.
- `FINDINGS.md` (generated) — append-only review/merge ledger, mirrored
  to Linear ticket comments.

## Config

`LINEAR_API_KEY` and `HOLO2_PROJECT_ID` — env vars or `.env` next to
`linear_provider.py`.

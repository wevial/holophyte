# Holophyte Agent Guide

Holophyte is a minimal, Linear-driven software factory. `main` is the only
integration point; each claimed ticket is implemented in an isolated sibling
worktree and merged only after mechanical verification and independent review.

## Scope and execution

- Work one ready ticket at a time. Treat its approved Linear body as a frozen
  contract: scope may narrow, never expand.
- `factory.py [repo]` runs the loop and `factory.py --report [repo]` prints the
  store's estimate-vs-actual table without claiming anything. The command line
  is parsed, not indexed, so `--help` is safe to ask for.
- Make implementation changes only in the task worktree/branch. Keep the main
  checkout untouched until the factory's merge gate succeeds.
- Preserve the ticket's exact relative-path, non-interactive verify commands.
  They must pass before review and again before merge.
- Commit a reviewable candidate before independent review. An implementer must
  never review or approve its own candidate.
- Keep one independent review plus at most one narrow re-review. Record review
  findings and their `ADDRESS`, `FOLLOW_UP`, or `DECLINE` adjudications in the
  Linear ticket ledger; the factory records the round in the store and renders
  it into `FINDINGS.md`.
- Do not treat a worker's report as proof. Inspect the diff and reproduce the
  ticket's verification commands before reporting completion.

## Tests and verification

- Test observable behavior, meaningful boundaries, and reproduced regressions;
  do not test private implementation details merely to inflate coverage.
- **Do not add tautological unit tests.** A test is invalid if it only restates
  the implementation under test, mirrors its branches/formula, asserts a value
  the test itself constructed without an independent oracle, or passes equally
  if the required behavior is absent.
- Prefer one to three focused tests for the main behavior. A behavior change or
  bug fix should show a meaningful RED before the smallest useful GREEN change.
- Add extra coverage only for material safety/destructive boundaries or a real
  demonstrated regression. Documentation, templates, and static configuration
  use proportional structural or smoke verification rather than artificial
  unit-test ceremony.
- Treat zero-test discovery and opaque shell failures as failures, not green
  verification. Verification must make the failed command and actionable output
  visible.

## Repository conventions

- Use the canonical ticket structure in `ticketTemplate.md` and validate it
  with `ticket_template.py` when ticket-template behavior changes.
- Keep credentials only in local environment/configuration; never commit or log
  secrets.
- The repository is public: code, comments, docs and commit messages name roles
  (the writer host, the operator seat), never machine names or personal paths.
- `FINDINGS.md` is regenerated from the store at each close-out: a bounded
  window over `runs`/`reviewRounds` below the frozen pre-store preamble. The
  execution evidence is the rows — do not hand-edit the rendered window or drop
  rows to make a run appear clean.
- Model/harness routing is an explicit factory policy: live-probe the exact
  configured CLI/provider/model path before dispatching a task, and do not
  silently substitute a model or harness when a route fails.
- Cyclomatic complexity above 12 is a lint failure (ruff `C901`); an exemption
  is a per-function `noqa: C901` that names its reason and the ticket that
  retires it.

## Operator protocol

- **Escalation ladder, in order.** When the factory is stuck: (1)
  relaunch/unblock through the factory's legal paths; (2) `factory.py
  <repo> --sweep`, then `--sweep --act` once a trip is confirmed; (3)
  store *API* calls from a Python REPL (`release`, `resume`,
  `transition`, `record_intervention`, `walk_ticket`); (4) raw SQL only
  where no API exists — and then only paired with a ticket for the
  missing API, filed the same day. Never skip a rung downward.
- **Operator store API, by name.** `release`, `resume`, `transition`,
  `record_intervention` and `walk_ticket` are kept for the REPL rung above
  even when the loop itself does not call them; `tests/test_store_surface.py`
  holds the module's public surface to an explicit allow-list and checks
  these five against this list, so a removal or addition is deliberate.
- **A stuck or refused lease is a `--sweep` question, not a SQL
  question.** The first response to "lease already held by run N" is a
  read-only `--sweep` (the loop now runs one at startup and prints it);
  the second, after the strike interval confirms silence, is
  `--sweep --act`. Hand-editing `runs`/`projects` to free a lease is
  prohibited now that the sweep exists.
- **Two relaunches, then diagnose.** At most two relaunches against the
  same infrastructure failure. The third response is a written diagnosis
  and plan, not another relaunch.
- **Record before acting.** Every out-of-band state change gets its
  interventions row (`store.record_intervention()`, action `close_out`
  for a close-out — never a mislabeled `resume`) or at minimum a
  runEvent *before* the write, in the same transaction where possible,
  with truthful action semantics and real timestamps — `act_on_trip()`'s
  confirm-callback is the house style. Backdating or mislabeling a
  record is worse than no record.
- **Manual merge to main is a named event with a gate — and an
  ask-first boundary.** Requirements: suite green, ruff clean, an
  independent review pass over the final branch state (the loop's gate
  is verify *and* approve — substitute the reviewer, never skip it),
  `--no-ff` with a message naming the why, and store + Linear walked to
  their terminal states in the same sitting (`store.walk_ticket()`),
  FINDINGS window committed. When the human is present or watching: ask
  before the first out-of-band state edit and always before a manual
  merge to main. When absent: freeing a work-blocking lease and
  preserving at-risk work (stash rescue) are authorized — reversible,
  additive — with the merge question parked `blocked_on_operator` until
  they return.
- **Close the loop afterwards.** Reconcile every touched surface (store
  status, board status, FINDINGS, branches/stashes) before ending the
  incident, and file one ticket per gap the incident revealed.

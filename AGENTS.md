# Holophyte Agent Guide

Holophyte is a minimal software factory. It claims a ticket from the
project's board, implements it in an isolated sibling worktree, verifies and
reviews it, and lands it on `main` directly or through a pull request
(`[merge] mode`). The first three sections are for every agent; the last is
for the operator.

## Working a ticket
* The ticket you were handed is the contract as it was claimed. Meet every
  acceptance criterion and nothing more. An unmet or unwitnessed criterion
  blocks approval, so if one cannot be met, say so in your output rather
  than drop it.
* Work only in the task worktree, and commit there. The factory pushes,
  opens pull requests, merges and writes to the board; you do none of them.
* Run the ticket's verify commands as written, `ruff check .`, and the
  focused test modules you touched
  (`python3 -m unittest discover -s tests -p 'test_x.py'`). Do not run the
  whole suite in a ticket worktree; it runs as the `unit` pull request check.
* On review findings, adjudicate each one in the fix commit's message:
  `ADDRESS` (fix now), `FOLLOW_UP` (valid, out of scope) or `DECLINE` (with
  the reason). Fix only the `ADDRESS` items. The factory records the rounds.
* Commit messages carry no AI attribution or co-author lines.
* A reviewer changes nothing, and no agent reviews its own candidate.

## Tests
* Test observable behavior, meaningful boundaries and reproduced regressions,
  not private details.
* **No tautological tests.** A test is invalid if it restates the
  implementation, mirrors its branches, asserts a value it built itself with
  no independent oracle, or passes with the behavior absent.
* One to three focused tests per behavior; a bug fix shows a meaningful RED
  before the smallest GREEN. Docs and config get proportional smoke checks.
* A change that crosses into git, containers, file permissions or another
  external tool has at least one test that runs the real tool (real `git` in
  a temporary repository), not only a fake.
* Zero tests discovered, or an opaque shell failure, is a failure.

## Suite pins and conventions

These fail the `unit` check, which you do not run locally, so settle them in
the same commit:
* `tests/test_file_sizes.py`: 1000 lines a module, 1500 a test module. A file
  in `PINNED` may not grow past its pin; raise the pin in the same commit.
* `tests/test_store_surface.py`: a public `store` function added or removed
  edits the allow-list with the ticket that needs it. No raw
  `UPDATE runs|tickets|projects` under `holophyte/`: use a named store writer.
* `tests/test_docs*.py`: a new CLI mode goes in the README usage block, a new
  module in `docs/development.md`, a config key in `docs/config.md`, a daemon
  key in `docs/reference/http.md`.
* Complexity above 12 fails ruff `C901`; an exemption is a per-function
  `noqa: C901` naming its reason and the ticket that retires it.
* Boards are reached only through `provider.Board`, built by
  `board_for()`; never call `linear_provider` directly.
* Schema changes bump `SCHEMA_VERSION` and stay additive where they can
  (`store/schema.py` says when `READABLE_FROM` must rise). Tickets never state
  a literal schema version.
* Agent routes are live-probed before dispatch; the only substitution is a
  configured `*_fallback`, probed and recorded. Never switch silently.
* The store is the run record (console, `--report`, `--status`); this
  repository keeps no FINDINGS.md file.
* The repository is public: name roles (the writer host, the operator seat),
  never machine names or personal paths. Credentials stay in local config.

## Operator

The runbook (`docs/operating/runbook.md`) is the procedure; these are the
rules it serves.
* **Escalation ladder, in order.** (1) The factory's own verbs: `--requeue`,
  `--approve`, `--babysit`, `--repoint`, `--pause`/`--resume`/`--abort`,
  `--hold`/`--release-hold`, `--close KO-n --landed URL`,
  `--file-ticket --update`; a loop is relaunched as
  `holophyte-loop@NAME` (the host sweep also starts it for a ready ticket).
  (2) `--sweep`, then `--sweep --act`; on a registered project the host
  sweep already acts every minute, so first check it is running
  (`factory.py --status`). A stuck lease is a sweep question: never
  hand-edit `runs`, `tickets` or `projects` to free one. (3) The store API below from a REPL. (4) Raw SQL
  only where no API exists, paired with a ticket for the missing API filed
  the same day. Never skip a rung downward; two relaunches against one
  failure, then a written diagnosis.
* **Operator store API, by name.** `release`, `resume`, `transition`,
  `record_intervention`, `record_project_intervention`, `walk_ticket`,
  `requeue` and `repair_references` are kept for rung 3 even when the loop
  does not call them; `tests/test_store_surface.py` checks these eight.
* **Record before acting.** Every out-of-band state change gets its
  interventions row, with truthful action and real time, before the write;
  the verbs write it for you. A backdated or mislabeled record is worse than
  none.
* **A worker's report is not proof.** Read the diff and rerun the verify
  commands before calling anything done.
* **Tickets** follow `ticketTemplate.md`; check one with
  `python3 ticket_template.py --repo . TICKET.md`, file it with
  `--file-ticket`, and change a filed body only with
  `--file-ticket --update KO-n`.
* **After a merge** nothing is restarted by hand: the sweep timer runs from
  `HEAD`, the host daemon exits on a `HEAD` move, the loop re-execs.
* **A merge outside the loop** goes through a pull request, never a push
  straight to `main` (an admin push skips the required `unit` check). It
  needs the suite green, ruff clean and an independent review of the final
  branch state; a ticket the factory ran is closed with
  `--close KO-n --landed URL`. Ask before it, and before the first
  out-of-band state edit, when the human is present. When absent, freeing a
  work-blocking lease and preserving at-risk work are authorized; the merge
  waits `blocked_on_operator`.
* **Close the loop.** Reconcile store, board and branches before ending an
  incident, and file one ticket per gap it revealed.

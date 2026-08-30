# holo2

A minimal software factory. Two small Python files, no frameworks.

Tickets live in a Linear project; `main` is the only integration point.

Usage: `python3 factory.py /srv/dev/holo2test`, or
`python3 factory.py --report /srv/dev/holo2test` for the timing table.

## The loop

1. Claim the first ready ticket — non-terminal and unblocked (Linear
   `blocks` relations are the only machine-checked dependencies).
2. Cut a per-task branch in a sibling worktree (`<repo>.worktrees/`), so
   the main checkout stays untouched.
3. Implementer agent (Claude Code / Opus at high effort, write access)
   implements and commits under a wall-clock budget from the ticket's estimate
   (default 20 min).
4. Verify gate: the ticket's mechanical verify command must pass before
   each review round and again before merge. A failure is fail-loud: a
   top-level `&&` chain is run clause by clause in one shell, and the report
   names the clause that failed and its exit status, shows the output of
   every clause that ran, and names the clauses the failure short-circuited
   — silence is reported as silence, never as a bare non-zero exit.
5. Local reviewer agent (Codex / GPT-5.6 Sol at medium effort) reviews the
   diff against the task inside the hardened container boundary described
   below;
   findings go back to the implementer for one fix round. Max 2 review
   rounds.
6. Merge gate: the verify command passes again, and the ticket is re-read
   from Linear and held against the snapshot the claim froze (title,
   acceptance criteria, verify commands). A body edited while the run was
   working refuses the merge and preserves the branch — the candidate answers
   the ticket as it was claimed, not as it now reads. A ticket that cannot be
   re-read (Linear down, issue gone) is *not* read as drift: the run records
   that the check had no evidence and the merge goes ahead on the frozen
   contract. On a clean gate: `--no-ff` merge to `main`, worktree and branch
   cleaned up, ticket → Done.
7. On failure (budget blown, no commits, verify stuck, 2 failed rounds):
   the loop stops and leaves the branch + worktree behind for a human;
   the ticket stays In Progress. A no-commit task is discarded outright —
   there is nothing to preserve.
8. On the *second* failed run of the same ticket (`MAX_FAILED_RUNS`), the
   ticket is blocked instead of left open: its stored status becomes
   `blocked_on_operator` and one Linear comment lists what each failed run
   ended on. The claim path re-reads that count before every claim, so the
   ticket is refused even when the board has been dragged back to Todo —
   unblocking is a human's move, not the loop's. A refused ticket is skipped
   and the next one is claimed: a blocked ticket still projects to Todo and
   still sorts where it sorts, so stopping on it would starve every ticket
   behind it.

## Files

- `factory.py` — the loop, and `--report`: a read-only query over the store
  printing estimate vs actual per finished run (actual, estimate, ratio,
  rounds, outcome) with mean and median ratio. Each run row carries the
  estimate it was claimed under and the round count stamped at close-out, so
  the report and FINDINGS.md read the same numbers. `--report` claims no
  ticket, cuts no worktree and calls no one.
- `review_runner.py` — exact-SHA staging and the model-neutral local reviewer
  boundary.
- `docker/reviewer.Dockerfile` — pinned minimal reviewer image.
- `linear_provider.py` — Linear GraphQL client: claim/fetch_task/set_state/
  comment,
  ready-ticket and blocker resolution. Ticket status lives in the store and is
  projected onto a Linear workflow state by `factory.mirror_push()` — one way,
  last write wins, never read back — so `set_state` is the only writer of that
  state, and the mapping table beside `mirror_push` says which state each
  status shows as. The direction holds when the board is stale too: a ticket
  whose stored status will not enter `in_flight` is refused at the claim and
  re-projected instead of worked a second time.
- `ticketTemplate.md` — ticket shape. Verify commands go in the
  "Verify command(s)" section (exit 0 = pass, relative paths only);
  estimate is the budget in minutes. The optional "Contract checks" section
  declares `relative/path: exact literal` lines the gate asserts verbatim, so
  a required value (a port, a URL) cannot drift while the commands still pass.
- `ticket_template.py` — parser/validator for that shape;
  `python3 ticket_template.py TICKET.md [...]` exits 0 iff the ticket is
  pickable-ready.
- `tests/test_ticket_template.py` — stdlib unittest suite for it
  (`python3 -m unittest discover tests`).
- `strman.py` — small string utilities.
- `FINDINGS.md` (generated) — a rendered window over the store, not a log:
  the factory regenerates it at each close-out from `runs`/`reviewRounds` as
  the newest 25 entries below a `<!-- store-rendered below -->` marker, with
  everything older counted in one archive line and kept in `holophyte.db`.
  Text above the marker is frozen pre-store history and is never rewritten;
  Linear ticket comments stay the full per-ticket archive.

## Linting

`ruff check .` from the repo root; it exits 0 when the tree is clean. Run it
alongside the tests — the developer verify path is:

```
ruff check .
python3 -m unittest discover -s tests
```

The configuration lives in `pyproject.toml` under `[tool.ruff]`: line length
88, target `py311`, and rule sets `E`, `F`, `W`, `I` (pycodestyle errors and
warnings, pyflakes, import ordering). Nothing is formatted, only checked.
Every enabled rule is a promise the factory keeps forever, so the selection
stays small, and a violation that has to stand is suppressed with a per-line
`# noqa: <CODE>` rather than a file-level or blanket ignore.

ruff is a developer tool, not a dependency: install it on the host with
`pip install --user ruff` (or `uv tool install ruff`). It is never vendored.

## Config

`LINEAR_API_KEY` and `HOLO2_PROJECT_ID` — env vars or `.env` next to
`linear_provider.py`.

Per-target behavior lives in `<repo>.holophyte.toml`, a sibling of the target
the way `<repo>.holophyte.db` and `<repo>.worktrees` are. The file is optional:
absent means every default below stays in place, which is how the factory runs
against itself. A file that exists but does not parse is a startup error naming
the file and the line — a config the operator wrote is never silently ignored.
Tables this version does not know are left alone.

```toml
[agents]
# Each role's harness command. The task goal is appended as the last argument,
# so end the command where its prompt goes (`-p` for Claude Code, nothing for
# `codex exec`). Omit a role to keep its default.
implementer = "claude --model opus --effort high -p"   # default route
reviewer    = "my-reviewer --diff"                     # see the caveat below
adjudicator = "my-reviewer --final"
```

Defaults, in place whenever the key is absent: `claude -p <goal> --model opus
--effort high` implements; `review` and `adjudicate` go through the hardened
container described below. **A `reviewer` or `adjudicator` override is also an
opt-out of that container** — the configured command runs directly in the task
worktree. Overriding the implementer has no such effect; it already runs there.

## Local reviewer boundary

The factory never gives a reviewer the implementation worktree directly.
`review_runner.py` stages the frozen base and candidate commits into a fresh,
detached, zero-remote Git repository and verifies its identity before and after
the review. Docker mounts that repository at `/workspace` read-only. The
container also has a read-only root filesystem, no Linux capabilities, no
privilege escalation, bounded processes/memory/CPU, and no Docker socket or
host home.

Codex runs with `danger-full-access` **inside** this container because Ubuntu's
AppArmor policy blocks its nested Bubblewrap sandbox in the Hermes service
context. The outer container is the enforcement boundary: an actual write
probe under `/workspace` must fail before the model is called. Only a
disposable copy of `~/.codex/auth.json` and the installed Codex release binaries
are mounted; the copy and all reviewer state are removed afterward. Outbound
network remains enabled because Codex uses remote inference, but no GitHub,
SSH, Linear, Docker, or unrelated host credentials are exposed.

The first review builds `holophyte-reviewer:ubuntu24.04-v1` automatically from
the digest-pinned Ubuntu image. A run fails closed if preflight identity or
write rejection fails, the Codex tool host cannot execute a local command, the
container times out, or the staged repository fingerprint changes.

# Development

The package map as it is at merge time, the tests, and linting. Back to the
[README](index.md).

## Files

The `holophyte/` package is the factory; `factory.py` is its entry point.
Each module, one line:

- `holophyte/__init__.py` — the package docstring: which module owns what.
- `holophyte/cli.py` — the argument parser and mode dispatch: `--report`,
  `--requeue`, `--file-ticket`, `--sweep [--act]`, `--supervise`,
  `--serve` and the loop itself.
- `holophyte/target.py` — where a target's state lives (`HOLOPHYTE_HOME`,
  the `<slug>` directory, legacy adoption) and the `Target` value.
- `holophyte/config.py` — `config.toml` and every table it can set, checked
  at startup.
- `holophyte/gates.py` — the verify gate: a ticket's command in, a red or
  green fail-loud report out.
- `holophyte/agents.py` — the agent routes and the `agent()` call, one turn
  of a role.
- `holophyte/review.py` — reviewer output as structured findings and a
  verdict.
- `holophyte/findings.py` — `FINDINGS.md` as a bounded window over the
  store's rows.
- `holophyte/report.py` — `--report`: estimate vs actual per finished run
  (actual, estimate, ratio, rounds, outcome) with mean and median ratio, a
  read-only query over the store that claims no ticket, cuts no worktree and
  calls no one.
- `holophyte/runs.py` — the store seam: a run's progress as store rows.
- `holophyte/board.py` — Linear as the notice board: the ticket mirror, its
  pushes, `--file-ticket` and the escalation. Ticket status lives in the
  store and is projected onto a Linear workflow state by `mirror_push()` —
  one way, last write wins, never read back — so the provider's `set_state`
  is the only writer of that state, and the mapping table beside
  `mirror_push` says which state each status shows as.
- `holophyte/supervisor.py` — the stale-run sweep, its report, the lock and
  the `--supervise` loop.
- `holophyte/serve.py` — `--serve PORT|HOST:PORT`, the read-only HTTP daemon.
- `holophyte/loop.py` — the loop: worktree setup and reuse, `run_task`,
  `main`, `report`, `requeue` and the self-merge re-exec.
- `holophyte/reexec.py` — `reexec_self`, the shared self re-exec the loop
  and the supervisor both restart themselves through.

The store is its own package:

- `store/__init__.py` — the v2 durable state store, one WAL-mode SQLite
  file: schema, claims and leases, ticket and run-phase transitions, review
  rounds, interventions, and the state-graph renderer.
- `store/read.py` — typed read views over the store: one query, one row
  type, no SQL elsewhere.

At the root:

- `factory.py` — the entry point: imports `cli` from the package and calls
  it. Holds no `def` or `class` of its own.
- `provider.py` — the `Provider` protocol the loop talks to a board through.
- `linear_provider.py` — the Linear GraphQL client: claim/fetch_task/
  set_state/comment, ready-ticket and blocker resolution, issue creation.
- `review_runner.py` — exact-SHA staging and the model-neutral local
  reviewer boundary (see [Reviewing](reviewing.md)).
- `ticket_template.py` — parser/validator for the ticket shape;
  `python3 ticket_template.py TICKET.md [...]` exits 0 iff the ticket is
  pickable-ready.
- `ticketTemplate.md` — the ticket shape. Verify commands go in the
  "Verify command(s)" section (exit 0 = pass, relative paths only);
  estimate is the budget in minutes. The optional "Contract checks" section
  declares `relative/path: exact literal` lines the gate asserts verbatim, so
  a required value (a port, a URL) cannot drift while the commands still pass.
- `docker/reviewer.Dockerfile` — pinned minimal reviewer image.
- `FINDINGS.md` (generated) — a rendered window over the store, not a log:
  the factory regenerates it at each close-out from `runs`/`reviewRounds` as
  the newest 25 entries below a `<!-- store-rendered below -->` marker, with
  everything older counted in one archive line and kept in the store.
  Text above the marker is frozen pre-store history and is never rewritten;
  Linear ticket comments stay the full per-ticket archive.
- `tests/` — the stdlib unittest suite, one `test_*.py` per surface, with
  `tests/fake_agent.py`, `tests/procs.py` and `tests/waiting.py` as shared
  helpers. Run it
  with `HOLOPHYTE_HOME=$(mktemp -d) python3 -m unittest discover -s tests`.

## Linting

`ruff check .` from the repo root; it exits 0 when the tree is clean. Run it
alongside the tests — the developer verify path is:

```
ruff check .
python3 -m unittest discover -s tests
```

The configuration lives in `ruff.toml`: line length
88, target `py311`, and rule sets `E`, `F`, `W`, `I`, `C90` (pycodestyle
errors and warnings, pyflakes, import ordering, McCabe complexity). Nothing is formatted, only checked.
Every enabled rule is a promise the factory keeps forever, so the selection
stays small, and a violation that has to stand is suppressed with a per-line
`# noqa: <CODE>` rather than a file-level or blanket ignore.

Cyclomatic complexity above 12 is a lint failure (ruff `C901`); an exemption
is a per-function `noqa: C901` that names its reason and the ticket that
retires it.

ruff is a developer tool, not a dependency: install it on the host with
`pip install --user ruff` (or `uv tool install ruff`). It is never vendored.


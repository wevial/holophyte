# Seams and modules

The package split (phase 2, 2026-09-02) turned one 3,300-line file into
modules with named seams. The seams are the point: each is a place where a
later implementation, a test double, or a port in another language can
stand in without the rest noticing. [Development](../development.md) lists
every file; this page lists what each seam promises.

## The seams

| Seam | Where | Promise |
| --- | --- | --- |
| **`Target`** | `holophyte/target.py` | Everything about where a target's state lives, as a value: repository path, state directory, store path, config path. No module-level globals name a target; a function that needs one takes it. Two targets can exist in one process, which is what the tests, the daemon and a future port need. |
| **`Provider`** | `provider.py` | The board as a protocol: `claim_next`, `fetch_task`, `set_state`, `comment`, `team`. `LinearProvider` lazily imports the GraphQL module; `FileProvider` reads a directory of `<ID>.md` files for tests and offline runs. The loop never names Linear. |
| **`store.read`** | `store/read.py` | Typed, read-only views; the only SQL outside `store/__init__.py`. Every consumer that renders state (report, sweep, findings, serve) goes through it. |
| **`runs`** | `holophyte/runs.py` | The loop's store seam: `open_store`, `set_phase`, `heartbeat_while`, `record_round`, `warn_on_run`. Five helpers, so a wiring change extends one file instead of threading SQL through the loop. |
| **`gates`** | `holophyte/gates.py` | Worktree cutting and reuse, the verify gate, process-group reaping. Takes a target and a ticket, returns a red or green report. |
| **`agents`** | `holophyte/agents.py` | `agent_route()` (which command, which model, from `[agents]`) and `agent()` (one turn of a role in a process group with a budget). The implementer and the reviewer are both routes; `review_runner` is the reviewer's transport. |
| **`review`** | `holophyte/review.py` | Reviewer prose in, structured findings and a verdict out: the `CRITERION n:` checklist parser, the witness-test resolver, the finding key. |
| **`findings`** | `holophyte/findings.py` | The `FINDINGS.md` window renderer, byte-stable, from `EndedRun` and `ReviewRound` rows only. |
| **`board`** | `holophyte/board.py` | Linear as a notice board: mirror a ticket into the store with its contract snapshot, push status, detect drift at merge, escalate a twice-failed ticket, file and update tickets from files. |
| **`reexec`** | `holophyte/reexec.py` | Replace the process with the same command line, through an `EXEC` seam tests can intercept. Shared by the loop and the supervisor. |
| **`config`** | `holophyte/config.py` | Every `config.toml` table as a typed value with defaults, validated at startup; unknown keys are startup errors. |

## What depends on what

```mermaid
flowchart TB
  cli[cli] --> loop
  cli --> supervisor
  cli --> serve
  cli --> report
  cli --> board
  loop --> gates
  loop --> agents
  loop --> runs
  loop --> board
  loop --> findings
  loop --> reexec
  supervisor --> reexec
  supervisor --> report
  agents --> review_runner[review_runner]
  runs --> review
  runs --> store
  gates --> store
  board --> provider
  board --> ticket_template[ticket_template]
  provider -.lazy.-> linear_provider[linear_provider]
  findings --> read[store.read]
  report --> read
  serve --> read
  supervisor --> read
  supervisor --> store
  read --> store
  loop --> config
  supervisor --> config
  serve --> config
  gates --> config
  agents --> config
  board --> config
  everything[every module] --> target[Target]
```

Arrows point at what a module imports. Three rules hold the graph in this
shape: `serve` imports `store.read` and never `store` (it cannot write);
`holophyte.config` never imports `factory` or the loop (no cycles); and
nothing outside `store/` writes SQL.

## Configuration as the second seam

Everything an operator would otherwise patch is a `config.toml` table on
the target, read at startup and refused if unknown:

| Table | Chooses |
| --- | --- |
| `[agents]` | the implementer, reviewer and adjudicator commands |
| `[worktree]` | setup commands run in each fresh worktree and their cap |
| `[supervisor]` | stale threshold, strikes, time-box grace, review-overlap threshold, sweep interval, restart grace |
| `[loop]` | stop on failure; claim order by identifier or priority |
| `[board]` | the Linear project and team this target claims from |
| `[report]` | the host label rendered instead of the machine name |

[Config](../config.md) has each with a commented example.

## What a port would replace

The store schema and the ticket template are the cross-language contracts.
A Rust daemon replaces `serve.py` against the same store; a Rust verify
gate replaces `gates.py` with the same clause-by-clause report; the Python
test suite run against the other binary is the acceptance oracle. That
ordering is the roadmap's, and it is why the seams came first.

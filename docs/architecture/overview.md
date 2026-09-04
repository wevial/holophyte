# How it links together

Everything in Holophyte is one of four kinds of thing: a **process** that
does work, a **store** that remembers it, a **boundary** that other code is
kept behind, or a **projection** rendered from the store for someone to
read. The store is the only thing every other part agrees on.

```mermaid
flowchart TB
  subgraph board[Linear · the board]
    L[project: ready tickets]
  end
  subgraph writer[Writer host]
    direction TB
    F[loop]
    SUP[supervisor]
    D[serve daemon]
    ST[(store.db)]
    subgraph run[one run]
      direction LR
      WT[worktree] --> IMP[implementer] --> VG[verify gate] --> RR[reviewer] --> M[merge --no-ff]
    end
    FD[FINDINGS.md]
  end
  subgraph seat[Operator seat]
    DR[drawer]
    OP[operator]
  end
  L --> F
  F --> run
  F <--> ST
  SUP <--> ST
  D --> ST
  M --> ST
  ST --> FD
  ST -. status, ledger .-> L
  DR -- /status /runs --> D
  OP -- ssh, git push --> writer
  OP -- file-ticket --> L
```

Solid arrows carry work. Dotted arrows are projections: the store is
written by the loop and the supervisor only, and Linear, `FINDINGS.md`, the
daemon's JSON and the drawer are all views of it.

## The four kinds, named

### Processes

| Process | Started by | Reads | Writes | Ends when |
| --- | --- | --- | --- | --- |
| **loop** (`factory.py TARGET`) | operator, in tmux | Linear, store, config | store, worktrees, `main`, Linear, `FINDINGS.md` | queue empty, a failed run, or after a self-merge (re-execs) |
| **supervisor** (`--supervise`) | operator, in tmux | store | store (strikes, releases, heartbeats) | SIGTERM; re-execs itself when the factory checkout's HEAD moves |
| **serve daemon** (`--serve`) | systemd user unit | store, read-only | nothing | never; restart to pick up new code |
| **implementer** | the loop, per run | the worktree, the ticket body | the worktree | budget or commit |
| **reviewer** | the loop, per round | a staged, read-only export of the candidate | its verdict text | verdict or timeout |
| **drawer** | SwiftBar, every 10 s | the daemons | nothing | never |

[Processes](processes.md) describes each in detail, including how they
restart.

### The store

One SQLite file per target, WAL mode, ten tables: projects, tickets, runs,
review rounds, run events, sweep strikes, supervisor heartbeats, loop
restarts, Linear deliveries, interventions. Two state machines live in it
as data (`TICKET_TRANSITIONS`, `RUN_PHASE_TRANSITIONS`) and every
transition goes through one function that refuses edges not in the table.
Typed read views in `store/read.py` are the only SQL the rest of the code
sees. [Store and state](data.md) has the tables and the diagrams.

### Boundaries

- **The ticket body** is frozen at claim time. The merge gate re-reads it
  and refuses to merge if it drifted.
- **The worktree** is where the implementer works; the main checkout is
  never touched until the merge gate passes.
- **The verify gate** is the ticket's own command, run clause by clause,
  before every review and again before the merge.
- **The review container** sees a clean export of the candidate commit,
  read-only, with no credentials and no host home. It can witness only
  what is in that tree. See [Reviewing](../reviewing.md).
- **The bind address** is the daemon's entire access control: it listens
  on the tailnet address and nowhere else.

### Projections

- **Linear state** is pushed from the store, one way, last write wins,
  never read back for status. The ticket body is read back, once, at
  claim and again at merge.
- **`FINDINGS.md`** is the newest twenty-five entries below a marker,
  regenerated from `runs` and `reviewRounds` at every close-out. Nobody
  edits it.
- **`/status`, `/runs`, `/attention`** are the store as JSON, one
  read-only connection per request.
- **The drawer** is those endpoints as a menu.

## What talks to what, and over which channel

| From | To | Channel | Direction |
| --- | --- | --- | --- |
| loop | Linear | HTTPS GraphQL, `LINEAR_API_KEY` | outbound |
| loop | implementer | subprocess in its own process group | local |
| loop | reviewer | `docker run`, staged repo mounted read-only; Codex reaches its backend outbound from inside | local + outbound |
| loop | origin | nothing. The factory never pushes; the operator does | none |
| supervisor | loop | only through the store: strikes, releases, `loopRestarts` | local |
| daemon | drawer | HTTP on the tailnet address | inbound, tailnet only |
| operator | writer host | ssh over the tailnet | inbound, tailnet only |
| operator | Linear | `--file-ticket`, run on the writer host against the target's `[board]` | outbound |

## Why it is shaped this way

Every gate is the fossil of a failure. The
[roadmap's failure lineage](../roadmap.md#failure-lineage-why-each-gate-exists)
lists the wreck each one bought, from empty verify output to mid-run
goalpost edits. The standing decisions that keep the shape small are on the
same page: no mechanism without a demonstrated consumer, pluggable by shape
rather than by system, tickets as atoms, every failure becomes a mechanical
gate, and the factory never pushes.

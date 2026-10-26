# Holophyte

A minimal software factory. Tickets live on a board, either a native board
kept in the factory's own store or a Linear project; `main` is the only
integration point. Each claimed ticket is implemented in an isolated sibling
worktree by an implementer agent, held to the ticket's own verify command,
and reviewed by an independent reviewer, by default inside a hardened
container. When the verify gate and the review both pass, the candidate lands
by a local `--no-ff` merge or, under `[merge] mode = "pr"`, through a pull
request the babysitter watches through its checks and review threads;
`[merge] approve = "human"` parks it for an operator either way. Python and
SQLite, with two pinned runtime dependencies.

A ticket can belong to a [story](holophyte/tickets/templates/storyTemplate.md) ([its config](docs/config.md#story)), and where
`[merge] ui_paths` is set one declaring a major UI change needs an approved mock-up
([The shape](docs/operating/tickets.md#the-shape)). A run can pass a [trim step](docs/config.md#trim)
before review, have a failure's cause [triaged](docs/config.md#questionsfailures)
and answer bare pull request mentions as [typed questions](docs/config.md#questions). The [console and
host daemon](docs/operating.md#serving) serve it and the [host sweep](docs/operating.md#the-host-sweep) supervises it.

## Install

Python 3.11+ and Git on the host, and Docker for the reviewer container and,
under `[agents] implementer_isolation = "container"`, the implementer's turns.
A Linear board, and only a Linear board, needs `LINEAR_API_KEY` in the
environment or a `.env` at the checkout root (beside `factory.py`).
`ruff` is the one developer tool (`pip install --user ruff`). `tomlkit` and `mcp` are the runtime dependencies, pinned in
`requirements.txt` (`python3 -m pip install --user -r requirements.txt`);
the daemon needs `tomlkit` to edit a config in place and exits naming it
when it is missing, and only `holo mcp` needs `mcp`.
`python3 -m pip install --user -e .` in the checkout installs
the `holo` command, which imports from the checkout and so follows its
`HEAD`. Bun is needed
only to build the console (`bun --cwd=console run build`); see
[Development](docs/development.md). Per-project settings go in
`~/.holophyte/<slug>/config.toml`; see [Config](docs/config.md).

## Usage

```
holo status [--json]                             # what the factory is doing: one project, or every project on the host
holo patrol [--json]                             # what needs the operator: attention, stranded, quiet and repeat-failing tickets
holo start ["note"] -p NAME|PATH                 # start the project's loop unit and return; the note releases a hold
holo stop [--now] "note" -p NAME|PATH            # hold the project: the loop ends after its live runs; --now aborts them
holo board [--json] -p NAME|PATH                 # the board's columns
holo draft [KEY] -p NAME|PATH                    # a ticket skeleton to fill, or KEY's stored body to edit
holo ticket check TICKET.md [--repo R]           # check a draft as python3 ticket_template.py does
holo file TICKET.md [--backlog] -p NAME|PATH     # file a ticket after checking it; --update KEY replaces a body
holo steer KEY "note" -p NAME|PATH               # amend a ticket, or steer its live run
holo requeue KEY "note" -p NAME|PATH             # put a failed run's ticket back in the queue
holo scout "ASK" --kind bug|research|recon|guardrails -p NAME|PATH # launch a scout on an ask
holo scout page ID -p NAME|PATH                  # a done scout's report as a Lotuspod page, with its decisions
holo serve [ADDR] [-p NAME|PATH]                 # the JSON daemon and the console
```

The [CLI reference](docs/reference/cli.md) has every `factory.py` mode and
the other `holo` commands.

## Read next

- [The loop](docs/loop.md) — what one run does, and the ticket and run
  state machines.
- [Operating](docs/operating.md) — supervising, serving, the operator
  commands.
- [Config](docs/config.md) — every `config.toml` table with a commented
  example.
- [Reviewing](docs/reviewing.md) — the local reviewer boundary.
- [Development](docs/development.md) — the package map, tests and linting.

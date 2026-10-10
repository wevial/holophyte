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

A ticket can belong to a [story](storyTemplate.md) ([its config](docs/config.md#story)), and where
`[merge] ui_paths` is set one declaring a major UI change needs an approved mock-up
([The shape](docs/operating/tickets.md#the-shape)). A run can pass a [trim step](docs/config.md#trim)
before review, have a failure's cause [triaged](docs/config.md#questionsfailures)
and answer bare pull request mentions as [typed questions](docs/config.md#questions). The [console and
host daemon](docs/operating.md#serving) serve it and the [host sweep](docs/operating.md#the-host-sweep) supervises it.

## Install

Python 3.11+ and Git on the host, and Docker for the reviewer container and,
under `[agents] implementer_isolation = "container"`, the implementer's turns.
A Linear board, and only a Linear board, needs `LINEAR_API_KEY` in the
environment or a `.env` beside `linear_provider.py`.
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
python3 factory.py /path/to/repo                  # run the loop
python3 factory.py --report /path/to/repo         # estimate-vs-actual table
python3 factory.py /path/to/repo --hold --note TEXT # stop new admission; existing runs continue
python3 factory.py /path/to/repo --release-hold --note TEXT # enable admission again
python3 factory.py --sweep [--act] /path/to/repo  # tripped runs; --act fails them
python3 factory.py /path/to/repo --board-diff    # where the store's ready queue differs from the board's; writes nothing
python3 factory.py /path/to/repo --status [--json] # projects, live and parked runs, ready count, locks
python3 factory.py --import-store PATH --dry-run /path/to/repo # what importing another store would move; writes nothing
python3 factory.py --board-import [--dry-run] /path/to/repo # copy every open Linear issue into the store; --dry-run writes nothing
python3 factory.py --supervise /path/to/repo      # the acting sweep on a timer (optional: the loop starts one)
python3 factory.py --supervise [--once]          # the host sweep over every project in host.toml; --once is one run
python3 factory.py --serve 7710 /path/to/repo         # JSON daemon on loopback, the console at /; reads, and writes only with [serve] actions or config_edit; HOST:PORT to bind elsewhere
python3 factory.py --serve [HOST:PORT]          # the host daemon: every project in host.toml under /projects/NAME, socket-activated under systemd
python3 factory.py --status [--json]            # every project in host.toml, the last host sweep, the locks
python3 factory.py --requeue KO-n --note TEXT /path/to/repo   # back in the queue
python3 factory.py --approve KO-n [--note TEXT] /path/to/repo  # release a run parked for merge approval
python3 factory.py --approve KO-n --force --note TEXT /path/to/repo # ...though its pull request is not ready to merge
python3 factory.py --babysit KO-n [--note TEXT [--author NAME]] /path/to/repo # look at a parked run's pull request again
python3 factory.py /path/to/repo --pause KO-n --note TEXT # stop at the next safe point
python3 factory.py /path/to/repo --resume KO-n --note TEXT # continue a paused run
python3 factory.py /path/to/repo --abort KO-n --note TEXT # end a run now, preserving its work
python3 factory.py /path/to/repo --abort KO-n --close-pr --note TEXT # ...and close its pull request
python3 factory.py --repoint KO-n SHA --note TEXT /path/to/repo # move a parked candidate to a rebuilt branch tip
python3 factory.py /path/to/repo --close KO-n --landed URL [--note TEXT] # record a change landed outside the factory
python3 factory.py /path/to/repo --gap-layer KEY-n LAYER --note TEXT [--carried-by KEY-n] [--found-by witness|operator] # where a gap's lesson landed: impossible, static, witness, guidance, review or none; who found it
python3 factory.py --file-ticket TICKET.md [--state Todo|Backlog] [--priority urgent|high|medium|low] [--note TEXT [--author NAME]] /path/to/repo
python3 factory.py --file-ticket TICKET.md --update KO-n /path/to/repo   # replace the body
python3 factory.py --file-ticket TICKET.md --update KEY-n --revision N [--priority urgent|high|medium|low] [--labels a,b] /path/to/repo # a native board's edit
python3 factory.py --file-story SLUG [--priority urgent|high|medium|low] /path/to/repo # a native or store-mode Linear board's story, filed from stories/SLUG
python3 factory.py --file-story SLUG --update KEY-n --revision N [--priority urgent|high|medium|low] /path/to/repo # apply a redrafted stories/SLUG to a filed story
python3 factory.py --approve-story KEY-n --revision N --note TEXT [--baseline-green W] [--baseline-red-kind exception W] /path/to/repo # approve a planned story on its red baseline and release its children
python3 factory.py --witness-pass KEY-n /path/to/repo # run an open story's witnesses at main's tip and print each verdict
python3 factory.py --decide KEY-n ID [OPTION] --note TEXT /path/to/repo # answer a parked story's decision ID with option OPTION (default: the default) and apply it
python3 factory.py --move KEY-n ready|backlog --revision N [--note TEXT] /path/to/repo # a native ticket to Ready or Backlog
python3 factory.py --cancel KEY-n --revision N --note TEXT /path/to/repo # cancel a native ticket; a live run ends abandoned
python3 factory.py --worker /path/to/repo         # internal: one worker of the pool [loop] workers > 1 spawns
python3 factory.py --shadow BRIEF /path/to/repo   # internal: the shadow implementer a fresh claim spawns under [agents.implementer_shadow]
python3 factory.py project add|remove|list|enable|hold|disable [--store PATH] # register projects and change their admission
holo --version                                   # the package version and the checkout's short HEAD
holo status [--json]                             # one project when -p, HOLO_PROJECT, the current repository or default_project names it, else the host; every factory.py mode but the internal --worker and --shadow has a holo command
holo status --watch [SECONDS]                    # the status page redrawn every SECONDS (default 5) until Ctrl-C
holo follow [--since AGO] [--every SECONDS] [--json] -p NAME|PATH # one line per run event and ledger entry as it is written, and one when a heartbeat goes stale
holo mcp                                         # an MCP server on stdio: tools for the holo reads and five signed writes (file_ticket, send_back, babysit, requeue, hold); see docs/reference/cli.md#holo
holo mcp --http [HOST:PORT]                      # the same tools at POST /mcp when [serve] actions = true, else the reads only; behind the host's machine token, 127.0.0.1:7711 by default; see docs/reference/http.md#post-mcp
holo requeue KEY "note" -p NAME|PATH             # factory.py --requeue KEY --note "note" PATH; -p takes a [serve] name or a path, and without it the project is found as for status
holo ticket requeue KEY "note" -p NAME|PATH      # the same: ticket VERB is an alias of each ticket verb
holo approve KEY ["note"] -p NAME|PATH           # each ticket verb takes the factory mode's arguments, the note last; see docs/reference/cli.md#holo
holo hold "note" -p NAME|PATH                    # and holo release "note"
holo send-back RUN "note" -p NAME|PATH           # the console's send-back of run RUN; no factory.py equivalent
holo steer KEY "note" [--hint] [--author NAME] -p NAME|PATH # amend a ticket, or a live run before its pull request at its next implementer turn (--hint: advice for its next implement turn alone), or send its run parked on a pull request back; no factory.py equivalent
holo start ["note"] [--foreground] -p NAME|PATH  # start the project's loop unit and return; the note releases a hold
holo stop [--now] "note" -p NAME|PATH            # hold the project: the loop ends after its live runs; --now aborts them
holo sweep [--act] | board diff | board import -p NAME|PATH
holo store import PATH --dry-run -p NAME|PATH     # factory.py --import-store PATH --dry-run
holo report [--since WINDOW] [--notes] [--json] -p NAME|PATH # the window's counts, read from the store
holo file TICKET.md [--backlog] [--priority P] ["note" [--author NAME]] -p NAME|PATH # factory.py --file-ticket; --update KEY [--revision N] replaces the body
holo move KEY ready|backlog --revision N ["note"] -p NAME|PATH # factory.py --move
holo cancel KEY --revision N "note" -p NAME|PATH  # factory.py --cancel
holo babysit KEY ["note" [--author NAME]] -p NAME|PATH # factory.py --babysit
holo repoint KEY SHA "note" -p NAME|PATH         # factory.py --repoint
holo pause KEY "note" -p NAME|PATH               # factory.py --pause
holo resume KEY "note" -p NAME|PATH              # factory.py --resume
holo abort KEY "note" [--close-pr] -p NAME|PATH  # factory.py --abort
holo close KEY URL ["note"] -p NAME|PATH         # factory.py --close KEY --landed URL
holo gap KEY LAYER "note" [--carried-by KEY] [--found-by F] -p NAME|PATH # factory.py --gap-layer
holo loop start|stop ...                         # the same as holo start and holo stop
holo story file|approve|witness|decide ... -p NAME|PATH # the story modes
holo supervise [--once]                          # the host form when no source names a project; --once is one run
holo supervise -p NAME|PATH                      # the project form, without --once
holo serve [ADDR] [-p NAME|PATH]                 # likewise the host daemon
holo project add|remove|list|enable|hold|disable   # factory.py project, unchanged
holo completion bash|zsh|fish                     # the shell's completion script; see docs/reference/cli.md#holo
holo runs [--limit N] [--json] -p NAME|PATH      # recent runs; --json prints GET /runs's body
holo run N [--files|--ledger|--turns] [--json] -p NAME|PATH # one run's detail, files, ledger or turns, as GET /runs/N[/files|/ledger|/turns]
holo attention [--json] [-p NAME|PATH]           # what waits on the operator, as GET /attention; the host's when no project is named
holo board [--json] | ticket KEY [--json] -p NAME|PATH # the board's columns or one ticket's detail, as GET /board and /tickets/KEY
```

`--file-ticket` validates the file against the project, creates the issue,
reads the stored body back and validates that again, so a transfer that
rewrites the body is caught. With `--update KO-n` it replaces that issue's
title, description and estimate from the file instead of creating one, with
the same validation on both sides. It adds the blockers the file names and
the board does not yet hold, and leaves in place one the board holds and the
file no longer names; state and priority stay as they are, so `--state` and
`--priority` are refused beside it. On a native board `--update KEY-n`
requires `--revision N`, the revision the ticket was read at, and takes
`--priority` and `--labels`; a ticket that moved past `N` is left unchanged
and its current revision printed. It prints `[holo2] updated KO-n: TITLE`,
naming any blocker it added with a `+`, or exits 1 with the problem and nothing
changed when the file is invalid and 2 with the identifier and the problem
when the stored body is.

A native board's column is a person's: `--move KEY-n ready|backlog` and
`--cancel KEY-n --note TEXT` act at `--revision N` as `--update` does, and
a live run continues through a move but is aborted by a cancel, ending
`abandoned` at its next safe point. Both are usage errors on a Linear
project, whose tickets are moved in Linear.

`--report`, `--status`, `--sweep` and `--import-store --dry-run` read the
store and call nobody; `--serve` reads it too, and writes only through its
two opt-ins, `[serve] actions` and `config_edit`
([The daemon's actions](docs/reference/daemon.md)); the loop and
`--supervise` need a `[board]` table. `--help` is safe: the command line is parsed, not indexed.

## Read next

- [The loop](docs/loop.md) — what one run does, and the ticket and run
  state machines.
- [Operating](docs/operating.md) — supervising, serving, the operator
  commands.
- [Config](docs/config.md) — every `config.toml` table with a commented
  example.
- [Reviewing](docs/reviewing.md) — the local reviewer boundary.
- [Development](docs/development.md) — the package map, tests and linting.
- [Roadmap](docs/roadmap.md) — phases and standing decisions.

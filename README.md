# Holophyte

A minimal, Linear-driven software factory. Tickets live in a Linear
project; `main` is the only integration point. Each claimed ticket is
implemented in an isolated sibling worktree by an implementer agent, held to
the ticket's own verify command, reviewed by an independent reviewer inside a
hardened container, and merged only when the verify gate and the review both
pass. Stdlib Python and SQLite; no frameworks.

## Install

Python 3.11+ and Git on the host, Docker for the reviewer container, and
`LINEAR_API_KEY` in the environment or a `.env` beside `linear_provider.py`.
`ruff` is the one developer tool (`pip install --user ruff`). Per-target
settings go in `~/.holophyte/<slug>/config.toml`; see [Config](docs/config.md).

## Usage

```
python3 factory.py /path/to/repo                  # run the loop
python3 factory.py --report /path/to/repo         # estimate-vs-actual table
python3 factory.py --sweep [--act] /path/to/repo  # tripped runs; --act fails them
python3 factory.py --supervise /path/to/repo      # the acting sweep on a timer (optional: the loop starts one)
python3 factory.py --serve HOST:PORT /path/to/repo   # read-only JSON daemon
python3 factory.py --requeue KO-n --note TEXT /path/to/repo   # back in the queue
python3 factory.py --file-ticket TICKET.md [--state Todo|Backlog] [--priority urgent|high|medium|low] /path/to/repo
python3 factory.py --file-ticket TICKET.md --update KO-n /path/to/repo   # replace the body
```

`--file-ticket` validates the file against the target, creates the issue,
reads the stored body back and validates that again, so a transfer that
rewrites the body is caught. With `--update KO-n` it replaces that issue's
title, description and estimate from the file instead of creating one, with
the same validation on both sides; state, priority and relations are left as
they are, so `--state` and `--priority` are refused beside it. It prints
`[holo2] updated KO-n: TITLE`, or exits 1 with the problem and nothing
changed when the file is invalid and 2 with the identifier and the problem
when the stored body is.

`--report`, `--sweep` and `--serve` read the store and call nobody; the loop
and `--supervise` need a `[board]` table. `--help` is safe: the command line
is parsed, not indexed.

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

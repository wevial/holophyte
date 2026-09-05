# Holophyte

A minimal, Linear-driven software factory. A ticket goes in as a frozen
contract; a merged commit on `main` comes out, or a preserved branch and a
ledger entry saying exactly why not. Stdlib Python and SQLite, no
frameworks, and every step is a row in a store that the rest of the system
is rendered from.

This site is the manual. It is organised by the question you are asking:

<div class="grid cards" markdown>

-   **How does it all link together?**

    ---

    One page, one diagram, every component and what it talks to.

    [:octicons-arrow-right-24: Architecture overview](architecture/overview.md)

-   **What happens to one ticket?**

    ---

    From a markdown file on the operator's machine to a `--no-ff` merge, step by
    step, naming the module, the table and the process at each step.

    [:octicons-arrow-right-24: Lifecycle of a ticket](architecture/lifecycle.md)

-   **Something is stuck. What do I do?**

    ---

    The escalation ladder, the operator commands, and the recovery recipes
    written the day each incident happened.

    [:octicons-arrow-right-24: Runbook](operating/runbook.md)

-   **How do I write a ticket the loop will take?**

    ---

    The template, the validator, what the reviewer can and cannot witness,
    and the contract defects that cost runs.

    [:octicons-arrow-right-24: Tickets as contracts](operating/tickets.md)

</div>

## The thesis in one paragraph

Holophyte turns a Linear ticket into a merged commit without a human in the
loop. One process claims the next ready ticket, cuts a git worktree, hands
the whole ticket body to an implementer agent, runs the ticket's own verify
commands, sends the diff and the ticket to an independent reviewer inside a
hardened container, gives the implementer at most two fix rounds, asks a
terminal adjudicator for a PASS or FAIL, and merges with `--no-ff`. A
separate supervisor sweeps the store for runs that died and frees their
leases. A read-only daemon serves the store's state as JSON, and a
menu-bar drawer answers "what should I look at next?" from it. All of it
runs on one machine; a second machine is optional. The factory dogfoods itself: it is the target most of its own
tickets run against, and it re-executes itself after merging its own code.

## Where things are

| Surface | Lives in | Read more |
| --- | --- | --- |
| The loop, supervisor, daemon, operator commands | `holophyte/` package, `factory.py` entry point | [Processes](architecture/processes.md) |
| Durable state | one SQLite file per target under `~/.holophyte/<slug>/` | [Store and state](architecture/data.md) |
| The board | a Linear project, one-way mirror of the store | [Tickets as contracts](operating/tickets.md) |
| The review boundary | a read-only Docker container running Codex | [Reviewing](reviewing.md) |
| Machines | one, by default; a second for the drawer or the operator is a page of its own | [Across machines](operating/hosts.md) |
| Evidence | `FINDINGS.md`, rendered from the store at every close-out | [Store and state](architecture/data.md#findings) |

## Install and run

Python 3.11+ and Git on the host, Docker for the reviewer container,
`LINEAR_API_KEY` in the environment or a `.env` beside
`linear_provider.py`, and `ruff` as the one developer tool. Per-target
settings go in `~/.holophyte/<slug>/config.toml`.

```
python3 factory.py /path/to/repo                  # run the loop
python3 factory.py --supervise /path/to/repo      # keep it honest
python3 factory.py --serve HOST:PORT /path/to/repo   # read-only JSON daemon
```

The full mode list is in the [CLI reference](reference/cli.md); every
`config.toml` table is in [Config](config.md).

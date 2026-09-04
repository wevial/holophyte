# CLI

`python3 factory.py [MODE] TARGET`. The command line is parsed, not
indexed, so `--help` is safe. Modes are mutually exclusive; the target is
always the repository path.

| Invocation | Does | Touches |
| --- | --- | --- |
| `factory.py TARGET` | runs the loop: claim, work, verify, review, merge, repeat; exits on an empty board or a failed run; re-execs after a self-merge | Linear, store, worktrees, `main`, `FINDINGS.md` |
| `--report TARGET` | prints the estimate-vs-actual table and the supervisor liveness line | store, read-only |
| `--sweep TARGET` | prints what an acting sweep would do; lists stray review containers | store (sightings only) |
| `--sweep --act TARGET` | fails tripped runs, releases their leases, removes stray containers | store, Docker |
| `--supervise TARGET` | the acting sweep every `sweep_interval_sec`, under the target's supervisor lock; re-execs itself when the factory code moves | store |
| `--serve HOST:PORT TARGET` | the read-only JSON daemon | store, read-only |
| `--requeue KO-n --note TEXT TARGET` | walks a failed ticket back to `ready` with an `interventions` row | store |
| `--file-ticket TICKET.md [--state Todo\|Backlog] [--priority urgent\|high\|medium\|low] TARGET` | validates, creates the issue in the target's `[board]` project, reads it back, validates again | Linear |
| `--file-ticket TICKET.md --update KO-n TARGET` | same, replacing an existing issue's title, body and estimate | Linear |

## Startup checks

Every mode validates every `config.toml` table it can see and refuses an
unknown key. The loop, `--supervise`, `--requeue` and `--file-ticket` need
a `[board]` table. The loop additionally live-probes each configured agent
route and the reviewer image before claiming, and runs a read-only sweep
whose output it prints.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | done, or the board was empty |
| 1 | a startup refusal, a failed run under `stop_on_failure`, an invalid ticket file, a refused requeue |
| 2 | `--file-ticket`: the issue exists but its stored body failed re-validation; argparse errors |

## Output prefix

Every line the factory prints begins `[holo2]`; the verify gate's report
lines begin `[verify]`. The loop's tmux log is the operator's first source
after the store.

## Environment

| Variable | Read by | Purpose |
| --- | --- | --- |
| `HOLOPHYTE_HOME` | `Target` | the state root, default `~/.holophyte`; tests point it at a temp dir |
| `LINEAR_API_KEY` | `linear_provider` | the board's API key; env or `.env` beside the module |
| `HOLOPHYTE_TARGET`, `HOLOPHYTE_SERVE_ADDRESS`, `HOLOPHYTE_SERVE_PORT` | the serve unit | one daemon instance's target, bind address, port |

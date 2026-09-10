# CLI

`python3 factory.py [MODE] TARGET`. The command line is parsed, not
indexed, so `--help` is safe. Modes are mutually exclusive; the target is
always the repository path.

| Invocation | Does | Touches |
| --- | --- | --- |
| `factory.py TARGET` | runs the loop: claim, work, verify, review, merge, repeat; exits on an empty board or a failed run; re-execs after a self-merge. Under `[loop] workers > 1` it is the scheduler of a pool of `--worker` children instead (see [The loop](../loop.md#the-pool)) | Linear, store, worktrees, `main`, `FINDINGS.md` |
| `--report TARGET` | prints the estimate-vs-actual table and the supervisor liveness line | store, read-only |
| `--sweep TARGET` | prints what an acting sweep would do; lists stray review containers | store (sightings only) |
| `--sweep --act TARGET` | fails tripped runs, releases their leases, removes stray containers | store, Docker |
| `--supervise TARGET` | the acting sweep every `sweep_interval_sec`, under the target's supervisor lock; re-execs itself when the factory code moves | store |
| `--serve PORT TARGET` | the read-only JSON daemon on loopback (`--serve 7710` binds `127.0.0.1:7710`), which also serves the console at `/` from the built bundle; `--serve HOST:PORT` binds the named address instead, and a non-loopback bind demands `[serve] token_file`, whose contents every JSON request but `/peers` must present as a bearer token (`/`, the console's files and `/peers` stay open; a loopback bind, `127.0.0.1:PORT` included, ignores the key) | store, read-only |
| `--requeue KO-n --note TEXT TARGET` | walks a failed ticket back to `ready` with an `interventions` row | store |
| `--approve KO-n [--note TEXT] TARGET` | releases a ticket parked by `[merge] approve = "human"`: an `interventions` row with action `approve`, the parked run ended with its resume point at the merge gate, the ticket walked to `ready`; the loop's next claim reuses the preserved worktree and branch, re-runs the pre-merge verify and merges with no implementer or reviewer -- under `[merge] mode = "pr"`, shepherds the pull request once more and merges it through the API when green and quiet; refuses any other state, naming it | store |
| `--shepherd KO-n [--note TEXT] TARGET` | sends a ticket parked on its pull request (`[merge] mode = "pr"`) back to the shepherd: an `interventions` row with action `shepherd`, the parked run ended with its resume point at the merge gate, the ticket walked to `ready`; the loop's next claim resumes the candidate on the PR and reads its threads and checks again, parking again under `approve = "human"` rather than merging; refuses any other state, naming it | store |
| `--repoint KO-n SHA --note TEXT TARGET` | moves a parked candidate to a rebuilt branch tip: an `interventions` row with action `repoint` carrying the note, a `runEvents` row naming the old and new shas, then `runs.candidateSha` set to `SHA` (a full 40-hex commit id); the run stays parked and the branch is not touched; the merge gate `--approve` resumes into holds the branch to the new sha; refuses a ticket not parked awaiting merge approval, one already approved (its release is in flight: requeue instead) or a malformed sha, naming it | store |
| `--file-ticket TICKET.md [--state Todo\|Backlog] [--priority urgent\|high\|medium\|low] TARGET` | validates, creates the issue in the target's `[board]` project, reads it back, validates again | Linear |
| `--worker TARGET` | internal, spawned by the scheduler under `[loop] workers > 1`: claims one ticket, works it to merge or park, exits with the run's status (0 merged, 1 failed, 2 parked, 3 nothing to claim, 4 stopped for a human); skips the startup probes, the sweep and the supervisor spawn, which the scheduler ran for the pool | Linear, store, worktrees, `main`, `FINDINGS.md` |
| `--file-ticket TICKET.md --update KO-n TARGET` | same, replacing an existing issue's title, body and estimate; a blocker the file's `Depends on:` names and the board lacks is recorded and printed as `+KO-a`, one the board holds and the file no longer names is printed as `board also holds KO-c` -- relations are added, never removed | Linear |

## Startup checks

Every mode validates every `config.toml` table it can see and refuses an
unknown key. The loop, `--supervise`, `--requeue`, `--approve`,
`--shepherd` and `--file-ticket` need a `[board]` table. The loop additionally live-probes each configured agent
route and the reviewer image before claiming, and runs a read-only sweep
whose output it prints.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | done, or the board was empty |
| 1 | a startup refusal, a failed run under `stop_on_failure`, an invalid ticket file, a refused requeue, approval, shepherd or re-point |
| 2 | `--file-ticket`: the issue exists but its stored body failed re-validation; argparse errors |

## Output prefix

Every line the factory prints begins `[holo2]`; the verify gate's report
lines begin `[verify]`. A worker of the pool prints `[holo2 wN]` instead,
`N` its slot number, so the lines of a pool sharing one log tell apart. The loop's tmux log is the operator's first source
after the store.

## Environment

| Variable | Read by | Purpose |
| --- | --- | --- |
| `HOLOPHYTE_HOME` | `Target` | the state root, default `~/.holophyte`; tests point it at a temp dir |
| `LINEAR_API_KEY` | `linear_provider` | the board's API key; env or `.env` beside the module |
| `HOLOPHYTE_TARGET`, `HOLOPHYTE_SERVE_ADDRESS`, `HOLOPHYTE_SERVE_PORT` | the serve unit | one daemon instance's target, bind address, port |

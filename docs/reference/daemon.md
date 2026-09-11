# The daemon's actions

`--serve` is a read daemon: every `GET` route in [HTTP endpoints](http.md)
opens the store read-only and closes it. Two opt-ins make it also an
operator's hand on the writer host: `actions`, below, and `config_edit`,
[the target's configuration](#the-targets-configuration-get-config-and-put-config).
With

```toml
[serve]
actions = true
name = "holophyte"
```

the daemon answers three `POST` routes under `/actions/`, each mapped to a
step the operator ladder already allows: restarting the supervisor unit,
starting the loop unit, and `requeue` in the store. Without `actions =
true` every `/actions/` path is 404, token or not, and the daemon writes
nothing. The routes are behind the same `Authorization: Bearer` token as
the JSON routes ([Authentication](http.md#authentication)) on every bind,
loopback included: the bind address guards reads, not a hand on the
units. So `actions = true` needs `[serve] token_file` whatever the bind,
and a daemon asked to bind without one exits naming the key. Without the
exact token the routes answer 401 before anything runs or is written.

The reply is always the same shape:

```json
{"action": "restart-supervisor", "ok": true, "detail": "systemctl --user restart holophyte-supervise@holophyte exited 0"}
```

`ok` says whether the step did what was asked; `detail` says what
happened. A step that fails is `ok: false` with the reason in `detail` and
still status 200: the operator asked for a thing and is told the answer,
which is not a server error. Each action is an interventions row
(`store.record_intervention()`, the operator ladder's record-before-acting
call) written before the action runs; an action that cannot be recorded
does not run.

## `POST /actions/restart-supervisor`

Runs `systemctl --user restart holophyte-supervise@NAME`, where `NAME` is
`[serve] name`: the instance the deploy unit templates are enabled under
(the target slug, see [Supervising and serving](../operating.md)), the
target directory's name when the key is absent. `systemctl` gets 20 s to
answer. A non-zero exit is `ok: false` with its stderr in `detail`; an
absent `systemctl` or one that outlives the cap is `ok: false` saying so.
The reply also carries `unit`, the instance addressed, and `recorded`, the
run the intervention landed on (below).

The record is a human `restart_supervisor` intervention on the store's
newest run, its narrative naming the unit and the route: interventions are
keyed by run, and a supervisor restart is about the runs it watches over.
A target with no store, or a store with no run yet, has nothing to record
against and the unit is left alone: `ok: false` saying so, `recorded`
null, `systemctl` not called. No body is read.

## `POST /actions/launch-loop`

Runs `systemctl --user start holophyte-loop@NAME`: one pass of the loop as
the deploy template defines it, inactive again once the queue is down.
Everything else is as `restart-supervisor`, the intervention a
`launch_loop` row.

## `POST /actions/requeue`

Body: a JSON object with `ticket` (required, the Linear identifier, `KO-n`)
and `note` (optional, why the ticket goes back in the queue; a fixed note
saying it came from the console when absent). The daemon does exactly what
`factory.py TARGET --requeue KO-n --note TEXT` does: the store's one
`requeue` transaction, a `requeue` interventions row carrying the note on
the failed run and the ticket walked to `ready`. The reply carries
`ticket` and, on success, `run`, the failed run it was requeued after.

A `ticket` the store never mirrored, or one the store refuses to requeue
(a live run, a ticket not `in_flight`, a last run that did not fail), is
200 with `ok: false` and the refusal in `detail`; nothing is written. A
body that is not a JSON object, or one with no `ticket`, is 400 naming it.
A target with no store is 503.

## The target's configuration: `GET /config` and `PUT /config`

A second opt-in, separate from `actions`:

```toml
[serve]
config_edit = true
```

opens the target's own `config.toml` -- the file the loop reads at
startup, [Configuration](../config.md) -- to the console, behind the same
bearer token on every bind, loopback included, so `config_edit = true`
needs `[serve] token_file` as `actions` does and a bind without it exits
naming the keys. Without `config_edit = true` both routes are 404, token
or not. It is off by default because the file is command execution on the
writer host: `[worktree] setup` and `[agents]` name programs the next loop
start runs, so a client that can write the file can run what it likes as
the loop's user.

`GET /config` answers

```json
{"text": "[serve]\ntoken_file = \"...\"\n...", "values": {"serve": {"token_file": "..."}, "loop": {"workers": 2}}, "path": "/home/.../config.toml", "applies": "next loop start"}
```

`text` is the file as written, except that the value of every key whose
name ends in `token` or `key` (`api_key`, `token`, `"api key"`) is
replaced by `"[redacted]"`; `token_file`, a path, stays. The key may be
bare, quoted or dotted, under a `[table]` or `[[array]]` header or inside
an inline table, and the value is replaced whole whatever its shape --
a multi-line string, an array, an inline table -- with a comment beside
it left in place. A table whose own name ends so (`[extra.api_key]`,
`api_key.value = ...`, `api_key = { ... }`) is a secret whole: every
value under it is replaced, whichever way the table is written. The daemon checks its own work against the parsed
document and answers 500 rather than serve a text in which a secret is
still readable. A target with no file yet has `text` `""`. `values` is the same redacted
text parsed with `tomllib`, as JSON: a quoted table name or a triple-quoted
string is an ordinary key or value here, so a client reads settings from
`values` and never parses TOML itself; null when the text does not parse. `applies` says when a change takes effect: the loop reads
the file once at startup, so a written change waits for the next start
(`POST /actions/launch-loop`, or the supervisor's), and a running loop is
not touched.

`PUT /config` takes `{"text": "..."}`, the whole new file. Every
`"[redacted]"` value in it -- any TOML string reading `[redacted]`,
however quoted, a comment beside it or not -- is replaced by the current
file's value for the same key in the same table before anything else, so
a round trip through the page never blanks a secret; a `[redacted]` under
a key the current file does not hold is 400 naming it. The text is then parsed as
TOML and run through the loader's startup checks -- unknown keys, every
constrained value, the shape of `[board]`, `[agents]` and `[worktree]` --
exactly as
`factory.py` does before it claims anything; what the daemon does not do
is probe the host (whether a program is on PATH, whether Docker answers),
which is the loop's question at its next start. A document the loader
refuses is 400:

```json
{"ok": false, "error": "[holo2] /home/.../config.toml: [loop] workers must be an integer >= 1, got 0"}
```

and nothing is written. An accepted document is recorded first, a human
`config_edit` interventions row on the store's newest run naming the file
and the backup (a target with no store or no run has nothing to record
against and is 503, the file untouched); then the previous text is copied
to `config.toml.bak-STAMP` beside the file, `STAMP` the UTC time to the
second (`-2`, `-3` when that second already has one), and the new text
lands by rename from a staging file of its own, so a reader sees the old
file or the new one and never a torn one. Writes are taken one at a time,
from the read of the current file to the rename, so two clients cannot
back up the same text twice and lose an edit. The reply is

```json
{"ok": true, "path": "/home/.../config.toml", "backup": "/home/.../config.toml.bak-20260910T120000Z", "applies": "next loop start", "recorded": 42, "probe": null}
```

`backup` is null when there was no file to keep. Backups are not pruned.

`PUT /config` also takes `{"patch": {...}}` in place of `text`: a flat
object of dotted `table.key` to a string, an integer, a boolean or a list
of strings,

```json
{"patch": {"loop.workers": 3, "worktree.setup": ["make deps", "make lint"], "agents.implementer": "claude -p"}}
```

The daemon loads the current file with `tomlkit`, sets each key -- creating
a `[table]` the file lacks, editing a multi-line array item by item so its
lines and comments stay -- and serialises it, so comments, order and the
formatting of everything but the patched values survive byte for byte;
the result is then held, recorded, backed up and written exactly as a
`text` is, with the same reply. A key with no table part, a table this
version does not read, a value of another shape or a `[table]` that is
not one is 400 naming the key and nothing is written; a value the loader
refuses is the same 400 a `text` gets. The interventions note names the
patched keys. `tomlkit` is the factory's one dependency (`requirements.txt`);
a daemon started without it exits naming the module and the install line.

`probe` is the implementer probe the loop runs at startup, run here when
the write changed `[agents] implementer` ([config.md](../config.md)): the
command as written, asked for the word `ready` in an empty directory under
the loop's cap, after the file is replaced. It reports and does not gate
-- the write is `ok` with the file and backup in place whatever the route
said -- so a route that does not answer is found at the write, not by the
next loop start refusing to run. Null when the key did not change or now
names no route. Otherwise

```json
{"ok": false, "command": ["my-harness", "--fast", "Reply with the single word: ready"], "returncode": 1, "timed_out": false, "timeout": 90, "output": ["my-harness: unknown option --fast"]}
```

with `returncode` null and `timed_out` true when the cap ended it, and
`output` the last lines the route printed.

## What a `pr_open` item's action is not

`/attention` ([HTTP endpoints](http.md#get-attention)) sends a run parked
on its pull request as kind `pr_open`, with the PR's URL and the reason it
parked. The console's one action on it, "Open PR", opens that URL in a
new tab and posts nothing: the pull request waits on a review or a merge
by a person, and the daemon has no route for either. An "Approve" route
belongs to a later ticket; `--approve KO-n` on the writer host merges a
parked candidate today.

## Errors

| Status | When |
| --- | --- |
| 400 | the body is not a JSON object, or `requeue` has no `ticket`; `PUT /config` whose `text` is not a string, is not TOML, the loader refuses, or holds a `[redacted]` with no current value; a `patch` that is not an object, or with a key the daemon cannot apply |
| 401 | no exact bearer value, on any bind; body `{}`, nothing run or written |
| 404 | `[serve] actions` is not `true`, or the action is not one of the three; `/config` without `[serve] config_edit = true` |
| 405 | `POST` on any path outside `/actions/`; `PUT` on any path but `/config` |
| 503 | `requeue` against a target with no store yet; `PUT /config` with no store or no run to record against |

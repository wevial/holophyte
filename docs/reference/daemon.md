# The daemon's actions

`--serve` is a read daemon: every `GET` route in [HTTP endpoints](http.md)
opens the store read-only and closes it. One opt-in makes it also an
operator's hand on the writer host. With

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
| 400 | the body is not a JSON object, or `requeue` has no `ticket` |
| 401 | no exact bearer value, on any bind; body `{}`, nothing run or written |
| 404 | `[serve] actions` is not `true`, or the action is not one of the three |
| 405 | `POST` on any path outside `/actions/` |
| 503 | `requeue` against a target with no store yet |

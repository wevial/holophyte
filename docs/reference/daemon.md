# The daemon's actions

`--serve` is a read daemon: every `GET` route in [HTTP endpoints](http.md)
opens the store read-only and closes it. Two opt-ins make it also an
operator's hand on the writer host: `actions`, below, and `config_edit`,
[the project's configuration](#the-projects-configuration-get-config-and-put-config).
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
{"action": "restart-supervisor", "ok": true, "detail": "systemctl --user restart holophyte-supervise@holophyte exited 0", "unit": "holophyte-supervise@holophyte", "recorded": 42}
```

Every project action's 200 reply carries these four keys, whatever else
the route adds (`ticket`, `run`, `unit`, `facts`, ...). `action` is the route's
name. `ok` says whether the step did what was asked; `detail` says what
happened. A step that fails is `ok: false` with the reason in `detail` and
still status 200: the operator asked for a thing and is told the answer,
which is not a server error. Each action is an interventions row
(`store.record_intervention()`, the operator ladder's record-before-acting
call) written before the action runs; an action that cannot be recorded
does not run. `recorded` is that row's id, the one `holo --json` write
results cite: the row the action itself inserted, never another
writer's row of the same name (`restart_supervisor`, `launch_loop`,
`requeue`, `operator_note` for `send-back`, `approve` for `merge`,
`babysit` for `ask`, `hold`,
`release_hold`, `pause`, `resume`, `abort` or `abort_close`, and `steer`
or `operator_note` for `steer`). It is
null when the action wrote none: a refusal, `ok: false` before the
write, or a `pause` or `abort` of a run whose request is already
pending. `scout` writes no interventions row, so its `recorded` is always
null: its record, written before it starts anything, is the scout's own
row and that row's first event, `scout_queued`. `scout-file` writes none
either: its record is the filed ticket and the action item's filed key.
`scout-stop` writes none either: its record is the scout's
`scout_stop_requested` event. `scout-message` writes none either: its record is
the ask's or steer's thread message. A 400, 404 or 503 answers `{"error": ...}` instead.

## On a host daemon

`factory.py --serve` with no project answers every route on this page
under the project's prefix, `/projects/NAME/actions/...` and
`/projects/NAME/config`, `NAME` its `[serve] name` ([HTTP
endpoints](http.md#the-host-daemon)). The opt-ins move: `[serve] actions`
in `host.toml` opens the actions for every registered project at once, and
`config_edit` stays in each project's own config. Both answer only to
`host.toml`'s `[serve] machine_token_file`, on every bind, and a project's
own `token_file` under its own prefix, for one release (not `merge`,
which answers only to the machine token); the daemon refuses
to start with either opt-in on and no machine token, naming the key.
`restart-supervisor` is not a host daemon's route (404): a registered
project has no supervisor unit, and the host sweep's own action,
`run-sweep`, below, is at the root. The record of each project action is
written in that project's store, as on a project daemon; a store locked
when the action writes is that project's 503. Every read a host daemon
makes waits at most one second for a store's lock (`HOST_READ_WAIT_S` in
`holophyte/serve/serve_host.py`), not the store's own thirty, so an action can
answer 503 `database is locked` during a brief write lock where a project
daemon would have waited ([HTTP endpoints](http.md#the-host-daemon)).

## `POST /actions/run-sweep`

At a host daemon's root, behind `host.toml`'s `[serve] actions` and the
machine token. It runs `systemctl --user start --no-block
holophyte-sweep.service`: one host sweep run now, beside the timer's
(systemd never starts it twice at once, so a request while a run is in
flight joins that run). `--no-block` because a oneshot's plain `start`
would wait out the whole run.

Body: optional, a JSON object with `note` and `author`. No store owns the
sweep, and the stores may be what is down, so the record is not an
interventions row: before `systemctl` is asked, one JSON line is appended
to `HOLOPHYTE_HOME/host-actions.jsonl` and synced, with `at`, `action`
(`run_sweep`), `unit`, `via`, `pid`, `author` (default `maintainer`) and
`note`. A line that cannot be written runs nothing:

```json
{"action": "run-sweep", "unit": "holophyte-sweep.service", "ok": true,
 "detail": "systemctl --user start --no-block holophyte-sweep.service exited 0",
 "recorded": "/home/op/.holophyte/host-actions.jsonl"}
```

`recorded` is the ledger's path, null when the line could not be written;
`ok` and `detail` are as on the unit actions below. The console's
"Run sweep" on a stale sweep or supervisor row is this route.

## `POST /actions/restart-daemon`

At a host daemon's root, behind `host.toml`'s `[serve] actions` and the
machine token. Body: optional, a JSON object with `note` and `author`.
Before restarting, one JSON line is appended to
`HOLOPHYTE_HOME/host-actions.jsonl` and synced, with `at`, `action`
(`restart_daemon`), `via`, `pid`, `author` (default `maintainer`) and `note`.
If the ledger cannot be written, the daemon keeps running and answers 200
with `ok: false`, `recorded: null` and `detail` naming the ledger.

```json
{"action": "restart-daemon", "ok": true,
 "detail": "the daemon drains its requests and restarts",
 "recorded": "HOLOPHYTE_HOME/host-actions.jsonl"}
```

`recorded` is the ledger's resolved path. After writing the answer, the
daemon stops accepting requests and drains active requests for at most
`DRAIN_SEC`, logging `restart requested over HTTP`. With an adopted socket
it exits 0; the service manager starts the next daemon when a request
arrives on that socket. Otherwise it re-executes itself. It comes back on
the checkout's code; a greater root `/status` `daemon.started_ms`
identifies the new daemon. Requests while draining answer 503
`daemon restarting`.

This route does not restart the supervisor, sweep or loop. It is 404 on a
project daemon and under `/projects/NAME/actions/` on a host daemon.

## `POST /actions/restart-supervisor`

Runs `systemctl --user restart holophyte-supervise@NAME`, where `NAME` is
`[serve] name`: the instance the deploy unit templates are enabled under
(the project slug, see [Supervising and serving](../operating.md)), the
project directory's name when the key is absent. `systemctl` gets 20 s to
answer. A non-zero exit is `ok: false` with its stderr in `detail`; an
absent `systemctl` or one that outlives the cap is `ok: false` saying so.
The reply also carries `unit`, the instance addressed.

The record is a human `restart_supervisor` intervention on the store's
newest run, its narrative naming the unit and the route: interventions are
keyed by run, and a supervisor restart is about the runs it watches over.
A project with no store, or a store with no run yet, has nothing to record
against and the unit is left alone: `ok: false` saying so, `recorded`
null, `systemctl` not called. No body is read.

## `POST /actions/launch-loop`

Runs `systemctl --user start holophyte-loop@NAME`: one pass of the loop as
the deploy template defines it, inactive again once the queue is down.
Everything else is as `restart-supervisor`, the intervention a
`launch_loop` row, except that a store with no run yet links the
`launch_loop` intervention to the project instead
(`store.record_project_intervention()`), and `recorded` is that
interventions row's id, not the project's; only a store with neither
leaves the unit alone.

## `POST /actions/requeue`

Body: a JSON object with `ticket` (required, the Linear identifier, `KO-n`),
`note` (optional, why the ticket goes back in the queue; a fixed note
saying it came from the console when absent) and `force` (optional
boolean, default `false`; `true` is `--force` and requires a non-blank
`note`, the written diagnosis, with no console default). The daemon does
exactly what `factory.py PROJECT --requeue KO-n --note TEXT` does: the store's one
`requeue` transaction, a `requeue` interventions row carrying the note on
the failed run and the ticket walked to `ready`. The reply carries
`ticket` and, on success, `run`, the failed run it was requeued after.

A `ticket` the store never mirrored, or one the store refuses to requeue
(a live run, a ticket not `in_flight`, a last run that did not fail, a
ticket already relaunched twice against one failure since a human last
acted on it), is 200 with `ok: false` and the refusal in `detail`; nothing
is written. The relaunch refusal asks for a diagnosis: `"force": true`
with the diagnosis as the `note` requeues the ticket anyway, its note
recorded as `forced past N relaunches: NOTE`. A body that is not a JSON
object, one with no `ticket`, a `force` that is not a boolean, or
`"force": true` without a non-blank `note` is 400 naming it. A project with no store is 503.

## `POST /actions/abort`

Body: a JSON object with `run` (required, the run to end), `note`
(required, why), `close` (optional boolean, default `false`) and
`pr_comment` (optional text). The daemon does what
`factory.py PROJECT --abort KO-n --note TEXT` does, with `close: true` as
`--close-pr` and `pr_comment` as `--pr-comment TEXT`. With `close: true`
the run's pull request is closed with no comment, or, with `pr_comment`,
after one comment holding `pr_comment` alone under the factory's comment
header. `pr_comment` is posted only with `close: true`. The abort note is
never posted to GitHub.

A `run` that is not a positive integer, a missing or blank `note`, a
`close` that is not a boolean, a `pr_comment` that is not text or is blank,
or a `pr_comment` without `close: true` is 400 naming it; nothing is
written. A `null` `pr_comment` is no comment.

## `POST /actions/merge`

Body: a JSON object with `run` (required, the run parked for a human's
merge), `note`, `author` and `bypass_review` (all optional). Under `[merge] mode = "pr"`
and `approve = "human"` a green and quiet pull request parks its run in
`awaiting_merge_approval`; this route is a console's "merge" on it, and
it merges only an approved, green, quiet pull request.

First it computes the readiness [`GET /runs/N/merge`](http.md#get-runsnmerge)
answers, again, at call time: the run parked on its pull request under
`approve = "human"`, then GitHub read afresh through the babysitter's own
pull request read for an `APPROVED` review decision, passing required
checks, `MERGEABLE`, no open review thread, and the branch head on
`origin` and the pull request still the run's `candidateSha` or
`approvedSha`. Not ready is 200 with `ok: false` and nothing written:

```json
{"action": "merge", "ok": false, "run": 52, "reason": "review_not_approved",
 "detail": "GitHub's review decision is REVIEW_REQUIRED", "facts": [...],
 "recorded": null}
```

`reason`, `detail` and `facts` are the read route's. A
`review_bypassable` run is refused so too unless the body carries
`"bypass_review": true`; with it the run is released as a ready one is.
The flag on a ready run is a plain merge that records no bypass, and on
any other reason it changes nothing. Ready, the daemon
calls the store's `approve` release, the one `--approve KO-n` makes: in
one transaction an `approve` interventions row on the run, the run
released `abandoned` with `resumePhase` `merge_gate`, and the ticket
walked to `ready`. Its note reads `AUTHOR via the console: merge at head
SHA; FACTS held`, then the body's `note` when one is given; `author`
defaults to `maintainer`. A bypass lists every fact but `review_approved`
as held and adds `bypassing the required review: DETAIL`, the
`review_approved` fact's `detail` naming the bypassed ruleset, and the
answer's `detail` reads `approved at SHA, bypassing the required review;
...`. The loop merges a bypass release as any other: GitHub grants the
merge to a user its ruleset lets bypass, and a merge GitHub still
refuses takes the loop's existing `MergeRefused` path. The daemon does not merge: the loop's next
claim resumes the candidate on its pull request and the babysitter
merges it, re-checking green and quiet under the merge lock and parking
it again otherwise.

```json
{"action": "merge", "ok": true, "run": 52, "ticket": "KO-219",
 "head_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
 "detail": "approved at 5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f; the loop's next claim merges the candidate on https://github.com/example/repo/pull/31",
 "recorded": 87}
```

The release names the run it checked: a ticket that moves between the
read and the write, a newer run parked in its place included, is the
store's `ApproveRefused`, `ok: false` with `reason` `not_parked` and the
refusal in `detail`, and nothing is approved. On a host daemon
`/projects/NAME/actions/merge` answers only to the machine token; a
project's own token is 401. A `run` that is not a positive integer is
400, and so is a `bypass_review` that is not a boolean, before GitHub is
read; a project with no store is 503. `--approve` and `holo approve` keep
releasing without reading GitHub.

## `POST /actions/ask`

Body: a JSON object with `run` (required, a run parked on its pull
request), `question` (required) and `author` (optional, default
`maintainer`). The console's question about the pull request: the loop's
next claim answers it in the console, without waiting for the host sweep to
see new pull request activity. Nothing from a console ask is posted to GitHub.

A `run` that is not a positive integer is 400, a run the store does not
hold is 404 `no such run`, and a project with no store is 503. Other
refusals are 200 with `ok: false`, a `reason` and a `detail`, and write
nothing; they are checked in this order:

| `reason` | When |
| --- | --- |
| `empty_question` | `question` is missing, not a string, or blank |
| `no_pull_request` | the run has no pull request URL, as under `[merge] mode = "local"` |
| `ask_pending` | an earlier console ask on the same ticket and pull request URL has no answer yet |
| `finished` | the run has ended: merged, failed, abandoned or rejected |
| `not_parked` | the run is open but is not its ticket's newest run parked in `awaiting_merge_approval`: it is live in the loop, or paused |

```json
{"action": "ask", "ok": false, "run": 52, "reason": "ask_pending",
 "detail": "console ask event 812 on https://github.com/example/repo/pull/31 is not answered yet",
 "recorded": null}
```

Accepted, one transaction records a `console_ask` event on the run (detail
level, payload `{"question", "author"}`), writes a `babysit` interventions
row with source `human` and the note `AUTHOR via the console: ask:
QUESTION`, and releases the park as `send-back` does: the run ended
`abandoned` with `resumePhase` `merge_gate`, and the ticket walked to
`ready`. `event_id` is the ask's event and `recorded` the interventions
row. The daemon calls no GitHub API.

```json
{"action": "ask", "ok": true, "run": 52, "ticket": "KO-219",
 "event_id": 812, "recorded": 88,
 "detail": "console ask event 812 recorded; the loop's next claim answers it in the console"}
```

On that claim the babysitter treats each unanswered console ask on the
ticket's pull request as an `@holophyte ask:` on its conversation, whatever
`[merge] mention_accounts` lists: the adjudicator reads the checkout and the
ticket and changes nothing. No comment, review reply or issue comment is
posted: the answer, redacted, is recorded as a `console_ask_answered` event
with payload `{"event_id", "url", "answer"}` and a null `url`, and the run
parks again as before. An ask answered before console answers stayed in the
console keeps its posted comment's `url`, and such a comment, found on the
pull request, still settles its ask. [`GET /runs/N/asks`](http.md#get-runsnasks) lists the asks and
their answers. A failed or empty answer fails the run, as an
`@holophyte ask:` does, and the ask stays unanswered.

## `POST /actions/scout`

Body: a JSON object with `ask` (required, non-blank text: what the scout
looks into; links, ticket keys and pull request URLs go in its text),
`kind` (required, `bug`, `research` or `recon`), `lead` (optional, a
`[scout.leads.NAME]` route; default `[scout] lead`), `helper` (optional, a
`[scout.helpers.NAME]` route; default `[scout] helper`), `swarm` (optional
positive integer, helpers at once; default `[scout] swarm`), `minutes`
(optional positive integer, the wall-clock budget; default `[scout]
budget_min`), `subject` (optional, `KIND:REF`: what the scout is about)
and `author` (optional, default `maintainer`). The helper
total and the wave count are always the config's `helper_turns` and
`waves`. `holo scout "ASK" --kind K`, `holo bug`, `holo idea` and the
`holo mcp` `scout` tool are the same launch; `--about KIND:REF` and the
tool's `subject` send `subject`.

`subject` names one thing the project's store holds: `run:N`, run N;
`defect:N`, run N stopped on its ticket's wording, by a `contradiction`
event or a supervisor sweep that failed it as a stuck criterion or a stuck
review, as the Findings page counts it; `ticket:KEY`, a ticket, recorded
under the store's key for it; or `pr:URL`, a pull request one of the
project's runs opened, the URL compared and recorded as given. The scout
records it as `subjectKind` and `subjectRef`, and the scout reads serve it
as `subject`.

One transaction records the scout in phase `queued` with its ask, kind,
routes, swarm, helper total, waves, budget and subject, and its first
event, `scout_queued` (detail level, payload `{"author", "via",
"subject"}`, `subject` the string as given or null, `via` being
`POST /actions/scout`, `holo scout` or `holo mcp`). After that commit the
daemon runs the start half of the host sweep's scout step once: while the
host's live scouts are fewer than `host.toml`'s `[scout] max_parallel`, it
starts the host's oldest queued scouts, each moved to `planning` at
`main`'s sha and spawned as `factory.py --scout ID PROJECT`, so a new
launch never jumps the queue. Launches and the host sweep count and start
under one host-wide lock, so launches at once never start more scouts than
the slots. A scout not started stays `queued`, with
`ok: true` and why it did not start in `detail`, and a later host sweep
starts it. That includes a scout whose process could not be spawned: the
step records a `scout_start_failed` event and moves it back to `queued`
for the next pass, however often it fails; so does a launch whose start
lock in the host home cannot be opened. A launch whose
scout another pass started answers `ok: true` with `queued` false; one
whose scout has ended answers `ok: false` with its phase and reason.

The reply adds `scout`, the new scout's id, and `queued`, true when it was
not started; `recorded` is null.

```json
{"action": "scout", "ok": true, "scout": 7, "queued": true,
 "detail": "scout 7 queued: 2 of 2 scout slots in use", "recorded": null}
```

A body that is not a JSON object, a missing or blank `ask`, a `kind`
outside the three, a `lead` or `helper` that is not a string, or a `swarm`
or `minutes` that is not a positive integer is 400 naming the field. So is a
`subject` that is not a string, has no colon, has a kind outside the four,
a blank ref, a `run` or `defect` ref that is not a positive integer, or a
`pr` ref that does not start `http://` or `https://`. A
`lead` or `helper` the config does not define (`detail` names the
`[scout.leads]` or `[scout.helpers]` table), a `swarm` above the
config's `helper_turns`, and a `subject` the store does not hold (a run or
ticket it lacks, a `defect` run with no such stop, a URL no run opened;
`detail` names its ref) are 200 with `ok: false`. None of these writes
anything. A project with no store is 503.

## `POST /actions/scout-file`

Body: a JSON object with `scout` (required, a positive integer: the
scout's id), `item` (required, a positive integer: the action item's
position), `note` (optional) and `author` (optional, default
`maintainer`). It files one action item of a `done` scout as a Backlog
ticket, never Ready. `holo scout file ID N` and the `holo mcp`
`scout_file` tool are the same filing. On a host daemon
`/projects/NAME/actions/scout-file` answers only to the machine token; a
project's own token is 401.

An unknown scout is 404 `no such scout`, an unknown item 404 `no such
action item`. A scout not `done` is 200 with `ok: false` naming its
phase, and nothing is checked or written. An item already filed is 200 with `ok: false`, `ticket` the
key it was filed as, and `detail` `scout 14 item 2 is already filed as
KEY`; nothing is checked or written. A `revise` item is 200 with
`ok: false`, `ticket` null and `detail` `scout 14 item 2 revises NAT-3 at
revision 2; save it on NAT-3's edit page`: a revision is saved on its
ticket's edit page, so nothing is checked or filed and the item stays
unfiled, and `holo scout file` exits 1 printing that `detail`. A project with no `[board]` is
`ok: false`. A blank `body` is `ok: false`, and so is a body with no H1
whose item has a blank `title`; nothing is written. A body with no H1 is
filed as `# TITLE` (the item's `title`) and a blank line before it,
placed after a leading `+++` header so the header keeps its fields, and
that filed text is what the checker judges, so a free-text body is never
refused for its missing title.

The filed text is judged by the checker
[`POST /tickets/check`](http.md#post-ticketscheck) answers with, for the
`backlog` column. Only what refuses a Backlog filing on the board refuses
it: the check's `blocks` (a `Depends on` key the project does not hold, or
text the leak guard keeps out of the store), or the board refusing it.
Then the answer is 200 with `ok: false`, `problems` (every problem, in the
checker's order), `advisories`, and `detail`, the first problem; nothing
is filed, and the item stays unfiled.

Otherwise the text is filed to Backlog through the project's board, with
the title, estimate and `Depends on` read from it, and the item records
the filed key and time. Any other problem files it as a Backlog draft:
its first board note opens `Filed from scout 14 item 2 as a Backlog
draft; the ticket checker reports:` with each problem on its own `- `
line, in the checker's order. A problems note that cannot be posted
leaves `ok: true` and `detail` says so. A project's filings run one at a
time, from check to note, so two posts of one item file it once. With a
`note`, the filed ticket's next board note (its first, on an item with
no problems) reads `AUTHOR: NOTE`; a note that cannot be posted leaves
`ok: true` and `detail` says so. The reply adds `scout`, `item`,
`ticket`, the new key, `problems` (empty for a clean item) and
`advisories`; `recorded` is null.

```json
{"action": "scout-file", "ok": true, "scout": 14, "item": 2,
 "ticket": "KO-412", "problems": [], "advisories": [],
 "detail": "scout 14 item 2 filed as KO-412 in Backlog", "recorded": null}
```

A draft's `detail` reads `scout 14 item 2 filed as KO-412 in Backlog as
a draft; the checker's problems are its first note`, and its `problems`
are every problem, in the checker's order. `holo scout file` exits 0 and
prints the `detail`, then each problem.

A body that is not a JSON object, a `scout` or `item` that is not a
positive integer, or a `note` or `author` that is not text is 400 naming
the field. A project with no store is 503.

## `POST /actions/scout-stop`

Body: a JSON object with `scout` (required, a positive integer: the
scout's id), `note` (required, non-blank: why) and `author` (optional,
default `maintainer`). It stops one scout. `holo scout stop ID "note"`
and the `holo mcp` `scout_stop` tool are the same stop. On a host daemon
it is `/projects/NAME/actions/scout-stop`, admitted as the other project
actions are.

Every stop the route accepts first records a `scout_stop_requested`
event on the scout, at the `detail` level, its payload `{"author",
"note", "via", "pid"}`, `pid` the process the stop acts on or null. Then,
by the scout's phase:

- `queued`: the scout ends `stopped` at once, its reason `stopped by
  AUTHOR: NOTE`. Nothing is signalled, and the host sweep never starts it.
- live (`planning`, `swarming`, `synthesizing`, `answering`) on this host,
  with a recorded `pid` whose command line (`ps -o command= -p PID`) names
  `--scout ID`: that process is sent SIGTERM. The scout process ends
  itself: it kills the process groups of its running lead turn and helper
  turns, each such helper task ending `stopped`, runs no further turn,
  ends the scout `stopped` with the reason `stopped by AUTHOR: NOTE` read
  from its newest `scout_stop_requested` event, records a `scout_stopped`
  event and exits 0. The reply comes before that end. A process gone
  before the signal reaches it is treated as no running scout process
  (below), unless the scout has ended meanwhile: that end is kept, and
  the reply names its phase, `ok` true only when it is `stopped`. A
  signal the process's owner refuses is 200 with `ok: false` saying so,
  the scout left live.
- live with no running scout process (no `pid`, or one whose command line
  does not name `--scout ID`): the scout ends `stopped` at once, its
  reason `stopped by AUTHOR: NOTE; no scout process was running`, and
  nothing is signalled.
- live on another host: 200 with `ok: false` naming that host, whose
  daemon must stop it; nothing is written.
- already ended (`done`, `failed`, `stopped`): 200 with `ok: false`
  naming the phase; nothing is written.

An unknown scout is 404 `no such scout`. The reply adds `scout` and
`pid`, the process signalled or null; `detail` is `stop sent to scout N's
process P` or `scout N stopped`, and `recorded` is null.

```json
{"action": "scout-stop", "ok": true, "scout": 14, "pid": 48213,
 "detail": "stop sent to scout 14's process 48213", "recorded": null}
```

A body that is not a JSON object or a `scout` that is not a positive
integer is 400 naming the field; a `note` missing or blank is 400 `note
must say why`, and an `author` that is not text is 400. A project with no
store is 503.

## `POST /actions/scout-message`

Body: a JSON object with `scout` (required, a positive integer: the
scout's id), `mode` (required, `ask` or `steer`), `text` (required,
non-blank: the ask or the steer), `answers` (optional, a positive
integer: the id of the scout's card the message answers) and `author`
(optional, default `maintainer`). An `ask` asks a `done` scout about its
report, and its lead answers in its thread, which
[`GET /scouts/ID`](http.md#get-scoutsid) serves as `thread`. A `steer`
tells a `done` scout to look further: it runs one more pass on the steer
and appends that pass's report. `holo scout ask ID "TEXT"` and
`holo scout steer ID "TEXT"`, each with `--answers MSG`, and the
`holo mcp` `scout_ask` and `scout_steer` tools are the same messages. On a
host daemon it is `/projects/NAME/actions/scout-message`, admitted as the
other project actions are.

The message is recorded first, on the scout's thread. Then the route runs
the start half of the host sweep's scout step once, which takes the
oldest waiting work while the host is under its `[scout] max_parallel`:
for an ask the scout goes to `answering` and its lead's session is
resumed to answer; for a steer the scout goes to `planning` and runs its
next pass. A message not taken waits for the sweep's next pass.

An answer turn's ANSWER and a pass's REPORT may end with a `proposals`
list, each `{"question": "...", "choices": [{"label": "...", "mode":
"ask"}, {"label": "...", "mode": "steer"}]}` with two to four choices,
each choice's `mode` `ask` or `steer`.
After the turn's own record, at most `MAX_PROPOSALS` (2) of them, in the
order given, are posted to the thread as cards: author `scout`, mode
`answer`, `answers` the message the turn took, the question as text and
one block `{"type": "proposal", "question", "choices"}`. A malformed
proposal, or one past the cap, is dropped and named in the turn's
`scout_answered` or `scout_pass_done` event, under `dropped_proposals`.
A person answers a card with an ask or a steer whose `answers` is the
card: a client posts a choice as its label with that choice's mode, and a
typed answer as its text; the daemon does not match the text to a label. The turn that takes the message reads the card's question
beside it.

An unknown scout is 404 `no such scout`. A scout not `done` is 200 with
`ok: false`, `reason` `not_done` and `detail` naming its phase; a scout
with an ask or steer still waiting is 200 with `ok: false`, `reason`
`ask_waiting` and `detail` naming the waiting message's id. An `answers`
that is not a card of this scout, or a card a person's message already
answers, is 200 with `ok: false`, `reason` `card_refused` and `detail`
naming the card's id; the store holds that rule, so two messages cannot
both answer one card. None of these writes anything. The reply adds
`scout`, `message`, the new message's id, and `queued`, true when the
message was not taken and waits for a scout slot; `detail` is `scout N is
answering message M`, `scout N runs pass P for message M` or `message M
waits for a scout slot: ...`, and `recorded` is null.

```json
{"action": "scout-message", "ok": true, "scout": 14, "message": 3,
 "queued": false, "detail": "scout 14 is answering message 3",
 "recorded": null}
```

A body that is not a JSON object, a `scout` that is not a positive
integer, a `mode` missing or not `ask` or `steer`, a `text` missing or
blank, and an `answers` that is not a positive integer are 400 naming the
field. A project with no store is 503.

## The project's configuration: `GET /config` and `PUT /config`

A second opt-in, separate from `actions`:

```toml
[serve]
config_edit = true
```

opens the project's own `config.toml` -- the file the loop reads at
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
{"text": "[serve]\ntoken_file = \"...\"\n...", "values": {"serve": {"token_file": "..."}, "loop": {"workers": 2}}, "path": "/home/.../config.toml", "applies": "next loop start", "revision": "<sha256>", "modified_ms": 1791969120000, "saved": {"at_ms": 1791969120000, "actor": "maintainer"}}
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
still readable. A project with no file yet has `text` `""`. `values` is the same redacted
text parsed with `tomllib`, as JSON: a quoted table name or a triple-quoted
string is an ordinary key or value here, so a client reads settings from
`values` and never parses TOML itself; null when the text does not parse. `applies` says when a change takes effect: the loop reads
the file once at startup, so a written change waits for the next start
(`POST /actions/launch-loop`, or the supervisor's), and a running loop is
not touched.

`revision` is the SHA-256 hex digest of the file's text before redaction,
as read by the daemon; an absent file has the digest of empty text.
`modified_ms` is the file's modification time in epoch milliseconds, or
null with no file. `saved` names the newest `config_edit` intervention on
this project's runs: `at_ms` is its timestamp and `actor` is `maintainer`,
`operator`, `supervisor`, or null for an older row with no recorded actor.
With no such intervention, `saved` is null. A manual file edit changes the
revision and modification time but does not create a save record.

Send the revision read in `If-Match` when saving either `text` or `patch`.
Under the write lock, the daemon compares it with the current file before
restoring secrets, validating, recording or writing. A mismatch answers
409, without creating a backup or intervention:

```json
{"ok": false, "error": "config.toml changed since this revision was read; read it again", "current": "<current-sha256>"}
```

The error names the project's own file. Omitting `If-Match` preserves
unconditional writes for existing clients.

`PUT /config` takes `{"text": "..."}`, the whole new file. Every
`"[redacted]"` value in it -- any TOML string reading `[redacted]`,
however quoted, a comment beside it or not -- is replaced by the current
file's value for the same key in the same table after the revision check, so
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
{"ok": false, "error": "[holo2] /home/.../config.toml: [loop] workers must be an integer >= 1, got 0", "line": 2}
```

and nothing is written. An accepted document is recorded first, a human
`config_edit` interventions row on the store's newest run naming the file
and the backup (a project with no store or no run has nothing to record
against and is 503, the file untouched); then the previous text is copied
to `config.toml.bak-STAMP` beside the file, `STAMP` the UTC time to the
second (`-2`, `-3` when that second already has one), and the new text
lands by rename from a staging file of its own, so a reader sees the old
file or the new one and never a torn one. Writes are taken one at a time,
from the read of the current file to the rename. Clients sending `If-Match`
cannot overwrite an intervening save with a stale revision. The reply is

```json
{"ok": true, "path": "/home/.../config.toml", "backup": "/home/.../config.toml.bak-20260910T120000Z", "applies": "next loop start", "recorded": 42, "probe": null, "revision": "<written-sha256>"}
```

`revision` names the written file. `backup` is null when there was no file
to keep. Backups are not pruned.

`PUT /config` with `{"text": "...", "check": true}` checks without saving.
It restores redacted values from the current file and runs the same TOML
parser and loader checks. Success is `200 {"ok": true, "checked": true}`;
it takes no save lock, writes no file or backup, records no intervention,
and probes no implementer. Checks ignore `If-Match` and do not need a run
in the store. The `config_edit` opt-in and token gate still apply.
`check` must be a boolean; any `patch` body containing `check` is 400
(`check takes a text body`). With `check` false, a text body saves normally.

Every 400 for a text body, whether a check or a save, includes `line`:
the 1-based line from a malformed-TOML message, or the first assignment
of the named `[table] key` under that header (including a top-level
`table.key` assignment). It is null when the refusal cannot be located.
Only the first refusal is returned.

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
patched keys. `tomlkit` is one of the factory's two dependencies
(`requirements.txt`; the other, `mcp`, only `holo mcp` needs); a daemon
started without it exits naming the module and the install line.

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

## What a no-commit turn leaves on the run

An implementer turn that ends without a commit, whatever its exit code,
normally fails the run. A PR fix turn that is not cut off, leaves a clean
candidate, and answers every addressed thread with a `THREAD n:` summary
instead parks as `blocked_on_operator` with the implementer's reasons, so
an operator can send it back with a corrected instruction. Dirty no-commit
fix turns retain the failure and recovery path.

The loop records one `detail`-level runEvent of kind `implementer_output`:
its summary is the last non-empty line of the implementer's final message
and its payload the message's last 4000 characters
(`OUTPUT_TAIL` in `holophyte/loop/implement.py`), passed through the prose redactor
(`redact_prose()` in `holophyte/redact.py`): every credential the config
and the environment hold, and every `name = value` pair with a secret's
name wherever it sits in the text, read `[redacted]`. It sits beside the
stream's other kinds (`phase_change`, `crash`, `carried_candidate`,
`merge_gate`, `pull_request`) and is what tells a contract the implementer
judged unwinnable from a crash or a refusal. `GET /runs/N` answers it among
the run's `events` by its summary, the one `detail` kind the route shows;
the payload is read from the store.

## What a `pr_open` item's action is not

`/attention` ([HTTP endpoints](http.md#get-attention)) sends a run parked
on its pull request as kind `pr_open`, with the PR's URL and the reason it
parked. The console's one action on it, "Open PR", opens that URL in a
new tab and posts nothing: the pull request waits on a review or a merge
by a person. For a run parked for a human's merge (`[merge] approve =
"human"`), [`GET /runs/N/merge`](http.md#get-runsnmerge) says whether it
may merge now and [`POST /actions/merge`](#post-actionsmerge) releases it
to the loop once GitHub shows it approved, green, mergeable, quiet and at
the parked head. The console draws a Merge button when it is ready, and a
"Merge (bypass review)" button when its readiness is `review_bypassable`,
each behind an in-row confirm; the bypass confirm names the bypassed
ruleset and posts `"bypass_review": true`.
Beside it the console draws Ask from [`GET /runs/N/asks`](http.md#get-runsnasks):
an in-row question box whose Send posts [`POST /actions/ask`](#post-actionsask)
with the row's `run` and the `question`. While the latest ask is unanswered
the row shows it as waiting and offers no Ask; once answered, the row shows
the question and the answer, with no link to GitHub.
`--approve KO-n` on the writer host releases a parked candidate without
reading GitHub.

## Errors

| Status | When |
| --- | --- |
| 400 | the body is not a JSON object, or `requeue` has no `ticket`, a non-boolean `force`, or `"force": true` without a non-blank `note`, or `merge` has no positive integer `run`; `PUT /config` whose `text` is not a string, is not TOML, the loader refuses, or holds a `[redacted]` with no current value; a `patch` that is not an object, or with a key the daemon cannot apply |
| 401 | no exact bearer value, on any bind; body `{}`, nothing run or written |
| 404 | `[serve] actions` is not `true`, or the action is not one of this page's; `/config` without `[serve] config_edit = true`; on a host daemon, `restart-supervisor`, a project name outside the registry, and any root action but `run-sweep` or `restart-daemon`; `restart-daemon` on a project daemon or under `/projects/NAME/actions/` |
| 405 | `POST` on any path outside `/actions/`; `PUT` on any path but `/config` |
| 503 | any request while the daemon drains for restart (`daemon restarting`); `requeue` or `merge` against a project with no store yet; `PUT /config` with no store or no run to record against; on a host daemon, a project whose store is stamped newer than the build can read, or is locked or corrupt when the action reads or writes it |

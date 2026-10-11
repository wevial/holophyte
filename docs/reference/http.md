# HTTP endpoints

`--serve PORT|HOST:PORT PROJECT`, the project daemon, answers the JSON
paths below and serves the console's built files at `/`. `--serve` with no
project, the host daemon, answers the same paths for each registered
project under `/projects/NAME` and its own `/status` and `/attention` at
the root ([The host daemon](#the-host-daemon)); every section below
describes a project's route as either daemon answers it. Every response carries
`Cache-Control: no-store` and `Access-Control-Allow-Origin: *`; the JSON
ones `Content-Type: application/json`; every GET route opens the store
read-only and closes it. The open origin is for the console page, which
one daemon serves and which fetches the others from the browser: without
the header the browser refuses a cross-origin answer. The daemon reads by
default and writes only through two opt-ins, documented in
[The daemon's actions](daemon.md): the `POST /actions/...` routes
`[serve] actions = true` opens and the `PUT /config` route
`[serve] config_edit = true` opens. On loopback the bind address is the
whole boundary for reads, and beyond it every JSON route but `/peers` is
behind a bearer token ([Authentication](#authentication)). Unknown paths
are 404 and any method but GET is 405, both with a JSON `error`, except
those writing routes. A project with no store answers 503.

## `GET /status`

```json
{
  "project": "/path/to/repo",
  "schema_version": 35,
  "admission": "enabled",
  "hold_note": null,
  "active_routes": {"implementer": {"command": "claude"},
                    "reviewer": {"command": "claude", "fallback": "claude"},
                    "adjudicator": {"command": "codex"},
                    "writer": {"command": null},
                    "trimmer": {"command": null}},
  "route_labels": {"implementer": "claude opus", "reviewer": "codex gpt-5.6-sol",
                   "reviewer_fallback": null, "adjudicator": "codex gpt-5.6-sol",
                   "writer": "claude opus", "trimmer": "claude opus"},
  "workers_on_previous_build": 0,
  "host": "writer-1",
  "now": 1788450534491,
  "toil": {"24h": {"interventions": 3, "merged": 2, "per_merge": 1.5,
                   "by_action": {"requeue": 2, "babysit": 1}},
           "7d": {"interventions": 4, "merged": 3, "per_merge": 1.3333333333333333,
                  "by_action": {"requeue": 2, "approve": 1, "babysit": 1}}},
  "daemon": {"started_ms": 1788446934491, "pid": 2801590},
  "supervisor": {"state": "live", "pid": 2801613, "heartbeat_age_ms": 8258, "host": "writer-1"},
  "thresholds": {"heartbeat_stale_ms": 300000, "strikes": 2, "run_cap": 3.0},
  "actions": false,
  "config_edit": false,
  "runs": [
    {"id": 52, "ticket": "KO-219", "ticket_url": "https://linear.app/example/issue/KO-219",
     "title": "The sweep frees a silent lease", "phase": "working",
     "started_ms": 1788450461675, "heartbeat_age_ms": 71989, "elapsed_ms": 72816,
     "working_ms": 61200, "work_started_ms": 1788450511675,
     "agent_ms": 61200, "verify_ms": 0, "run_count": 1,
     "verify_started_ms": null,
     "time_box_ms": 1500000, "round": 0, "strikes": 0, "host": "writer-1",
     "stop_requested": null, "stop_action": null}
  ]
}
```

`runs` lists every live run in a sweepable phase. Ages are computed by the
daemon against its own `now`, so a client compares one number to
`thresholds.heartbeat_stale_ms` and never has to agree with the writer
host about the time. A row reports its ticket's chain of runs: every
run of the ticket up to the live one since the ticket's last merged run,
so a run sent back to the babysitter, parked on CI, paused or failed and
requeued counts toward the live one. `run_count` is the number of runs in
the chain, 1 for a first attempt. `started_ms` is the chain's first run's
start as epoch milliseconds; `round` is the review rounds the live run
has recorded so far; `strikes` is
the sweep's tally for the run, 0 when it is not under suspicion.
`ticket_url` is the ticket's page on the board (`tickets.url`), null when
the store has none. `elapsed_ms` is wall time since `started_ms`.
`working_ms` is the work the chain has recorded plus, while the live run's
span of work is open, the time since that span began, null when no run of
the chain had its work measured; `agent_ms` and `verify_ms` are summed over
the chain the same way; `work_started_ms` is when the open span began, as epoch
milliseconds, and null while no span is open,
so a client interpolates work between polls only from `work_started_ms`
and never from `elapsed_ms`. `agent_ms` is the part of `working_ms` the
time box is read against and `verify_ms` the rest, the time spent in the
ticket's verify commands (the supervisor's trip still judges the live
run's own agent time, not the chain's); `agent_ms` is null when `working_ms` is, and
`verify_ms` is null for a run recorded before the split, whose work all
counts as `agent_ms`; `verify_started_ms` is set only while the open span is a
verify, else null. `time_box_ms` is the box the run is counted
against, the estimate scaled by `[agents] budget_scale`, null for a
ticket with no estimate; `thresholds.run_cap` is the hard ceiling in
multiples of that box, so a time-box bar can draw it. `stop_requested` is
the note of a pause or abort the operator has asked of the run and the
loop has not yet acted on, and `stop_action` that request's action
(`pause`, `abort` or `abort_close`); both
are null when none is pending.
`supervisor.state` is `live`, `stale` or `none`. `daemon` describes the
serving process: its pid and when it started. `project` is the
repository the daemon serves, as a path. `schema_version` is the
store's schema version (its `PRAGMA user_version`). `admission` is the
project's admission state (`store/enums.py` `ProjectAdmission`): `enabled`,
`held` or `disabled`; while it is `disabled`, `runs` is empty.
`hold_note` is the note of the latest admission change, null when there
is none.
`actions` is whether
`[serve] actions = true` opened the `POST /actions/...` routes of [The daemon's actions](daemon.md); the
console draws its action buttons disabled while it is `false`.
`config_edit` is whether `[serve] config_edit = true` opened `GET /config`
and `PUT /config`; the console's settings sheet is read-only, naming the
key, while it is `false`. `route_labels` names what each seat is configured
to run, labelled as a recorded turn is: the command's first word plus its
`-m`/`--model` value. A seat left unset shows the default the loop
dispatches, an unset `writer` or `trimmer` the implementer's label, and an unset
`reviewer_fallback` null. `active_routes` holds one entry per seat
(`implementer`, `reviewer`, `adjudicator`, `writer`, `trimmer`) naming what the seat
runs now: `command` is the executable alone, never its arguments, null for
a seat left unset in `[agents]`; while a running loop has switched the seat
to its fallback, `command` is the fallback's and the entry also carries
`fallback`, naming it. A configured `trimmer` that a running loop turned
off, its probe and `trimmer_fallback` both failed, shows `command` null and
`down` true, unlike an unset one's bare null. `workers_on_previous_build` counts the workers a
restarted loop inherited from the build it replaced and still owns, 0
when there are none. `toil` is the operator's hand work per merge over
the last `24h` and the last `7d` before `now`, the same windows as
`--report`'s `toil` lines: `interventions` counts the interventions with
source `human` in the window, project-level ones such as a `hold`
included; `merged` counts the runs that ended merged in it; `per_merge` is
their ratio, null when `merged` is 0; `by_action` counts the interventions
by action, most frequent first, ties by name.
Every `host` passes through `[report] host_label`.

## `GET /runs?limit=N`

```json
{"rows": [
  {"run": 312, "ticket": "KO-241", "actual_min": 8.4, "estimate_min": 10.0,
   "ratio": 0.84,
   "rounds": 1, "outcome": "merged", "host": "writer-1", "ended_ms": 1788478953000,
   "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f"}
], "limit": null}
```

The `--report` table as JSON, oldest first, the same rows in the same
order the terminal prints. `run` is the run's id, as `/runs/N` takes it,
which the table does not print. `ended_ms` is the run's end as epoch
milliseconds, which the table does not print; the drawer ages the last
merge from it. `merge_sha` is the full merge commit a merged run landed
on main as, null for any other outcome or a run merged before the store
recorded it. `?limit=N` keeps the first N rows and echoes `limit`; a
non-positive or non-integer limit is 400.

## `GET /shipped?limit=N&before=RUN_ID&outcome=merged`

```json
{"rows": [
  {"id": 312, "ticket": "KO-241", "title": "Run detail: files touched",
   "rounds": 1, "findings": 2, "started_ms": 1788478203000,
   "ended_ms": 1788478953000, "actual_min": 8.4, "working_ms": 504000,
   "agent_ms": 432000, "verify_ms": 72000, "wall_min": 12.5,
   "run_count": 2, "turn_count": 3, "estimate_min": 10.0,
   "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
   "commit_url": "https://github.com/example/repo/commit/5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
   "pr_url": "https://github.com/example/repo/pull/2170",
   "host": "writer-1", "outcome": "merged", "outcome_reason": null}
], "next_before": 298, "limit": 50}
```

Finished runs, newest end first, ordered by `ended_ms` descending then
`id` descending. A row is the run that closed its ticket's chain of runs:
a run that merged, or the newest run of its ticket. A run that later runs
of its ticket continue (sent back to the babysitter, parked on CI, paused,
or failed and requeued) has no row of its own; its figures count in the
row of the run that closed the chain. The chain runs from the run after
the ticket's last earlier merged run through the closing run.
`id`, `ended_ms`, `outcome`, `outcome_reason`, `merge_sha`, `pr_url` and
`host` are the closing run's. `started_ms` is the chain's first run's
start, and `wall_min` the minutes from it to `ended_ms`, CI waits and
parks included. `working_ms`, `agent_ms`, `verify_ms` and `actual_min`
(`working_ms` in minutes) are summed over the chain's runs, each null only
when no run of the chain had its work measured; `rounds` is summed over
the chain's runs. `run_count` is the number of runs in
the chain and `turn_count` the number of agent turns `/runs/N/turns`
lists, summed over them. `outcome=merged` (the default) returns only merged runs;
`outcome=all` returns every run that closed its chain, whatever its
outcome. Each row includes `outcome` and
`outcome_reason` (the stored reason cut at 400 characters, null when absent).
Any other `outcome` is 400 naming the parameter and its value. The console's
Shipped view scrolls back over it grouped by day; the Board's "shipped
today" is its first page. `findings` is the count of findings over the
chain's review rounds. `limit` defaults to 50 and is capped at 200; the
body echoes the limit applied. `before=RUN_ID` answers the rows that
ended before that run's end (ties broken by id), so a client pages by
passing `next_before` back; `next_before` is the last row's id while
more rows remain and null on the last page. A non-positive or
non-integer `limit`, or a non-integer `before`, is 400 with `error`
naming the parameter; a `before` no run has is 200 with no rows.
`/runs` is untouched: it stays the terminal's table, oldest first.

`commit_url` is the merge commit's page on the project's `origin`:
`https://HOST/OWNER/REPO/commit/SHA` when the `origin` URL is
`https://HOST/OWNER/REPO(.git)` or `git@HOST:OWNER/REPO(.git)` and the
sha is an ancestor of `origin/main` in the project's checkout. It is null
when the row has no `merge_sha`, the project has no `origin`, the remote
is of another shape (including one carrying a `?` query, `#` fragment
or credentials, which would otherwise ride into the link), or the sha has not reached `origin/main` (a local
merge never pushed, one rewritten on the way up, a fresh clone with no
`origin/main` yet), so a link is only ever to a page that exists. The
remote is read once per request and the ancestry checked once per row;
a git failure is null, never an error.

`pr_url` is the pull request the run merged through under `[merge] mode
= "pr"`, as the store recorded it when the run parked (`runs.prUrl`);
null for a run that opened none, including every run merged locally.
Nothing is read from GitHub: the URL is the one the factory itself
opened, so its last path segment is the PR number.

## `GET /runs/N`

```json
{"run": {"id": 52, "ticket": "KO-219", "ticket_url": "https://linear.app/example/issue/KO-219",
         "title": "The sweep frees a silent lease",
         "phase": "done", "attempt": 1, "started_ms": 1788450461675,
         "ended_ms": 1788451661675, "outcome": "merged",
         "elapsed_ms": 1200000, "working_ms": 912000, "agent_ms": 840000,
         "verify_ms": 72000, "time_box_ms": 1500000,
         "branch": "task/ko-219-the-sweep-frees-a-silent-lease", "host": "writer-1",
         "heartbeat_age_ms": null,
         "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
         "commit_url": "https://github.com/example/repo/commit/5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
         "pr_url": "https://github.com/example/repo/pull/2170",
         "work_started_ms": null, "verify_started_ms": null,
         "approved_at": 1788451561675, "approved_by": "operator",
         "max_rounds": 2},
 "rounds": [
  {"round": 1, "started_ms": 1788450761675, "ended_ms": 1788450941675,
   "verdict": "changes_requested", "reviewer_model": "reviewer-model",
   "findings": [{"path": "holophyte/serve/server.py", "line": 12, "severity": "p1",
                 "criterion": "AC1", "message": "the route is unmatched"},
                {"path": "holophyte/serve/server.py", "line": 40, "severity": "p1",
                 "criterion": null, "message": "Validate input", "kind": "thread",
                 "author": "review-bot", "author_kind": "bot",
                 "summary": "Validate input", "verdict": "ADDRESS",
                 "raw": "Please validate input",
                 "url": "https://github.com/example/repo/pull/2170#discussion_r1"}],
   "instructions": [{"kind": "instruction", "path": "holophyte/serve/server.py", "line": null,
                     "author": "operator", "severity": "nit",
                     "message": "Keep validation", "request": "Keep validation",
                     "url": "https://github.com/example/repo/pull/2170#discussion_r2",
                     "outcome": "changed", "reply": "Validation retained"}],
   "operator_notes": [{"note": "Keep validation", "author": "operator",
                       "kind": "operator_note", "event_id": 7, "consumed": true,
                       "run_id": 52, "round": 1}]},
  {"round": 2, "started_ms": 1788451061675, "ended_ms": 1788451181675,
   "verdict": "pass", "reviewer_model": "reviewer-model", "findings": [],
   "instructions": [], "operator_notes": []}
 ],
 "findings": [{"tone": "advisory", "message": "https://github.com/example/repo/pull/2170#discussion_r3: Consider a cache"}],
 "events": [
  {"at": 1788450461675, "kind": "phase_change", "summary": "claimed"},
  {"at": 1788450941675, "kind": "review", "summary": "round 1 asked for changes"}
 ],
 "chain": {"started_ms": 1788449861675, "elapsed_ms": 1800000, "working_ms": 1212000,
           "agent_ms": 1140000, "verify_ms": 72000,
           "runs": [
  {"id": 50, "attempt": 1, "outcome": "abandoned", "phase": "done",
   "started_ms": 1788449861675, "ended_ms": 1788450161675, "elapsed_ms": 300000,
   "working_ms": 300000, "agent_ms": 300000, "verify_ms": 0, "max_rounds": 2,
   "turn_count": 3,
   "rounds": [{"round": 1, "verdict": "pass", "reviewer_model": "reviewer-model"}]},
  {"id": 52, "attempt": 2, "outcome": "merged", "phase": "done",
   "started_ms": 1788450461675, "ended_ms": 1788451661675, "elapsed_ms": 1200000,
   "working_ms": 912000, "agent_ms": 840000, "verify_ms": 72000, "max_rounds": 2,
   "turn_count": 4,
   "rounds": [{"round": 1, "verdict": "changes_requested", "reviewer_model": "reviewer-model"},
              {"round": 2, "verdict": "pass", "reviewer_model": "reviewer-model"}]}
 ]}}
```

One run in full, by id: what the console shows when a run is expanded.
`run` is the row joined to its ticket. `ended_ms` is null while the run
is live; `heartbeat_age_ms` is the daemon's `now` minus the run's last
heartbeat while it is live and null once it has ended. `max_rounds` is
the review-round cap the loop gave this run (two rounds plus one per 800
changed lines, at most four), so a client can say "round 2 of 3" without
recomputing it; a run recorded before the store carried the cap answers
the loop's base of 2. `rounds` lists the run's review rounds oldest
first, each with its `findings` decoded into objects (`path`, `line`,
`severity`, `criterion`, `message`) rather than the stored JSON string.
A finding decoded from a pull-request review thread also carries `kind`
(`thread`), `author` (the thread's last commenter), `author_kind` (`bot`
for a login `[merge] bot_authors` or `bot_logins` names, else `user` or
`unknown` as GitHub reported it), `summary` (the finding's one-line gist,
which `message` repeats), `verdict` (the adjudication: `ADDRESS`,
`FOLLOW_UP` or `DECLINE`), `raw` (the comment's text, redacted and cut at
20,000 characters) and `url` (the thread's page). A round's `instructions` are the requests a human
reviewer addressed to the factory on the pull request, split out of
`findings`: each an object with `kind` (`instruction`), `path`, `line`
(null when the thread has none), `author`, `request` (the ask), `url` and,
where the round recorded them, `severity`, `message`, `outcome` (`changed`
or `asked`) and `reply`, the factory's answer on the thread. A round's
`operator_notes` are the private operator notes (`--babysit KO-n --note
TEXT` on a parked pull request) that round consumed: each with the
`note`, its `author`, `kind` (`operator_note`), `event_id` (the run event
that recorded it),
`consumed`, and the `run_id` and `round` that consumed it. Top-level
`findings` are the advisory bot threads the factory noted on the pull
request without acting on: each with `tone` (`advisory`) and `message`,
the thread's URL and first line; empty when there are none.
`elapsed_ms`, `working_ms`, `agent_ms`, `verify_ms`, `work_started_ms`,
`verify_started_ms`, `time_box_ms` and `ticket_url` are as `/status`
carries them, except that a run's clocks stop at `ended_ms` once it has
ended. `approved_at` (epoch milliseconds) and `approved_by` (the
operator's login) record the approval (`--approve`) that released a
parked candidate to merge; both are null for a run no one approved, and a
`--requeue` clears them.
`events` is the `narrative` level of the run's event stream, oldest
first; `detail` events and their payloads are not served. An `N` that is
not an integer is 400; an integer with no run behind it is 404 carrying
`run`. Leading zeros are ignored, so `/runs/007` is run 7. An integer no
run can have (negative, or wider than SQLite's 64-bit INTEGER, however
long) is 404 with `run` echoing the path segment as typed. `host` passes
through `[report] host_label`. `commit_url` and `pr_url` are as
`/shipped` carries them: the merge commit's page on `origin` and the
pull request the run opened, each null when there is none.

`chain` is the ticket's chain of runs up to run N, as `/shipped` defines
it: every run of the ticket after its last merged run before N, through
N. `run`, `rounds`, `events` and `findings` describe run N alone.
`chain`'s `started_ms` is the first run's start and `elapsed_ms` runs from
it to run N's end (or to `now` while N is live); `working_ms`, `agent_ms`
and `verify_ms` are summed over the chain's runs, each null only when no
run of the chain had its work measured. `chain.runs` lists the runs
oldest first, run N last, each with its own `id`, `attempt`, `outcome`
(null while live), `phase`, `started_ms`, `ended_ms`, its clocks as `run`
carries them, `max_rounds`, `turn_count` (the turns `/runs/ID/turns`
lists) and `rounds`, each round's `round`, `verdict` and
`reviewer_model`. A run with no earlier run in its chain has a chain of
itself alone, whose clocks equal `run`'s.

## `GET /runs/N/files`

```json
{"run": 52,
 "base": "038e4e513e5a4e8367b69a36d73ecc8fd0e12366",
 "head": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
 "files": [
  {"path": "docs/reference/http.md", "status": "M", "added": 31, "deleted": 2},
  {"path": "holophyte/files.py", "status": "A", "added": 168, "deleted": 0},
  {"path": "holophyte/serve/server.py", "status": "M", "added": 62, "deleted": 14}
 ],
 "total_added": 261, "total_deleted": 16, "truncated": false}
```

The files a run touched, read from git: what the console's "files
touched" panel shows under a run. The store holds only the run's branch,
recorded the moment the loop cuts its worktree, and, once it landed, its
merge commit; the daemon resolves those to a commit range and runs `git
diff --numstat` and `git diff --name-status` over it, each under a
timeout, so the answer is what git says today, not a snapshot. For a
merged run (a recorded `merge_sha`) the range is the merge commit's
first parent to the merge commit in the project's checkout: exactly what
the `--no-ff` landing added to main, whether or not the branch still
exists. For a live run, one whose branch still has its worktree beside
the project, the diff is taken inside that worktree from the merge base
of its HEAD with the newer of `main` and `origin/main` to the working
tree: commits and uncommitted edits together, untracked files listed as
added, so the panel fills in as the implementer works, and a worktree
with nothing changed yet answers an empty `files` with 200. For a run
whose branch survives without a worktree it is the merge base of the
branch with the newer of `main` and `origin/main` to the branch head in
the checkout: what the branch has that main does not, unaffected by what
main gained since, and not crediting the run with main's commits merged
into the branch from `origin/main` before the local `main` caught up.
With no `origin/main` in the repository the base is the merge base with
`main` alone. The daemon fetches nothing; it reads only the refs already
there. `base` and `head` are the full shas the range resolved to; for a
live run `head` is the worktree's HEAD, and the edits beyond it are in
the counts.

`files` is sorted by path. `status` is `A` (added), `M` (modified), `D`
(deleted) or `R` (renamed, listed under the new path); a binary file
counts as 0 added and 0 deleted. At most 200 files are listed;
`truncated` is true when the diff named more, and `total_added` and
`total_deleted` still sum the whole diff, so a truncated list still says
how big the run was.

`N` parses as on `/runs/N`: a non-integer is 400, an integer with no run
is 404 carrying `run`. 409 with an `error` when no range can be found:
the run recorded neither a branch nor a merge sha, or the branch it
recorded has neither a worktree nor a ref (a preserved branch deleted by
hand; the error names the branch).
504 when git does not answer within its cap. The endpoint serves no file
contents or diff hunks and writes nothing to the repository.

## `GET /runs/N/merge`

```json
{"run": 52, "ticket": "KO-219",
 "pr_url": "https://github.com/example/repo/pull/31",
 "head_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
 "ready": false, "reason": "checks_pending",
 "detail": "the required checks are pending: vitest",
 "facts": [
  {"name": "parked", "ok": true, "detail": "run 52 is KO-219's newest run, parked awaiting_merge_approval on https://github.com/example/repo/pull/31"},
  {"name": "human_approval", "ok": true, "detail": "[merge] approve is \"human\""},
  {"name": "ready_for_review", "ok": true, "detail": "the pull request is ready for review"},
  {"name": "review_approved", "ok": true, "detail": "GitHub's review decision is APPROVED"},
  {"name": "checks_passed", "ok": false, "detail": "the required checks are pending: vitest"},
  {"name": "mergeable", "ok": true, "detail": "GitHub's mergeable is MERGEABLE"},
  {"name": "threads_resolved", "ok": true, "detail": "no review thread is open"},
  {"name": "head_unchanged", "ok": true, "detail": "origin's task/ko-219 and the pull request are at 5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f"}
 ]}
```

Whether a run parked for a human's merge may be merged now: what the
console reads to draw a Merge button or say why not. The same answer
`POST /actions/merge` ([The daemon's actions](daemon.md#post-actionsmerge))
computes again at call time before it releases anything; nothing is
cached. `ready` is true when every fact holds. `facts` is always these
eight, in this order, each its `name`, whether it holds (`ok`) and a
`detail` in words:

| `name` | Holds when |
| --- | --- |
| `parked` | the run is its ticket's newest run, in phase `awaiting_merge_approval`, the ticket `blocked_on_operator` with no live run, and the run recorded a pull request and a branch |
| `human_approval` | `[merge] approve` is `"human"` |
| `ready_for_review` | the pull request is not a draft (`isDraft: false`); a draft fails with reason `draft` |
| `review_approved` | GitHub's `reviewDecision` is `APPROVED`; a bypassable review (below) stays `ok: false` |
| `checks_passed` | the required checks fold to success, as the babysitter folds them; a check run that started before the pull request's latest ready-for-review event is not counted, so a required check whose only run is from the draft reads missing |
| `mergeable` | GitHub's `mergeable` is `MERGEABLE` |
| `threads_resolved` | no review thread is open, as the babysitter counts them |
| `head_unchanged` | the branch head on `origin`, read with `git ls-remote` from the project's checkout, and the pull request's `headRefOid` are the same commit, and it is the run's `candidateSha` or its `approvedSha` |

When `parked` or `human_approval` fails the daemon asks GitHub nothing:
the six GitHub facts are `ok: false` with a `detail` saying they were
not read. `reason` is null when `ready`, else the first failing fact's
reason (a `REVIEW_REQUIRED` review only when no other fact fails), one of `not_parked`, `not_human_approval`,
`draft`, `review_not_approved`, `review_bypassable`, `checks_pending`,
`checks_failing`, `conflicting`, `mergeable_unknown` (GitHub has not
computed `mergeable` yet), `threads_unresolved`, `head_moved` and
`github_unreadable` (a GitHub read or the `ls-remote` failed); `detail`
is that fact's `detail`. A repository whose rules require no review has a
null `reviewDecision`: `review_not_approved`. `head_sha` is `origin`'s
branch head, null when it was not read. `ticket` and `pr_url` are the
run's.

`review_bypassable` is a `REVIEW_REQUIRED` review the host's GitHub user
may bypass. On that decision, and only once every other fact holds, the
daemon reads main's rules
(`GET repos/OWNER/REPO/rules/branches/main`) and, for each `pull_request`
rule that asks for a review (one or more approving reviews, a code
owner's review, approval of the most recent push, or a non-empty
`required_reviewers`), its ruleset (`GET repos/OWNER/REPO/rulesets/ID`).
When every such ruleset answers `current_user_can_bypass` as `always`,
`pull_requests_only` or `exempt`, the `review_approved` fact's reason is
`review_bypassable` and its `detail` names each ruleset, the reviews it
asks for and the bypass value:

```
GitHub's review decision is REVIEW_REQUIRED; the host's GitHub user may bypass it: ruleset human-review asks 1 approving review, current_user_can_bypass pull_requests_only
```

While any other fact fails the bypass is not read and that fact is the
answer's `reason`, so checks, conflicts, open threads and a moved head
read as themselves before a `REVIEW_REQUIRED` review, bypassable or not.
The bypass fails closed: an unreadable rules or ruleset answer, a rule or
value that cannot be made out, a ruleset answering `never` or anything
else, or no
ruleset asking for a review (classic branch protection) keeps
`review_not_approved`, its `detail` ending `no bypass: ` and what stopped
it. A `CHANGES_REQUESTED` review is never bypassable.

`N` parses as on `/runs/N`: a non-integer is 400, an integer with no run
is 404 carrying `run`. The route writes nothing; a GitHub read that fails
is `github_unreadable` with 200.

## `GET /runs/N/asks`

```json
{"run": 53, "ticket": "KO-219",
 "pr_url": "https://github.com/example/repo/pull/31",
 "asks": [
  {"id": 812, "question": "Why is the guest keyed by name?",
   "author": "maintainer", "asked_ms": 1788451661675,
   "answered_ms": 1788451781675,
   "url": "https://github.com/example/repo/pull/31#issuecomment-901",
   "answer": "Names are unique per party: src/app.py:30."},
  {"id": 840, "question": "Can a guest be renamed?", "author": "reviewer",
   "asked_ms": 1788452021675, "answered_ms": null, "url": null,
   "answer": null}
 ]}
```

The questions asked about run N's pull request from the console
([`POST /actions/ask`](daemon.md#post-actionsask)), oldest first. `ticket`
and `pr_url` are run N's. `asks` holds every console ask recorded on any
run of N's ticket with N's pull request URL, so the run the loop re-parks
after answering lists the asks made on the run before it. Each ask is its
event `id`, its `question` and `author`, and `asked_ms`, when it was
recorded. Once the babysitter has posted its answer on the pull request,
`answered_ms` is when, `url` the posted comment's `html_url` (null if
GitHub answered none) and `answer` the answer text as posted, redacted;
until then the three are null. A run with no pull request answers an empty
list. `N` parses as on `/runs/N`: a non-integer is 400, an integer with no
run is 404 carrying `run`. The route writes nothing and asks GitHub
nothing.

## `GET /runs/N/ledger`

```json
{"run_id": 52, "ticket": "KO-219",
 "entries": [
  {"at": 1788450941675, "kind": "round",
   "text": "Round 1: changes_requested · reviewer reviewer-model · verify passed",
   "source": "loop"},
  {"at": 1788451181675, "kind": "round",
   "text": "Round 2: pass · reviewer reviewer-model", "source": "loop"},
  {"at": 1788451661675, "kind": "merge",
   "text": "MERGED to main as 5acc138.", "source": "loop"}
 ]}
```

One run's ledger: the narrative the store holds for it (design note 9),
which is what a frontend reads and what the Linear comments and the
`FINDINGS.md` window are projections of. `entries` is oldest first, each
its `at` in epoch milliseconds, its `kind` (`round`, `merge`, `failure`,
`adjudication`, `intervention` or `note`), its `text` and its `source`
(`loop` for an entry the factory wrote, `operator` for one recorded out of
band). An `intervention` entry carries two more fields, `cleared` and
`waited_ms`: what the operator's step cleared and how long that had
waited, computed here because the store knows both and a page that has
lost the item cannot (design note 13's "how long it waited and who
cleared it"). One closed rule, against the entry's own run: two marks are
read, the `at` of the run's newest `redirect` intervention strictly
before the entry (the ask) and the run's `endedAt` when set and strictly
before the entry (the failure); the newer mark wins, and a redirect in
the same millisecond as the failure is not newer, so `cleared` is
`"question"` or `"failed"` and `waited_ms` is the entry's `at` minus that
mark. A mark after the entry never counts, and a `redirect` entry never
pairs with itself since its own row is not strictly before it. With no
mark both are `null`. Entries of other kinds do not carry the fields.

```json
{"at": 1788452000000, "kind": "intervention",
 "text": "human resume: answered: keep the flag name", "source": "operator",
 "cleared": "question", "waited_ms": 818325}
```

A run with no entries answers an empty list. `N` parses as on
`/runs/N`: a non-integer is 400, an integer with no run is 404 carrying
`run`. Nothing here is rendered; the endpoint serves the rows.

## `GET /ledger?since=MS`

```json
{"since": 1788450000000, "limit": 200,
 "entries": [
  {"at": 1788451661675, "run": 52, "ticket": "KO-219", "kind": "merge",
   "source": "loop", "text": "MERGED to main as 5acc138."},
  {"at": 1788451181675, "run": 50, "ticket": "KO-217", "kind": "intervention",
   "source": "operator", "text": "answered: keep the flag name",
   "cleared": "question", "waited_ms": 818325},
  {"at": 1788450941675, "run": 52, "ticket": "KO-219", "kind": "round",
   "source": "loop", "text": "Round 1: changes_requested · reviewer reviewer-model · verify passed"}
 ]}
```

The ledger across runs, newest first, from `since` on: the console's
"resolved today" fold is one window over the store's ledger table, and the
thread under a blocked ticket's question is the same window narrowed to
that ticket. `since` is required and is epoch milliseconds; entries at or
after it come back, ordered by `at` then id, newest first. `kind` narrows
to one of the kinds `/runs/N/ledger` names; `ticket` narrows to one
identifier (`KO-n`). `limit` defaults to 200 and is capped at 1000; there
is no paging past it, a day of ledger fits in one page. Each entry is its
`at`, `run`, `ticket`, `kind`, `source` and `text`, as `/runs/N/ledger`
spells them; an `intervention` entry carries `cleared` and `waited_ms`
too, by the rule `/runs/N/ledger` states, so the fold reads each
resolution's wait off the wire. For a thread, `/attention`'s blocked item is the question
and `/ledger?ticket=KO-n&since=ASKED` is the rest: the `intervention`
rows carry the operator's answer text. A missing or non-integer `since`,
a bad `limit` or an unknown `kind` is 400 with an `error` naming the
parameter.

The separate `active_outages` array contains one `route_down` row per project
with an uncleared launch backoff. These ongoing outages are independent of
`since` and `limit`, so an outage begun before midnight remains visible even
when `entries` fills its historical limit. Each outage carries `project`, `at`
(the outage's start in epoch milliseconds), `reason`, `text`, `kind`, `source`,
and null `run` and `ticket`. The array is empty for ticket-filtered requests
or kinds other than `intervention`; it is included for unfiltered requests.
Launch-loop interventions at or after an ongoing outage's start are suppressed
from history.

## `GET /report?since=SPAN`

```json
{"project": "/home/op/repo",
 "window": {"since": "7d", "from_ms": 1788000000000, "now_ms": 1788604800000},
 "shipped": {"merged": 3, "abandoned": 1, "failed": 2,
             "median_min": 16.0, "median_estimate_min": 30.0},
 "failures": {"verify": 1, "review": 1},
 "gaps": {"open": 2, "layers": {"none": 2, "test": 1},
          "found_by": {"review": 3}},
 "hands_on": {"interventions": 4, "by_action": {"requeue": 3, "pause": 1},
              "send_backs": 1},
 "runs": [
  {"run": 312, "ticket": "KO-241", "actual_min": 8.4, "agent_min": 6.1,
   "verify_min": 2.3, "estimate_min": 10.0, "ratio": 0.84, "rounds": 1,
   "outcome": "merged", "host": "writer-1", "ended_ms": 1788478953000,
   "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f", "wall_min": 9.0}
 ],
 "notes": [
  {"run": 310, "ticket": "KO-240", "round": 2, "event_id": 881,
   "author": "maintainer", "note": "name the window in the header",
   "consumed_ms": 1788470000000}
 ],
 "steers": [
  {"id": 4, "ticket": "KO-242", "kind": "hint", "author": "maintainer",
   "note": "the port is in config.toml", "steered_ms": 1788480000000,
   "run": null, "event_id": null, "consumed_by": 315,
   "consumed_ms": 1788481000000}
 ]}
```

`holo report --json`, the weekly report, as the daemon reads it from the
store, so `holo report` runs over `transport = "http"`. `since` is the
window: `Nh`, `Nd` or `all`, `7d` when absent; any other form is 400
naming the forms. `project` is the repository the daemon serves, as a
path. `window` echoes `since` and gives the window's start, `from_ms`
(null for `all`), and the read's moment, `now_ms`, both epoch
milliseconds. `shipped` counts the runs that ended in the window by
outcome, `merged`, `abandoned` and `failed`, with `median_min`, the
median actual minutes of the merged runs, and `median_estimate_min`, the
median of their estimates; both null with no merged run. `failures`
counts the window's failed runs by failure kind, most first. `gaps` is the
store's whole gap ledger, not the window's: `open`, the gaps with no
layer yet, `layers`, the count per layer, and `found_by`, the count per
finder. `hands_on` counts the window's interventions: `interventions`,
the total less send-backs, `by_action`, the count per action, and
`send_backs`, the `operator_note` rows. `runs` are the window's ended
runs, each as a `/runs` row without `ticket_url`: `agent_min` and
`verify_min` are its agent's and its verify's minutes, and `wall_min` its
wall-clock minutes.
`notes` are the send-back notes a run consumed in the window, newest
first: the `run` and `ticket`, the `round` that consumed it, the
`event_id` of the `operator_note` event, its `author` and `note`, and
`consumed_ms`. `steers` are the `holo steer` notes recorded in the window
or still pending, oldest first: the steer note's `id`, its `ticket`, `kind`
(`amendment` or `hint`), `author` and `note`, `steered_ms`, the `run` it
was recorded on and, for a parked run's send-back, its `event_id`, both
null otherwise; `consumed_by` is the run that first carried it and
`consumed_ms` when, both null while it is pending. `holo report` prints its
notes and steers only with `--notes`; the body always holds them.

## `GET /attention`

What needs the operator, computed where the store is:

```json
{"level": "attention", "now": 1788450534491,
 "project": "/path/to/repo", "items": [
  {"kind": "blocked", "ticket": "KO-n", "question": "…", "run": 50, "asked_ms": 1788449000000,
   "pr_url": null, "level": "attention"},
  {"kind": "pr_open", "ticket": "KO-n", "title": "…", "run": 53,
   "pr_url": "https://github.com/example/repo/pull/2170", "reason": "…", "asked_ms": 1788449000000,
   "pr": {"number": 2170, "checks": "success", "review": "approved", "threads": 2, "title": "…"},
   "level": "attention"},
  {"kind": "stale_run", "run": 52, "ticket": "KO-n", "phase": "working", "heartbeat_age_ms": 400000,
   "pr_url": null, "level": "attention"},
  {"kind": "failed", "run": 51, "ticket": "KO-n", "reason": "…", "ended_ms": 1788450000000, "attempt": 2,
   "pr_url": null, "triage": {"choice": "infra", "confidence": 0.92, "backend": "claude",
   "model": "sonnet", "requeued": true}, "level": "attention"},
  {"kind": "supervisor", "state": "stale", "heartbeat_age_ms": 1200000, "level": "attention"}
]}
```

A `blocked` item's `run` is the run parked for the ticket and `asked_ms`
when the question was asked: the newest `redirect` intervention on that
run, else the run's last heartbeat (both null only for a ticket parked
with no run behind it). A `pr_open` item is a `blocked_on_operator`
ticket whose parked run has a `pr_url` and whose recorded park kind
(`runs.parkKind`, one of `store/enums.py` `ParkKind`) is `pull_request`,
as a park under `[merge] mode = "pr"` records it, or `ci`, as a babysit
waiting only on pending checks or the quiet period records it; the
question's wording plays no part. The run waits on a review, its checks
or a merge, not on an answer,
so the item carries `pr_url` and `reason` (the question with its first
line removed, or the whole question when it has one line) in place of
`question`; its `run` and `asked_ms` are as on `blocked`. Its `pr` is
the pull request as the loop's reconcile last read it: `number` from the
URL, `checks` (`success`, `pending`, `failure`, null for a PR with no
checks), `review` (GitHub's review decision lower-cased: `approved`,
`changes_requested`, `review_required`, null when none is required) and
`threads`, the review-thread count, `open_threads`, the count of those
unresolved (the string `"100+"` when the read that counted it saw more
threads than the first page of 100 it counts over,
`runs.prSeenOpenThreadsFloor`), and `title`, the pull
request's title (`runs.prSeenChecks`, `prSeenReview`, `prSeenThreads`,
`prSeenOpenThreads`, `prSeenTitle`); all five facts are null for a run
never polled, and `open_threads` for one polled before it was recorded. The
item's own `title` is the ticket's title, which the console shows when
`pr.title` is null. A `failed` item's `attempt` is the run's 1-based
attempt number. Its `triage` is the run's cause question under
`[questions.failures]` (see `docs/config.md`): `choice` (`infra`, `code` or
`spec`), `confidence`, the `backend` and `model` asked, and `requeued`, true
when the factory requeued the ticket on that answer; `choice` and
`confidence` are null when the question failed, and `triage` is null for a
run never asked. Every item that names a `run` carries its `pr_url`: the
pull request the run opened under `[merge] mode = "pr"` (`runs.prUrl`),
null when it opened none, so a console can link the parked question to
the PR it waits on. `project` is the project path, as on `/status`.

`level` is `none`, `working`, `attention` or `critical`; with no items it
is `working` if any run is live. Items come in this order: `blocked` and
`pr_open` tickets in ticket order, `stale_run`, `failed` within the last 24
hours while it is its ticket's latest attempt and the ticket has neither
merged nor started a new one (a requeue to `ready` keeps it), or at any
age while it strands its ticket (its last run, the ticket `in_flight` with
no active run, which only the operator will move on; a requeue ends this
unbounded stay, not the 24 hours), oldest end first,
`supervisor` when not live. A daemon older than this endpoint answers 404, and the
drawer then computes the stale-run and supervisor rows itself from
`/status`; any other failure of `/attention` is shown, never hidden.

## `GET /board`

The open tickets by state, as the store mirrors them: the console's Board
view, Linear's columns without a call to Linear.

```json
{"now": 1788450534491, "editable": false, "columns": [
  {"state": "needs_spec", "tickets": []},
  {"state": "blocked_on_deps", "tickets": [
    {"ticket": "KO-n", "title": "…", "time_box_ms": 1500000, "run": null,
     "question": null, "waits_on": ["KO-m"], "mirrored_ms": 1788449000000,
     "column": "ready", "priority": 2, "labels": ["ui"], "revision": 3}]},
  {"state": "ready", "tickets": []},
  {"state": "blocked_on_operator", "tickets": []},
  {"state": "in_flight", "tickets": [
    {"ticket": "KO-m", "title": "…", "time_box_ms": 1500000, "run": 52,
     "question": null, "waits_on": [], "mirrored_ms": 1788449000000,
     "column": null, "priority": null, "labels": [], "revision": 1}]}
]}
```

`columns` is one entry per open state in the path-to-merge order
`needs_spec`, `blocked_on_deps`, `ready`, `blocked_on_operator`,
`in_flight`, every column present even when empty; `merged` and
`abandoned` tickets are absent. Tickets within a column are ordered by
identifier. `run` is the ticket's active run, null when none is working
it; `question` is what a `blocked_on_operator` ticket asks, null
otherwise; `waits_on` is the identifiers of the open tickets its Linear
dependencies name, empty when none (a dependency the store has never
mirrored is shown by its Linear issue id, since the store cannot name
what it has not seen); `mirrored_ms` is when the store last mirrored the
ticket. `column` (`backlog`, `ready`, `canceled`, null when the board
named none), `priority` (null when none), `labels` and `revision` are the
ticket's board-owned fields as the store holds them; `revision` is the one
a revision-checked edit sends as `If-Match`, 0 when none is recorded.

A native project (`[board] kind = "native"`) answers a `backlog` column
first, before the five above: its tickets idle in `needs_spec`,
`blocked_on_deps` or `ready` whose `column` is `backlog`. A ticket in
`in_flight` or `blocked_on_operator` stays under its status whatever its
column, and a Linear project has no `backlog` column. `editable` is true
only when a host daemon with `[serve] actions` on answers for a native
project; a project daemon, or a Linear project, answers false.

The board is the store's mirror and nothing more, and the endpoint
never calls the provider. The loop mirrors every ready issue the provider
lists at each claim, a valid body to `ready` and one failing the template
to `needs_spec`, so the queue is on the board before the loop starts on
it; a ticket the loop could not claim (Backlog, closed, blocked in Linear)
is not.

## `GET /tickets/KO-n`

One mirrored ticket by its Linear identifier, body included: what the
console shows when the operator opens a ticket without leaving for Linear.

```json
{"ticket": "KO-n", "title": "…", "status": "in_flight",
 "body": "# The store mirrors a ticket's body…\n\n## Summary\n…",
 "acceptance_criteria": ["Given …, when …, then …"],
 "verification_commands": ["ruff check ."],
 "time_box_ms": 1800000, "run": 52, "mirrored_ms": 1788449000000}
```

`ticket` is the identifier as the store mirrors it; `status` is the
store's status, any of the seven including `merged` and `abandoned`;
`body` is the issue text the loop last read at claim time, served as the
text Linear held then and not rendered, so it is the exact contract the
run worked from, which Linear's current text may no longer be (empty for
a ticket mirrored before the store kept bodies); `acceptance_criteria`
and `verification_commands` are the lists the mirror parsed from it;
`run` is the ticket's active run, null when none is working it;
`mirrored_ms` is when the store last mirrored the ticket. An identifier
the store has never mirrored is 404 with an empty object, like an absent
run. The endpoint reads the store's mirror and nothing more; it never
calls the provider.

## `POST /actions/steer`

Opened by `[serve] actions = true` ([The daemon's actions](daemon.md)),
behind its token. It does what `holo steer KEY NOTE` does: the store's one
steer, routed by the ticket's state, so a run parked on its pull request
goes back to the babysitter with the note, a live run's next implementer
turn or babysitter fix round reads it, and a ticket with no run in flight
carries it into its next run. The console's Steer box posts it from a live
run's detail card and from a parked pull request row; `holo steer` posts
it under `transport = "http"`.

```json
{"ticket": "KO-n", "note": "remove the subheader", "hint": false,
 "now": false, "author": "maintainer"}
```

`ticket` (required) is the Linear identifier; `note` is the steer;
`hint` (default `false`) makes it one turn's hint rather than a contract
amendment; `now` (default `false`) is `--now`, stopping a live run's
implementer turn to resume its session with the note; `author` defaults
to `maintainer`.

```json
{"action": "steer", "ok": true, "ticket": "KO-n", "recorded": 42,
 "detail": "KO-n steered: run 52 sent back to the babysitter as a maintainer instruction (operator_note event 7, steer note 3)"}
```

`detail` is the line `holo steer` prints; `recorded` is the interventions
row the steer wrote, `steer` or, for a parked run, `operator_note`. A
blank note, a ticket the store never mirrored or holds twice, and every
refusal the store makes for the ticket's state are 200 with `ok: false`
and the reason in `detail`, and nothing is written. A missing `ticket`,
or a `hint` or `now` that is not a boolean, is 400; a project with no
store is 503.

## `GET /peers`

Where the other daemons are, so the page can fan out from whichever
daemon it was loaded from:

```json
{"self": "127.0.0.1:7710", "peers": ["writer-2:7710", "writer-3:7710"]}
```

`self` is the address this daemon bound, `HOST:PORT` as `--serve`
announced it, not the machine's name. `peers` is the project's `[console]
daemons` list (see `docs/config.md`) in its configured order, empty when
the table is absent: an empty list, never an error. The daemon answers
from config and never contacts a peer; it needs no store, so a project
with none still answers 200 here. A host daemon answers the same shape at its root from
`host.toml`'s `[console] daemons`.

## The host daemon

`factory.py --serve` with no project serves every project in the host
registry, `HOLOPHYTE_HOME/host.toml` ([Operating](../operating.md#the-host-registry)).
Each route of this page answers under the project's prefix,
`/projects/NAME/...`, with the body a project daemon answers at its root:
`/projects/holophyte/status`, `/projects/holophyte/runs/52`,
`/projects/holophyte/config`, `/projects/holophyte/actions/requeue`.
`NAME` is the project's `[serve] name`, percent-decoded (a client encodes a
name that holds a space). It resolves through the registry alone, re-read
when `host.toml` changes: a name outside it is 404, naming the registry,
before any file of any project is opened, and a project `project remove`
dropped stops answering at the next request.

One project's failure is that project's answer. Before every project route
the daemon checks the store's schema stamp, read-only. A store stamped
newer than the daemon's build still answers when its `readableFrom` floor
is at or below the daemon's schema version (an additive bump); one this
build cannot read, a floor above it or none, is 503 naming the versions
(and makes the daemon look at the checkout's `HEAD` at once, so a daemon
whose checkout moved leaves for the new code). A locked or corrupt store is 503 with its
`error`, in a read or in an action's write; `/projects/NAME/status` for a
store with no row for the project's path is 503 naming `factory.py project
add` (the other routes still answer, and a `hold` may write the row); any
other failure is 500. Every read waits at most one second for a store's
lock (`HOST_READ_WAIT_S` in `holophyte/serve/serve_host.py`, through
`store.read.lock_wait()`), so a locked store answers 503 inside the
drawer's two-second limit. The root lists each such project with its
`error` and the others whole.

The bound has a cost a project daemon's thirty-second wait does not: a
store whose write lock is held past that second, even briefly, answers 503
`database is locked` for that poll, and the root carries it as the
project's `error` and a `project_error` item on `/attention`. The tray and
the drawer can show a needs-you row for that one poll; it clears at the
next. A row that stays across polls is a store that stays locked.

The root answers `GET /status` and `GET /attention` for the host, below,
`GET /peers` from `host.toml`'s `[console] daemons`, `/` and the console's
files, and `POST /actions/run-sweep` ([The daemon's
actions](daemon.md#post-actionsrun-sweep)). Any other JSON path at the root
is 404, naming the `/projects/NAME` prefix. Tokens are in
[Authentication](#authentication).

## Host `GET /status`

```json
{
  "now": 1788450534491,
  "daemon": {"started_ms": 1788446934491, "pid": 2801590},
  "build": {"daemon": "abc1234…", "sweep": "abc1234…", "head": "abc1234…"},
  "console": {"served": "def5678…", "tree": "0a1b2c3…", "stale": true,
              "reason": "`bun install --frozen-lockfile` did not start: …",
              "failed_ms": 1788446935100, "tried": "0a1b2c3…"},
  "sweep": {"started": 1788450500000, "ended": 1788450512000,
            "revision": "abc1234…", "pid": 2801700, "exit": 0,
            "projects": {"holophyte": "ok", "lotuspod": "skipped: disabled"},
            "error": null, "state": "fresh"},
  "actions": true,
  "projects": [
    {"name": "holophyte", "path": "/path/to/holophyte",
     "store": "/home/op/.holophyte/holophyte-HASH/store.db", "error": null,
     "host": "writer-1", "schema_version": 41, "admission": "enabled",
     "hold_note": null, "project_row": 1,
     "supervisor": {"state": "live", "pid": 0, "heartbeat_age_ms": 21000,
                    "host": "writer-1"},
     "runs": [{"id": 52, "ticket": "KO-219", "phase": "working",
               "heartbeat_age_ms": 71989}],
     "workers_on_previous_build": 0}
  ]
}
```

`now` is the daemon's clock, epoch milliseconds, as on a project's
`/status`; `daemon` is its `started_ms` and `pid`. `build` names three
builds side by side: `daemon`, the checkout's revision when this daemon
started (null when it runs from no git checkout); `sweep`, the revision the
last host sweep ran; `head`, the checkout's `HEAD` now. The console flags
any that differ. `actions` is whether `host.toml`'s `[serve] actions` is
on.

`console` is the console build this daemon serves. `served` is the
`console` tree its build is stamped with and `tree` the checkout's
`console` tree now, each null when unknown. `stale` is true when `tree`
is known and differs from `served`. While it is, `reason`, `failed_ms`
(epoch milliseconds) and `tried` (the tree the build started from) are
the last failed startup build's, from `HOLOPHYTE_HOME/console-build.json`;
otherwise, or with no such record, each is null. A successful startup
build removes the record. The console shows a banner while the daemon
that served it reports `stale`.

`sweep` is `HOLOPHYTE_HOME/sweep.json` as the last run wrote it: `started`
and `ended` (epoch milliseconds), `revision`, `pid`, `exit` (0, or 1 when a
project errored or a signal stopped the run) and `projects`, each name's
outcome (`ok`, `skipped: WHY`, `error: WHY`), each null when the file lacks
it; `error` is why the file could not be read, else null. `state` is
`none` with no file, `unreadable`, `running` while a run started within
the sweep unit's 120 s has not ended, `killed` once it is past that with no
`ended`, `fresh` when the last run ended within two `[supervisor]
sweep_sec` intervals, and `stale` after that.

`projects` is every registry entry in registry order. `name` is its
`[serve] name` (null for an entry whose config cannot be read), `path` its
repository and `store` its store file, null when there is none. `error` is
the project's failure as text (a config that cannot be read, a store that
cannot be opened, a schema newer than the daemon's build can read), null
when it answered; the fields below are then null. `host` is the project's
`[report] host_label`, `schema_version` its store's stamp, `admission` and
`hold_note` as on a project's `/status`, and `project_row` the id of the
store's row for this path, null when it has none (a store deleted or
recreated since `project add`). `supervisor` is the store's watcher beat
(`state` `live`, `stale` or `none`, `pid`, `heartbeat_age_ms`, `host`),
stale after two sweep intervals; `pid` 0 is the host sweep's one beat per
store. `runs` is every live run in a sweepable phase with its `id`,
`ticket`, `phase` and `heartbeat_age_ms`, empty for a disabled project.
`workers_on_previous_build` is as on a project's `/status`.

## Host `GET /attention`

Every project's `/attention` items in one list, each item carrying its
`project` name, after the host's own:

```json
{"level": "attention", "now": 1788450534491, "items": [
  {"kind": "sweep_stale", "project": null, "state": "killed",
   "started": 1788450300000, "ended": null, "level": "attention"},
  {"kind": "project_error", "project": "lotuspod", "path": "/path/to/lotuspod",
   "error": "OperationalError: database is locked", "level": "attention"},
  {"kind": "stale_run", "run": 52, "ticket": "KO-219", "ticket_url": null,
   "phase": "working", "heartbeat_age_ms": 1800000, "pr_url": null,
   "level": "attention", "project": "holophyte"}
]}
```

`sweep_stale` comes first when the sweep's `state` is anything but `fresh`
or `running`, with that `state` and the run's `started` and `ended`; its
`project` is null. Then, per project in registry order: `project_error`
for a project the root `/status` shows with an `error`; `no_store` for one
with no store and `no_project_row` for a store with no row for its path,
each with a `detail` naming `factory.py project add`; and the project's own
items as its `/attention` answers them (`kind`, `run`, `ticket`,
`ticket_url`, `phase`, `heartbeat_age_ms`, `pr_url` and the rest, above),
each with `project` added. A project's `supervisor` item judges the host
sweep's beat against two sweep intervals. `level` is `attention` when
there is any item, else `working` when any project has a live run, else
`none`; `now` is the daemon's clock.

## `PUT /tickets/ID`

A host daemon's edit of one ticket on a native board (`[board] kind =
"native"`), at `/projects/NAME/tickets/ID`: the console's Board writing a
ticket's body at the revision it read. A project daemon has no such
route, and a Linear project's board stays read-only on the console.

```
PUT /projects/holophyte/tickets/NAT-1
Authorization: Bearer MACHINE_TOKEN
If-Match: 2
Content-Type: application/json

{"body": "# …\n\n## Summary\n…", "priority": 2, "labels": ["ui"]}
```

```json
{"ticket": "NAT-1", "revision": 3}
```

The gate, in order: the host's machine token on every bind, loopback
included, and never a project's own `token_file`, else 401 with `{}`; host
`[serve] actions` on and the project native, else 404 with nothing
written; `If-Match` a non-negative integer, the `revision` `GET /board`
served, else 428; a JSON object body, else 400. `body` is the ticket's
full text; `priority` (0 to 4) and `labels` (a list of strings) are
optional, and each left out keeps the ticket's own.

The edit is the store's `edit_ticket()`, judged as `--file-ticket` judges
a body and recorded as the ticket's next revision authored `console`,
never an author the body names. It answers 200 with the new `revision`;
409 with `error` and `current`, the ticket's revision now, when it has
moved past `If-Match` (another edit landed; read it again); and 422 with
`problems`, every blocking problem, when the body fails the template for
a ticket outside the `backlog` column or names a ticket the project does
not hold, with nothing written. A store failure is the project's 503 or
500 as a read's is.

## `POST /tickets`

A host daemon's filing of a new ticket on a native board, at
`/projects/NAME/tickets`: the console's New ticket. It passes the gate of
[`PUT /tickets/ID`](#put-ticketsid) but for `If-Match`, which it does not
read: a new ticket has no revision.

```
POST /projects/holophyte/tickets
Authorization: Bearer MACHINE_TOKEN
Content-Type: application/json

{"body": "# …\n\n## Summary\n…", "priority": 2, "column": "ready"}
```

```json
{"ticket": "NAT-1", "revision": 1}
```

`body` is the ticket's full text; `priority` (0 to 4) is optional, and
`column`, `ready` by default or `backlog`, the column it lands in; any
other is 400. The filing is the store's `file_ticket()` under the board's
`[board] prefix`, authored `console`: 201 with the new `ticket` and its first
`revision`, 1; 422 with `problems` and nothing written when a body filed
to `ready` fails the template or `Depends on:` names a ticket the project
does not hold. A body filed to `backlog` with problems is saved as a
draft.

## `POST /tickets/ID/move`

A host daemon's move of a native ticket between Ready and Backlog, at
`/projects/NAME/tickets/ID/move`, behind the whole gate of
[`PUT /tickets/ID`](#put-ticketsid), `If-Match` included.

```
POST /projects/holophyte/tickets/NAT-1/move
Authorization: Bearer MACHINE_TOKEN
If-Match: 1
Content-Type: application/json

{"column": "backlog", "note": "after the release"}
```

```json
{"ticket": "NAT-1", "revision": 2}
```

`column` is `ready` or `backlog`, else 400; `note`, optional, is the
move's note. The move is the store's `move_ticket()`, authored `console`:
200 with the new `revision`; 409 with `error` and `current` when the
ticket has moved past `If-Match`; 422 with `problems` and nothing written
for a canceled or closed ticket, one already in `column`, or a draft
moved to `ready`. A canceled ticket does not move.

## `POST /tickets/ID/cancel`

A host daemon's cancel of a native ticket, at
`/projects/NAME/tickets/ID/cancel`, behind the whole gate of
[`PUT /tickets/ID`](#put-ticketsid), `If-Match` included.

```
POST /projects/holophyte/tickets/NAT-3/cancel
Authorization: Bearer MACHINE_TOKEN
If-Match: 1
Content-Type: application/json

{"note": "wrong scope"}
```

```json
{"ticket": "NAT-3", "revision": 2, "run": 5}
```

`note`, the reason, is required: missing or blank is 400 with nothing
written. The cancel is the store's `cancel_ticket()`, authored `console`:
200 with the new `revision` and `run`, the live run the cancel asked to
abort (its `stopRequested` names an `abort` intervention), null when the
ticket had none; 409 with `current` and 422 with `problems` as a move's.

## `POST /mcp`

The factory's MCP tools over HTTP, for an agent with no shell on the
seat: the [Model Context Protocol](https://modelcontextprotocol.io)'s
Streamable HTTP transport, served by `holo mcp --http [HOST:PORT]`, not by
either daemon. It is a process of its own on the writer host, the MCP
Python SDK's app under uvicorn, on `127.0.0.1:7711` by default or the
address given (the host's private-network address, beside the daemon's
7710); `deploy/holophyte-mcp.service` runs it. The daemon never imports the
SDK, and a host without it keeps its daemon.

```
POST /mcp
Authorization: Bearer MACHINE_TOKEN
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
```

```json
{"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "requeue",
 "description": "put a failed run's ticket back in the queue (holo requeue KEY --json)",
 "inputSchema": {"type": "object", "properties": {"project": {"type": "string",
 "minLength": 1, "description": "..."}, "ticket": {}, "note": {}, "author": {}},
 "required": ["ticket", "note", "author"], "additionalProperties": false},
 "annotations": {"readOnlyHint": false, "destructiveHint": false}}]}}
```

Its tools are the ones `holo mcp` serves on stdio ([`holo mcp`](cli.md#holo)),
with the same schemas, annotations and results, and it adds none. Each
reply is a JSON-RPC 2.0 object, `jsonrpc` and the request's `id` beside
`result` or `error`. A tool list's `result` holds `tools`, each with its
`name`, `description`, `inputSchema` and `annotations`. An `inputSchema` is
a JSON Schema object: `type`, `properties` (each a `type`, `minLength` or
`minimum`, an `enum` and its `default`, and a `description`), `required`
and `additionalProperties`, false. Every tool takes an optional `project`,
a `[serve]` name or a repository path; `report` also takes `since`, `runs`
`limit`, `run` the `run` id and its `view`, `ticket` the `key`, and the
writes their own `ticket`, `run` or `body` and the required `note` and
`author`, `steer` also its `hint` and `now` booleans. `annotations` holds `readOnlyHint`, true for a read, and
`destructiveHint`, false for every tool served today, so a client that
asks a person before a destructive write asks here as on stdio. A tool
call's `result` holds `content`, a list of one object whose `type` is
`text` and whose `text` is the answer; `structuredContent`, the command's
JSON object, when it printed one; and `isError`, true for a refusal. An
unknown tool is the reply's `error`, with `code` -32602 and a `message`
naming the tools.

Each call runs its `holo` command on the host (`HOLO_TRANSPORT=local`) as
a subprocess, as on stdio, and a write records its author as `AUTHOR via
MCP`. The six write tools are listed and callable only when
`host.toml`'s `[serve] actions` is true; otherwise only the reads are
listed and a write is an unknown tool.

The transport is stateless and answers in JSON: a request gets one JSON
reply, a notification 202 with no body, and no `Mcp-Session-Id` is
issued; an unsupported `MCP-Protocol-Version` is 400. No event stream is
offered. Every request, on every bind, loopback included, needs
`host.toml`'s `[serve] machine_token_file` as its bearer, compared in
constant time; a host registry without that key is a startup error naming
it, and nothing listens. A request carrying an `Origin` header is a
browser's and is refused, the DNS-rebinding defence the transport asks
of a server; an MCP client sends none. These answers come before anything
runs:

| Status | When |
| --- | --- |
| 401 | no `Authorization: Bearer` header, another scheme or another value; body `{}` |
| 403 | the right bearer and an `Origin` header; body carries `error` |
| 405 | the right bearer and any method but `POST`, `GET` included; `Allow: POST`, body carries `error` |

Like the daemon, it follows the code: every `CODE_CHECK_SEC` (15 s) it
compares the factory checkout's `HEAD` with the one it started from and
exits 0 on a move, and its unit starts the new code. It stops accepting at
once and waits at most `DRAIN_SEC` (20 s) for requests and tool calls in
flight; a tool call still running then is cut off, its `holo` process
killed so its SQLite transaction rolls back, and its reply is lost.

## Static files

`GET /` answers `console/dist/index.html` and `GET /PATH` answers
`console/dist/PATH` for a regular file under that directory: the
repository's own `console/dist/`, where the renderer's build writes the
console, found from the package rather than the project's checkout. The
JSON routes above, and any added later, take precedence over a file of
the same name. The content type follows the extension:

| Extension | Content-Type |
| --- | --- |
| `.html` | `text/html; charset=utf-8` |
| `.js` | `text/javascript` |
| `.css` | `text/css` |
| `.svg` | `image/svg+xml` |
| `.woff2` | `font/woff2` |
| `.png` | `image/png` |
| `.json`, `.map` | `application/json` |
| `.webmanifest` | `application/manifest+json` |
| anything else | `application/octet-stream` |

Every file answer is `Cache-Control: no-store`; there is no compression,
no other caching header and no range support. A path that resolves outside
the directory (`..`, an encoded `..`, an absolute path, a symlink pointing
out) or names no regular file is the same 404 JSON as an unknown route.
When `console/dist/` does not exist, `/` is 404 JSON whose `detail` says
the console is not built, and the JSON routes answer as before: a daemon
on a host without the renderer's toolchain still serves its JSON.

## Authentication

A daemon bound to anything but loopback runs with `[serve] token_file`
(see `docs/config.md`) and demands its contents on every JSON route:

```
Authorization: Bearer TOKEN
```

With `[serve] machine_token_file` also set, the contents of that file are
accepted as `TOKEN` too, on every route that demands the project's token:
one token for every daemon on the machine, beside the project's own.
Each value is compared whole, in constant time; a missing header, another
scheme or any other value is 401 with the body `{}` and no store access,
and nothing about the attempt is logged. `GET /`, the console's files
under it and `GET /peers` are served without the header, so the page can
load and learn where its peers are before it has a token to present. A
daemon bound to loopback never asks on the read routes: `--serve 7710`
answers them open, token file or not. The opt-in routes are the
exception: `[serve] actions` or `[serve] config_edit` needs
`[serve] token_file` on every bind, loopback included (the daemon
refuses to start without it), and `POST /actions/...`, `GET /config` and
`PUT /config` answer only to the bearer.

A host daemon takes its one token from `host.toml`: `[serve]
machine_token_file` is the bearer at the root and under every
`/projects/NAME` prefix. It is demanded on every read route beyond
loopback, and on every bind for `POST /actions/...` and `/config`; a bind
beyond loopback, `[serve] actions` or any project's `config_edit` without
it is a startup error naming the key. A project's own `[serve] token_file`
is accepted beside it under that project's prefix only, for one release;
it is 401 at the root and under any other prefix. A project's
`machine_token_file` is not read in host mode. The bind judged is the one
the daemon serves on, the handed-over socket's when the service manager
started it.

A page served by one daemon polls the others from the browser, and a
cross-origin GET carrying `Authorization` is not a simple request, nor
is the console's `POST /actions/...` or `PUT` with the bearer, a JSON
body and, for a ticket edit, `If-Match`: the
browser first sends a CORS preflight, `OPTIONS` on the path with
`Access-Control-Request-Headers: authorization` (`authorization,
content-type` for an action, and `if-match` beside them for a ticket
edit). Every daemon answers it on any path with
204, no body, `Access-Control-Allow-Origin: *`,
`Access-Control-Allow-Methods: GET, POST, PUT`, `Access-Control-Allow-Headers:
authorization, accept, content-type, if-match` and `Access-Control-Max-Age: 600`,
token or not: a preflight never carries credentials, so the answer
discloses nothing and touches no store, and the request it clears is
still refused without the bearer. Every other method but GET stays 405, `POST`
included on every path but the `/actions/` routes and `PUT` on every path
but `/config`, both in [The daemon's actions](daemon.md), and a host
daemon's `/tickets/ID` ([`PUT /tickets/ID`](#put-ticketsid)); a host
daemon also takes `POST` on `/tickets` ([`POST /tickets`](#post-tickets)),
`/tickets/ID/move` ([`POST /tickets/ID/move`](#post-ticketsidmove)) and
`/tickets/ID/cancel` ([`POST /tickets/ID/cancel`](#post-ticketsidcancel)),
each behind the machine token alone.

## Errors

| Status | When |
| --- | --- |
| 204 | `OPTIONS` on any path: the CORS preflight, empty, with the `Access-Control-*` headers above |
| 401 | a non-loopback daemon, any route but `/`, its files and `/peers`, without the exact `Authorization: Bearer` value; body `{}`; on a host daemon also a project's own token presented at the root or under another project's prefix |
| 400 | `/runs` with a bad `limit`; `/shipped` with a bad `limit`, `before` or `outcome`; `/ledger` with a missing or non-integer `since`, a bad `limit` or an unknown `kind`; `/report` with a `since` not `Nh`, `Nd` or `all`; `/runs/N`, `/runs/N/files`, `/runs/N/ledger` or `/runs/N/merge` with a non-integer `N` |
| 404 | `/runs/N`, `/runs/N/files`, `/runs/N/ledger` or `/runs/N/merge` with no such run, body carries `run`; `/tickets/KO-n` with no mirrored ticket, body `{}`; any other path with no console file behind it; body carries `path`, and `detail` when the console is not built. On a host daemon also `/projects/NAME/...` for a name outside the registry, and a project route at the root, both before any store is opened |
| 405 | any method but GET and OPTIONS, `POST` outside `/actions/` and, on a host daemon, `/tickets`, `/tickets/ID/move` and `/tickets/ID/cancel`, and `PUT` outside `/config` and, on a host daemon, `/tickets/ID`; `Allow: GET` |
| 409 | `/runs/N/files` for a run with no branch and no merge sha, or whose branch or merge commit is no longer in the repository; `error` names it |
| 503 | the project has no store yet; body carries `error`, `detail` and `project`, the repository the daemon serves, as a path. On a host daemon, under one project's prefix: its store stamped newer than the build can read, locked or corrupt (`error`, `project` its name), or `/status` with no project row for its path (`project_row` null, `detail` naming `project add`); and any route when `host.toml` itself cannot be read |
| 500 | on a host daemon, one project's route failing any other way; body carries `error` (type and message, redacted) and `project`, and the traceback goes to the daemon's log |
| 504 | `/runs/N/files` when git does not answer within its cap |

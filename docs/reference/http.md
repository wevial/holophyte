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
                    "writer": {"command": null}},
  "route_labels": {"implementer": "claude opus", "reviewer": "codex gpt-5.6-sol",
                   "reviewer_fallback": null, "adjudicator": "codex gpt-5.6-sol",
                   "writer": "claude opus"},
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
     "agent_ms": 61200, "verify_ms": 0, "verify_started_ms": null,
     "time_box_ms": 1500000, "round": 0, "strikes": 0, "host": "writer-1",
     "stop_requested": null, "stop_action": null}
  ]
}
```

`runs` lists every live run in a sweepable phase. Ages are computed by the
daemon against its own `now`, so a client compares one number to
`thresholds.heartbeat_stale_ms` and never has to agree with the writer
host about the time. `started_ms` is the run's start as epoch
milliseconds; `round` is the review rounds recorded so far; `strikes` is
the sweep's tally for the run, 0 when it is not under suspicion.
`ticket_url` is the ticket's page on the board (`tickets.url`), null when
the store has none. `elapsed_ms` is wall time since `started_ms`.
`working_ms` is the work the run has recorded plus, while a span of work
is open, the time since that span began, null for a run whose work was
never measured; `work_started_ms` is when the open span began, as epoch
milliseconds, and null while no span is open,
so a client interpolates work between polls only from `work_started_ms`
and never from `elapsed_ms`. `agent_ms` is the part of `working_ms` the
time box is judged against and `verify_ms` the rest, the time spent in the
ticket's verify commands; `agent_ms` is null when `working_ms` is, and
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
dispatches, an unset `writer` the implementer's label, and an unset
`reviewer_fallback` null. `active_routes` holds one entry per seat
(`implementer`, `reviewer`, `adjudicator`, `writer`) naming what the seat
runs now: `command` is the executable alone, never its arguments, null for
a seat left unset in `[agents]`; while a running loop has switched the seat
to its fallback, `command` is the fallback's and the entry also carries
`fallback`, naming it. `workers_on_previous_build` counts the workers a
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
  {"ticket": "KO-241", "actual_min": 8.4, "estimate_min": 10.0, "ratio": 0.84,
   "rounds": 1, "outcome": "merged", "host": "writer-1", "ended_ms": 1788478953000,
   "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f"}
], "limit": null}
```

The `--report` table as JSON, oldest first, the same rows in the same
order the terminal prints. `ended_ms` is the run's end as epoch
milliseconds, which the table does not print; the drawer ages the last
merge from it. `merge_sha` is the full merge commit a merged run landed
on main as, null for any other outcome or a run merged before the store
recorded it. `?limit=N` keeps the first N rows and echoes `limit`; a
non-positive or non-integer limit is 400.

## `GET /shipped?limit=N&before=RUN_ID&outcome=merged`

```json
{"rows": [
  {"id": 312, "ticket": "KO-241", "title": "Run detail: files touched",
   "rounds": 1, "findings": 2, "started_ms": 1788478449000,
   "ended_ms": 1788478953000, "actual_min": 8.4, "estimate_min": 10.0,
   "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
   "commit_url": "https://github.com/example/repo/commit/5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
   "pr_url": "https://github.com/example/repo/pull/2170",
   "host": "writer-1", "outcome": "merged", "outcome_reason": null}
], "next_before": 298, "limit": 50}
```

Finished runs, newest end first, ordered by `ended_ms` descending then
`id` descending. `outcome=merged` (the default) returns only merged runs;
`outcome=all` returns every ended run. Each row includes `outcome` and
`outcome_reason` (the stored reason cut at 400 characters, null when absent).
Any other `outcome` is 400 naming the parameter and its value. The console's
Shipped view scrolls back over it grouped by day; the Board's "shipped
today" is its first page. `findings` is the count of findings over the
run's review rounds. `limit` defaults to 50 and is capped at 200; the
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
   "findings": [{"path": "holophyte/serve.py", "line": 12, "severity": "p1",
                 "criterion": "AC1", "message": "the route is unmatched"},
                {"path": "holophyte/serve.py", "line": 40, "severity": "p1",
                 "criterion": null, "message": "Validate input", "kind": "thread",
                 "author": "review-bot", "author_kind": "bot",
                 "summary": "Validate input", "verdict": "ADDRESS",
                 "raw": "Please validate input",
                 "url": "https://github.com/example/repo/pull/2170#discussion_r1"}],
   "instructions": [{"kind": "instruction", "path": "holophyte/serve.py", "line": null,
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
 ]}
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

## `GET /runs/N/files`

```json
{"run": 52,
 "base": "038e4e513e5a4e8367b69a36d73ecc8fd0e12366",
 "head": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
 "files": [
  {"path": "docs/reference/http.md", "status": "M", "added": 31, "deleted": 2},
  {"path": "holophyte/files.py", "status": "A", "added": 168, "deleted": 0},
  {"path": "holophyte/serve.py", "status": "M", "added": 62, "deleted": 14}
 ],
 "total_added": 261, "total_deleted": 16, "truncated": false}
```

The files a run touched, read from git: what the console's "files
touched" panel shows under a run. The store holds only the run's branch,
recorded the moment the loop cuts its worktree, and, once it landed, its
merge commit; the daemon resolves those to a commit range and runs
`git diff --numstat` and `git diff --name-status` over it, each under a
timeout, so the answer is what git says today, not a snapshot. For a
merged run (a recorded `merge_sha`) the range is the merge commit's first
parent to the merge commit in the project's checkout: exactly what the
`--no-ff` landing added to main, whether or not the branch still exists.
For a live run, one whose branch still has its worktree beside the
project, the diff is taken inside that worktree from the merge base of
`main` and its HEAD to the working tree: commits and uncommitted edits
together, untracked files listed as added, so the panel fills in as the
implementer works, and a worktree with nothing changed yet answers an
empty `files` with 200. For a run whose branch survives without a
worktree it is the merge base of `main` and the branch to the branch head
in the checkout: what the branch has that main does not, unaffected by
what main gained since. `base` and `head` are the full shas the range
resolved to; for a live run `head` is the worktree's HEAD, and the edits
beyond it are in the counts.

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
   "pr_url": null, "level": "attention"},
  {"kind": "supervisor", "state": "stale", "heartbeat_age_ms": 1200000, "level": "attention"}
]}
```

A `blocked` item's `run` is the run parked for the ticket and `asked_ms`
when the question was asked: the newest `redirect` intervention on that
run, else the run's last heartbeat (both null only for a ticket parked
with no run behind it). A `pr_open` item is a `blocked_on_operator`
ticket whose parked run has a `pr_url` and whose recorded park kind
(`runs.parkKind`, one of `store/enums.py` `ParkKind`) is `pull_request`,
as a park under `[merge] mode = "pr"` records it; the question's wording
plays no part. The run waits on a review or a merge, not on an answer,
so the item carries `pr_url` and `reason` (the question with its first
line removed, or the whole question when it has one line) in place of
`question`; its `run` and `asked_ms` are as on `blocked`. Its `pr` is
the pull request as the loop's reconcile last read it: `number` from the
URL, `checks` (`success`, `pending`, `failure`, null for a PR with no
checks), `review` (GitHub's review decision lower-cased: `approved`,
`changes_requested`, `review_required`, null when none is required) and
`threads`, the review-thread count, and `title`, the pull request's
title (`runs.prSeenChecks`, `prSeenReview`, `prSeenThreads`,
`prSeenTitle`); all four facts are null for a run never polled. The
item's own `title` is the ticket's title, which the console shows when
`pr.title` is null. A `failed` item's `attempt` is the run's 1-based
attempt number. Every item that names a `run` carries its `pr_url`: the
pull request the run opened under `[merge] mode = "pr"` (`runs.prUrl`),
null when it opened none, so a console can link the parked question to
the PR it waits on. `project` is the project path, as on `/status`.

`level` is `none`, `working`, `attention` or `critical`; with no items it
is `working` if any run is live. Items come in this order: `blocked` and
`pr_open` tickets in ticket order, `stale_run`, `failed` within the last 24
hours whose ticket has not since merged or been requeued, `supervisor`
when not live. A daemon older than this endpoint answers 404, and the
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
lock (`HOST_READ_WAIT_S` in `holophyte/serve_host.py`, through
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
  "sweep": {"started": 1788450500000, "ended": 1788450512000,
            "revision": "abc1234…", "pid": 2801700, "exit": 0,
            "projects": {"holophyte": "ok", "lotuspod": "skipped: disabled"},
            "error": null, "state": "fresh"},
  "actions": true,
  "projects": [
    {"name": "holophyte", "path": "/path/to/holophyte",
     "store": "/home/op/.holophyte/holophyte-HASH/store.db", "error": null,
     "host": "writer-1", "schema_version": 37, "admission": "enabled",
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
daemon's `/tickets/ID` ([`PUT /tickets/ID`](#put-ticketsid)).

## Errors

| Status | When |
| --- | --- |
| 204 | `OPTIONS` on any path: the CORS preflight, empty, with the `Access-Control-*` headers above |
| 401 | a non-loopback daemon, any route but `/`, its files and `/peers`, without the exact `Authorization: Bearer` value; body `{}`; on a host daemon also a project's own token presented at the root or under another project's prefix |
| 400 | `/runs` with a bad `limit`; `/shipped` with a bad `limit`, `before` or `outcome`; `/ledger` with a missing or non-integer `since`, a bad `limit` or an unknown `kind`; `/runs/N`, `/runs/N/files` or `/runs/N/ledger` with a non-integer `N` |
| 404 | `/runs/N`, `/runs/N/files` or `/runs/N/ledger` with no such run, body carries `run`; `/tickets/KO-n` with no mirrored ticket, body `{}`; any other path with no console file behind it; body carries `path`, and `detail` when the console is not built. On a host daemon also `/projects/NAME/...` for a name outside the registry, and a project route at the root, both before any store is opened |
| 405 | any method but GET and OPTIONS, `POST` outside `/actions/` and `PUT` outside `/config` and, on a host daemon, `/tickets/ID`; `Allow: GET` |
| 409 | `/runs/N/files` for a run with no branch and no merge sha, or whose branch or merge commit is no longer in the repository; `error` names it |
| 503 | the project has no store yet; body carries `error`, `detail` and `project`, the repository the daemon serves, as a path. On a host daemon, under one project's prefix: its store stamped newer than the build can read, locked or corrupt (`error`, `project` its name), or `/status` with no project row for its path (`project_row` null, `detail` naming `project add`); and any route when `host.toml` itself cannot be read |
| 500 | on a host daemon, one project's route failing any other way; body carries `error` (type and message, redacted) and `project`, and the traceback goes to the daemon's log |
| 504 | `/runs/N/files` when git does not answer within its cap |

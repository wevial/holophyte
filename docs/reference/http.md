# HTTP endpoints

`--serve PORT|HOST:PORT` answers the JSON paths below and serves the
console's built files at `/`. Every response carries
`Cache-Control: no-store` and `Access-Control-Allow-Origin: *`; the JSON
ones `Content-Type: application/json`; every request opens the store
read-only and closes it. The open origin is for the console page, which
one daemon serves and which fetches the others from the browser: without
the header the browser refuses a cross-origin answer. The daemon is
read-only; on loopback the bind address is the whole boundary, and beyond
it every JSON route but `/peers` is behind a bearer token
([Authentication](#authentication)). Unknown paths are
404 and any method but GET is 405, both with a JSON `error`, except the
three `POST /actions/...` routes `[serve] actions = true` opens, documented
in [The daemon's actions](daemon.md). A target with no store answers 503.

## `GET /status`

```json
{
  "target": "/path/to/repo",
  "project": "/path/to/repo",
  "host": "writer-1",
  "now": 1788450534491,
  "daemon": {"started_ms": 1788446934491, "pid": 2801590},
  "supervisor": {"state": "live", "pid": 2801613, "heartbeat_age_ms": 8258, "host": "writer-1"},
  "thresholds": {"heartbeat_stale_ms": 300000, "strikes": 2},
  "actions": false,
  "config_edit": false,
  "runs": [
    {"id": 52, "ticket": "KO-219", "title": "The sweep frees a silent lease", "phase": "working",
     "started_ms": 1788450461675, "heartbeat_age_ms": 71989, "elapsed_ms": 72816,
     "time_box_ms": 1500000, "round": 0, "strikes": 0, "host": "writer-1"}
  ]
}
```

`runs` lists every live run in a sweepable phase. Ages are computed by the
daemon against its own `now`, so a client compares one number to
`thresholds.heartbeat_stale_ms` and never has to agree with the writer
host about the time. `started_ms` is the run's start as epoch
milliseconds; `round` is the review rounds recorded so far; `strikes` is
the sweep's tally for the run, 0 when it is not under suspicion.
`supervisor.state` is `live`, `stale` or `none`. `daemon` describes the
serving process: its pid and when it started. `project` is the same
string as `target`, the console's word for it; both are carried for one
release. `actions` is whether `[serve] actions = true` opened the
`POST /actions/...` routes of [The daemon's actions](daemon.md); the
console draws its action buttons disabled while it is `false`.
`config_edit` is whether `[serve] config_edit = true` opened `GET /config`
and `PUT /config`; the console's settings sheet is read-only, naming the
key, while it is `false`. Every `host` passes through `[report] host_label`.

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

## `GET /shipped?limit=N&before=RUN_ID`

```json
{"rows": [
  {"id": 312, "ticket": "KO-241", "title": "Run detail: files touched",
   "rounds": 1, "findings": 2, "started_ms": 1788478449000,
   "ended_ms": 1788478953000, "actual_min": 8.4, "estimate_min": 10.0,
   "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
   "commit_url": "https://github.com/example/repo/commit/5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
   "pr_url": "https://github.com/example/repo/pull/2170",
   "host": "writer-1"}
], "next_before": 298, "limit": 50}
```

The merge ledger, newest end first: only runs whose outcome is `merged`,
ordered by `ended_ms` descending then `id` descending. The console's
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

`commit_url` is the merge commit's page on the target's `origin`:
`https://HOST/OWNER/REPO/commit/SHA` when the `origin` URL is
`https://HOST/OWNER/REPO(.git)` or `git@HOST:OWNER/REPO(.git)` and the
sha is an ancestor of `origin/main` in the target's checkout. It is null
when the row has no `merge_sha`, the target has no `origin`, the remote
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
{"run": {"id": 52, "ticket": "KO-219", "title": "The sweep frees a silent lease",
         "phase": "done", "attempt": 1, "started_ms": 1788450461675,
         "ended_ms": 1788451661675, "outcome": "merged", "time_box_ms": 1500000,
         "branch": "task/ko-219-the-sweep-frees-a-silent-lease", "host": "writer-1",
         "heartbeat_age_ms": null,
         "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
         "commit_url": "https://github.com/example/repo/commit/5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f",
         "pr_url": "https://github.com/example/repo/pull/2170",
         "max_rounds": 2},
 "rounds": [
  {"round": 1, "started_ms": 1788450761675, "ended_ms": 1788450941675,
   "verdict": "changes_requested", "reviewer_model": "reviewer-model",
   "findings": [{"path": "holophyte/serve.py", "line": 12, "severity": "p1",
                 "criterion": "AC1", "message": "the route is unmatched"}]},
  {"round": 2, "started_ms": 1788451061675, "ended_ms": 1788451181675,
   "verdict": "pass", "reviewer_model": "reviewer-model", "findings": []}
 ],
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
parent to the merge commit in the target's checkout: exactly what the
`--no-ff` landing added to main, whether or not the branch still exists.
For a live run, one whose branch still has its worktree beside the
target, the diff is taken inside that worktree from the merge base of
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

## `GET /attention`

What needs the operator, computed where the store is:

```json
{"level": "attention", "now": 1788450534491,
 "target": "/path/to/repo", "project": "/path/to/repo", "items": [
  {"kind": "blocked", "ticket": "KO-n", "question": "…", "run": 50, "asked_ms": 1788449000000,
   "pr_url": null, "level": "attention"},
  {"kind": "pr_open", "ticket": "KO-n", "run": 53, "pr_url": "https://github.com/example/repo/pull/2170",
   "reason": "…", "asked_ms": 1788449000000,
   "pr": {"number": 2170, "checks": "success", "review": "approved", "threads": 2}, "level": "attention"},
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
ticket whose run has a `pr_url` and whose question opens with `PR open:`,
the line a park under `[merge] mode = "pr"` writes first: the run waits
on a review or a merge, not on an answer, so the item carries `pr_url`
and `reason` (the question with that first line removed) in place of
`question`; its `run` and `asked_ms` are as on `blocked`. Its `pr` is
the pull request as the loop's reconcile last read it: `number` from the
URL, `checks` (`success`, `pending`, `failure`, null for a PR with no
checks), `review` (GitHub's review decision lower-cased: `approved`,
`changes_requested`, `review_required`, null when none is required) and
`threads`, the review-thread count (`runs.prSeenChecks`, `prSeenReview`,
`prSeenThreads`); all three facts are null for a run never polled. A `failed` item's `attempt` is the run's 1-based
attempt number. Every item that names a `run` carries its `pr_url`: the
pull request the run opened under `[merge] mode = "pr"` (`runs.prUrl`),
null when it opened none, so a console can link the parked question to
the PR it waits on. `target` and `project` are the target path, as on
`/status`.

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
{"now": 1788450534491, "columns": [
  {"state": "needs_spec", "tickets": []},
  {"state": "blocked_on_deps", "tickets": [
    {"ticket": "KO-n", "title": "…", "time_box_ms": 1500000, "run": null,
     "question": null, "waits_on": ["KO-m"], "mirrored_ms": 1788449000000}]},
  {"state": "ready", "tickets": []},
  {"state": "blocked_on_operator", "tickets": []},
  {"state": "in_flight", "tickets": [
    {"ticket": "KO-m", "title": "…", "time_box_ms": 1500000, "run": 52,
     "question": null, "waits_on": [], "mirrored_ms": 1788449000000}]}
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
ticket. The board is the store's mirror and nothing more, and the endpoint
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
announced it, not the machine's name. `peers` is the target's `[console]
daemons` list (see `docs/config.md`) in its configured order, empty when
the table is absent: an empty list, never an error. The daemon answers
from config and never contacts a peer; it needs no store, so a target
with none still answers 200 here.

## Static files

`GET /` answers `console/dist/index.html` and `GET /PATH` answers
`console/dist/PATH` for a regular file under that directory: the
repository's own `console/dist/`, where the renderer's build writes the
console, found from the package rather than the target's checkout. The
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

The value is compared whole, in constant time; a missing header, another
scheme or any other value is 401 with the body `{}` and no store access,
and nothing about the attempt is logged. `GET /`, the console's files
under it and `GET /peers` are served without the header, so the page can
load and learn where its peers are before it has a token to present. A
daemon bound to loopback never asks: `--serve 7710` answers every route
open, token file or not.

A page served by one daemon polls the others from the browser, and a
cross-origin GET carrying `Authorization` is not a simple request, nor
is the console's `POST /actions/...` with the bearer and a JSON body: the
browser first sends a CORS preflight, `OPTIONS` on the path with
`Access-Control-Request-Headers: authorization` (`authorization,
content-type` for an action). Every daemon answers it on any path with
204, no body, `Access-Control-Allow-Origin: *`,
`Access-Control-Allow-Methods: GET, POST`, `Access-Control-Allow-Headers:
authorization, accept, content-type` and `Access-Control-Max-Age: 600`,
token or not: a preflight never carries credentials, so the answer
discloses nothing and touches no store, and the request it clears is
still refused without the bearer. Every other method but GET stays 405, `POST`
included on every path but the `/actions/` routes of
[The daemon's actions](daemon.md).

## Errors

| Status | When |
| --- | --- |
| 204 | `OPTIONS` on any path: the CORS preflight, empty, with the `Access-Control-*` headers above |
| 401 | a non-loopback daemon, any route but `/`, its files and `/peers`, without the exact `Authorization: Bearer` value; body `{}` |
| 400 | `/runs` with a bad `limit`; `/shipped` with a bad `limit` or `before`; `/ledger` with a missing or non-integer `since`, a bad `limit` or an unknown `kind`; `/runs/N`, `/runs/N/files` or `/runs/N/ledger` with a non-integer `N` |
| 404 | `/runs/N`, `/runs/N/files` or `/runs/N/ledger` with no such run, body carries `run`; `/tickets/KO-n` with no mirrored ticket, body `{}`; any other path with no console file behind it; body carries `path`, and `detail` when the console is not built |
| 405 | any method but GET and OPTIONS, and `POST` outside `/actions/`; `Allow: GET` |
| 409 | `/runs/N/files` for a run with no branch and no merge sha, or whose branch or merge commit is no longer in the repository; `error` names it |
| 503 | the target has no store yet |
| 504 | `/runs/N/files` when git does not answer within its cap |

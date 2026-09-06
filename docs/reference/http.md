# HTTP endpoints

`--serve PORT|HOST:PORT` answers five paths as JSON and serves the
console's built files at `/`. Every response carries
`Cache-Control: no-store`; the JSON ones `Content-Type: application/json`;
every request opens the store read-only and closes it. Unknown paths are
404 and any method but GET is 405, both with a JSON `error`. A target with
no store answers 503.

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
release. Every `host` passes through `[report] host_label`.

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
   "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f", "host": "writer-1"}
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

## `GET /runs/N`

```json
{"run": {"id": 52, "ticket": "KO-219", "title": "The sweep frees a silent lease",
         "phase": "done", "attempt": 1, "started_ms": 1788450461675,
         "ended_ms": 1788451661675, "outcome": "merged", "time_box_ms": 1500000,
         "branch": "task/ko-219-the-sweep-frees-a-silent-lease", "host": "writer-1",
         "heartbeat_age_ms": null,
         "merge_sha": "5acc138e0c2b4d7f9a1e6b3c8d0f2a4e6c8b0d1f", "max_rounds": 2},
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
the loop's review-round cap, so a client can say "round 2 of 3" without
knowing the constant. `rounds` lists the run's review rounds oldest
first, each with its `findings` decoded into objects (`path`, `line`,
`severity`, `criterion`, `message`) rather than the stored JSON string.
`events` is the `narrative` level of the run's event stream, oldest
first; `detail` events and their payloads are not served. An `N` that is
not an integer is 400; an integer with no run behind it is 404 carrying
`run`. Leading zeros are ignored, so `/runs/007` is run 7. An integer no
run can have (negative, or wider than SQLite's 64-bit INTEGER, however
long) is 404 with `run` echoing the path segment as typed. `host` passes
through `[report] host_label`.

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

The files a run touched, read from git in the target's checkout: what
the console's "files touched" panel shows under a run. The store holds
only the run's branch and, once it landed, its merge commit; the daemon
resolves those to a commit range and runs `git diff --numstat` and
`git diff --name-status` over it, each under a timeout, so the answer is
what git says today, not a snapshot. For a merged run (a recorded
`merge_sha`) the range is the merge commit's first parent to the merge
commit: exactly what the `--no-ff` landing added to main, whether or not
the branch still exists. For any other run it is the merge base of
`main` and the run's branch to the branch head: what the branch has that
main does not, unaffected by what main gained since. `base` and `head`
are the full shas the range resolved to.

`files` is sorted by path. `status` is `A` (added), `M` (modified), `D`
(deleted) or `R` (renamed, listed under the new path); a binary file
counts as 0 added and 0 deleted. At most 200 files are listed;
`truncated` is true when the diff named more, and `total_added` and
`total_deleted` still sum the whole diff, so a truncated list still says
how big the run was.

`N` parses as on `/runs/N`: a non-integer is 400, an integer with no run
is 404 carrying `run`. 409 with an `error` when no range can be found:
the run recorded neither a branch nor a merge sha, or the ref it recorded
is gone (a preserved branch deleted by hand; the error names the branch).
504 when git does not answer within its cap. The endpoint serves no file
contents or diff hunks and writes nothing to the repository.

## `GET /attention`

What needs the operator, computed where the store is:

```json
{"level": "attention", "now": 1788450534491,
 "target": "/path/to/repo", "project": "/path/to/repo", "items": [
  {"kind": "blocked", "ticket": "KO-n", "question": "…", "run": 50, "asked_ms": 1788449000000, "level": "attention"},
  {"kind": "stale_run", "run": 52, "ticket": "KO-n", "phase": "working", "heartbeat_age_ms": 400000, "level": "attention"},
  {"kind": "failed", "run": 51, "ticket": "KO-n", "reason": "…", "ended_ms": 1788450000000, "attempt": 2, "level": "attention"},
  {"kind": "supervisor", "state": "stale", "heartbeat_age_ms": 1200000, "level": "attention"}
]}
```

A `blocked` item's `run` is the run parked for the ticket and `asked_ms`
when the question was asked: the newest `redirect` intervention on that
run, else the run's last heartbeat (both null only for a ticket parked
with no run behind it). A `failed` item's `attempt` is the run's 1-based
attempt number. `target` and `project` are the target path, as on
`/status`.

`level` is `none`, `working`, `attention` or `critical`; with no items it
is `working` if any run is live. Items come in this order: `blocked`
tickets with their question, `stale_run`, `failed` within the last 24
hours whose ticket has not since merged or been requeued, `supervisor`
when not live. A daemon older than this endpoint answers 404, and the
drawer then computes the stale-run and supervisor rows itself from
`/status`; any other failure of `/attention` is shown, never hidden.

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
| anything else | `application/octet-stream` |

Every file answer is `Cache-Control: no-store`; there is no compression,
no other caching header and no range support. A path that resolves outside
the directory (`..`, an encoded `..`, an absolute path, a symlink pointing
out) or names no regular file is the same 404 JSON as an unknown route.
When `console/dist/` does not exist, `/` is 404 JSON whose `detail` says
the console is not built, and the JSON routes answer as before: a daemon
on a host without the renderer's toolchain still serves its JSON.

## Errors

| Status | When |
| --- | --- |
| 400 | `/runs` with a bad `limit`; `/shipped` with a bad `limit` or `before`; `/runs/N` or `/runs/N/files` with a non-integer `N` |
| 404 | `/runs/N` or `/runs/N/files` with no such run, body carries `run`; any other path with no console file behind it; body carries `path`, and `detail` when the console is not built |
| 405 | any method but GET; `Allow: GET` |
| 409 | `/runs/N/files` for a run with no branch and no merge sha, or whose branch or merge commit is no longer in the repository; `error` names it |
| 503 | the target has no store yet |
| 504 | `/runs/N/files` when git does not answer within its cap |

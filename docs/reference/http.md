# HTTP endpoints

`--serve HOST:PORT` answers three paths as JSON. Every response carries
`Cache-Control: no-store` and `Content-Type: application/json`; every
request opens the store read-only and closes it. Unknown paths are 404 and
any method but GET is 405, both with a JSON `error`. A target with no
store answers 503.

## `GET /status`

```json
{
  "target": "/path/to/repo",
  "host": "writer-1",
  "now": 1788450534491,
  "supervisor": {"state": "live", "pid": 2801613, "heartbeat_age_ms": 8258, "host": "writer-1"},
  "thresholds": {"heartbeat_stale_ms": 300000, "strikes": 2},
  "runs": [
    {"id": 52, "ticket": "KO-219", "phase": "working",
     "heartbeat_age_ms": 71989, "elapsed_ms": 72816, "time_box_ms": 1500000, "host": "writer-1"}
  ]
}
```

`runs` lists every live run in a sweepable phase. Ages are computed by the
daemon against its own `now`, so a client compares one number to
`thresholds.heartbeat_stale_ms` and never has to agree with the writer
host about the time. `supervisor.state` is `live`, `stale` or `none`.
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

## `GET /attention`

What needs the operator, computed where the store is:

```json
{"level": "attention", "now": 1788450534491, "items": [
  {"kind": "blocked", "ticket": "KO-n", "question": "…", "level": "attention"},
  {"kind": "stale_run", "run": 52, "ticket": "KO-n", "phase": "working", "heartbeat_age_ms": 400000, "level": "attention"},
  {"kind": "failed", "run": 51, "ticket": "KO-n", "reason": "…", "ended_ms": 1788450000000, "level": "attention"},
  {"kind": "supervisor", "state": "stale", "heartbeat_age_ms": 1200000, "level": "attention"}
]}
```

`level` is `none`, `working`, `attention` or `critical`; with no items it
is `working` if any run is live. Items come in this order: `blocked`
tickets with their question, `stale_run`, `failed` within the last 24
hours whose ticket has not since merged or been requeued, `supervisor`
when not live. A daemon older than this endpoint answers 404, and the
drawer then computes the stale-run and supervisor rows itself from
`/status`; any other failure of `/attention` is shown, never hidden.

## Errors

| Status | When |
| --- | --- |
| 400 | `/runs` with a bad `limit` |
| 404 | any other path; body carries `path` |
| 405 | any method but GET; `Allow: GET` |
| 503 | the target has no store yet |

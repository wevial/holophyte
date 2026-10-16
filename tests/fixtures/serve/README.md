These files pin actual daemon responses from the seeded store in
`tests/serve_contract_fixture.py` and are also parsed by the console tests.
`normalize_contract` preserves nulls and all keys, replaces host/path labels
with writer and /repo, IDs and PIDs with 1, and absolute timestamps with NOW;
the builder freezes clocks, so durations remain meaningful and unchanged.
The reviewed run's round recorded one criterion not met, so
`run-detail.json` lists it in `criteria`.
The status fixture includes an unestimated run with no recorded host or
measured work clocks; `run-detail-unestimated.json` pins the same run
with its estimate, host, and work clocks null.
It also lists one queued ticket, approved off its pull request and waiting
for a claim, so its toil counts that approval.
`attention.json` pins the same store's `/attention`: one ticket parked on
its pull request, not yet approved, as a `pr_open` item.
`shipped.json` pins the same store's `/shipped?outcome=all`: the approved
ticket's ended run.
`history.json` pins its `/history` with `since` 100 s before the frozen
clock: the three open runs, newest start first, the working run among
them though it started before `since`; the approved ticket's run ended
before it and is absent.
`ledger.json` pins its `/ledger?since=0`: a round, an operator approval and
the store's migration row, whose build, pid and command become `BUILD`,
1 and `COMMAND`.
`board.json` pins its `/board`, a store-mirror board with no backlog column.
`ticket.json` pins its `/tickets/KO-8`: the unestimated ticket, claimed at
its current revision, with one posted note.
`run-turns.json` pins its `/runs/N/turns` for the reviewed run once it
has recorded an implement session and turn and a fallback review turn,
recorded after the other answers are read; every turn `id` becomes 1.

`host-status.json` and `host-attention.json` pin a host daemon's root
`/status` and `/attention` over two registered projects whose run ids
collide, built in `tests/test_serve_host.py`: paths under the temporary
root become `/root`, a state directory's hash `HASH`, revisions
`REVISION`, pids 1 and host labels `writer`; the clock is fixed.

`run-merge-ready.json` pins `GET /runs/N/merge` for a run parked on an
approved, green, quiet pull request, built in `tests/test_serve_merge.py`:
the branch head's sha becomes `HEAD_SHA`.

`run-asks.json` pins `GET /runs/N/asks` for a run re-parked on its pull
request with one answered and one pending console ask, built in
`tests/test_console_ask.py`: times become `NOW`, nulls stay null.

`run-files.json` pins `GET /runs/N/files` for a live run whose branch
survives without a worktree, in a real repository: one file modified,
added, deleted and renamed off `main`. `run` becomes 1 and the range's
shas `BASE_SHA` and `HEAD_SHA`.

`mcp-tools-list.json` pins `holo mcp --http`'s reply to `tools/list` with
`[serve] actions` true, reads and writes, as served on loopback; nothing in
it depends on the host.

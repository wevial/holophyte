# Runbook

What to do when the factory is stuck, in the order to do it. The rule
behind every recipe: **record before acting**. Every out-of-band state
change gets an `interventions` row, with a truthful action and a real
timestamp, before the change; the operator commands write it for you. A
backdated or mislabeled record is worse than none.

## The escalation ladder

1. **The factory's own verbs.** `--requeue`, `--approve`, `holo send-back`,
   `holo steer`, `--repoint`, `--pause`/`--resume`/`--abort`,
   `--hold`/`--release-hold`, `--close HOLO-n --landed URL`,
   `--file-ticket --update`; a loop is relaunched with `holo start` (the
   `holophyte-loop@NAME` unit, which the host sweep also starts for a ready
   ticket).
2. **`--sweep`, then `--sweep --act`** once a trip is confirmed. On a
   registered project the host sweep already acts every minute, so first
   check it is running (`factory.py --status`). A stuck or refused lease is
   a sweep question: never hand-edit `runs`, `tickets` or `projects` to free
   one.
3. **Operator store API, by name.** From a Python REPL: `release`, `resume`,
   `transition`, `record_intervention`, `record_project_intervention`,
   `walk_ticket`, `requeue` and `repair_references`, kept public for this
   rung even when the loop does not call them;
   `tests/test_store_surface.py` checks these eight.
4. **Raw SQL** only where no API exists, paired with a ticket for the
   missing API filed the same day.

Never skip a rung downward. At most two relaunches against one failure;
the third response is a written diagnosis.

## Operator rules

- **A worker's report is not proof.** Read the diff and rerun the verify
  commands before calling anything done.
- **Tickets** follow `ticketTemplate.md`; check one with
  `python3 ticket_template.py --repo . TICKET.md`, file it with
  `--file-ticket`, and change a filed body only with
  `--file-ticket --update HOLO-n`. A ticket states no literal schema
  version; filing refuses one.
- **After a merge** nothing is restarted by hand: the sweep timer runs from
  `HEAD`, the host daemon exits on a `HEAD` move, the loop re-execs.

## Daily shape

```
# the host daemon and the host sweep are systemd user units, for every
# project in host.toml; see Serving standing
systemctl --user status holophyte.target holophyte-sweep.timer
# a loop by hand, one tmux session per project (the sweep also starts
# holophyte-loop@NAME when a ticket is ready and no loop is live)
tmux new-session -d -s holo-loop  "cd /path/to/holophyte && python3 -u factory.py /path/to/holophyte 2>&1 | tee -a loop.log"
```

The loop idle-exits when the board is empty and stops after a failed run,
so relaunching it is routine. The sweep runs every minute from the
checkout's `HEAD`, and the daemon leaves for the new code on its own after
a self-merge; neither is restarted by hand. The console rebuilds itself:
the host daemon builds `console/dist` at startup when its stamp is not the
checkout's `console/` tree, and keeps the old build if that fails. Under
`[merge] mode = "pr"` the factory pushes the run's branch and GitHub merges
its pull request, so nothing is pushed by hand; under `mode = "local"` the
merge lands on the checkout's own `main` and the factory does not push it
(see [Config](../config.md#merge)).

## Recipes

### A run failed and the ticket needs to go back in the queue

```
python3 factory.py PROJECT --requeue HOLO-n --note "why: contract fixed / infra outage / …"
# then relaunch the loop
```

Refuses a ticket that is merged, has a live run, or does not exist; writes
an `interventions` row with action `requeue`. If the failure was a contract
defect, fix the file and `--file-ticket FILE --update HOLO-n` first; the
rerun reuses the preserved branch.

### A run is parked awaiting merge approval

```
python3 factory.py PROJECT --approve HOLO-n --note "looked at the diff; merge"
# then relaunch the loop
```

Under `[merge] approve = "human"` an approved, verified candidate stops at
the gate with the ticket `blocked_on_operator` asking `merge?` (it is what
`/attention` lists). `--approve` writes an `interventions` row with action
`approve` (the note defaults to `approved for merge`), ends the parked run
with its resume point at the merge gate and walks the ticket to `ready`;
the loop's next claim reuses the preserved worktree and branch, re-runs
the pre-merge verify against current `main` and merges, with no
implementer or reviewer turn. The approval is of the sha the park
recorded: a worktree that has moved on since -- a commit added after the
park, uncommitted edits -- fails that run naming both shas, with the tree
left as found for you to reconcile, and merges nothing. Refuses a ticket
in any other state -- ready, in flight, failed, merged -- naming it, and
writes nothing then.
To decline instead, leave the ticket parked or close the run out by hand
through the store API.

### A run is parked on its pull request and the PR has moved

```
python3 factory.py PROJECT --babysit HOLO-n --note "new review thread; look at the PR again"
# then relaunch the loop
```

Under `[merge] mode = "pr"` a candidate that came up ready parks the same
way, on its pull request, and `--approve` merges it as it stands.
`--babysit` sends it back for another round instead: it writes the
`interventions` row `store.babysit()` writes (the note defaults to `sent
back to the babysitter`), ends the parked run with its resume point at the
merge gate and walks the ticket to `ready`; the loop's next claim resumes
the candidate on its PR, verdicts and answers the new threads, waits on
the checks, and a PR that comes up ready under `[merge] approve = "human"`
parks again for your `--approve` rather than merging. The refusals are
`--approve`'s: a ticket in any other state -- ready, in flight, failed,
merged -- is named and nothing is written.

### A parked candidate's branch was rebuilt on a rewritten `main`

```
# in the preserved worktree, after main was rewritten (a filtered history, say)
git rebase --onto main OLD_BASE task/HOLO-n-SLUG     # the same commits, new tips
python3 factory.py PROJECT --repoint HOLO-n $(git rev-parse HEAD) --note "rebased onto the filtered main; same commits"
python3 factory.py PROJECT --approve HOLO-n --note "looked at the diff; merge"
```

The approval holds the branch to the sha the park recorded, so a branch
rebuilt as the same commits on a rewritten `main` (a filtered history,
say) would fail its gate as drift. `--repoint` is the one legitimate
move of that sha: it writes an `interventions` row with action `repoint`
carrying the note, a `runEvents` row naming the old and new shas, then
sets `runs.candidateSha`, in one transaction, and prints both shas. The
run stays parked and the branch is not touched -- the rebase is your git
work, before the call. The sha must be the full 40-hex commit id; either case is accepted and it is stored lowercased.
Refuses a ticket whose newest run is not parked awaiting merge approval,
one already approved (its release is in flight; `--requeue` it instead),
or a malformed sha, naming it, and writes nothing then. Never re-point
with raw SQL on `runs.candidateSha` now that this verb exists.

### The supervisor ended a run that was fine

Read the run's round findings and the sweep's reason in the store:
`holo run N --ledger` for the findings, and `holo run N` for the outcome
reason the sweep wrote (`swept by the supervisor in phase …`). If the trip
was wrong, `--requeue` with a
note saying so and file a ticket for the trip rule; the false
`review_stuck` of 2026-09-03 became a finding-key fix the same day.

### A preserved branch conflicts with a moved-on `main`

The loop refuses to reuse it and says so. In the worktree:

```
git merge --no-ff main        # resolve, keep the branch's intent
git commit -q -m "HOLO-n: merge main (why)"
python3 factory.py PROJECT --requeue HOLO-n --note "merged main into the preserved branch by hand"
```

### The loop died mid-review

Its signal handler removes the review container on SIGTERM. If it was
killed harder, `--sweep` lists strays under `review containers` and
`--sweep --act` removes them.

### Two loops on one host claimed the same ticket

Each project's `[board]` table names its own board, a Linear `project_id`
or a native `prefix`; a project without one refuses to start. If it
happens anyway, kill the wrong loop, release its run as `failed` with
`outcome_class="infra"`, remove its worktree and branch, and record the
kill.

### A ticket was skipped as `needs_spec`

The loop prints the first validator problem. Common causes: an unfilled
`<placeholder>` (any angle-bracket token outside a link is one, HTML tags
included), a criterion naming a path the project gitignores, a section
after `## Open questions` (it must read exactly `- None`), a bold key that
Linear rewrote. Fix the file, `--file-ticket FILE --update HOLO-n`,
relaunch.

### The host sweep is stale or a project shows an error

The host `/status` (and the drawer's sweep line, the console's host card)
reads `sweep.json`: `stale` means no run ended in two intervals, `killed` a
run that started and never ended.

```
systemctl --user status holophyte-sweep.timer holophyte-sweep.service
journalctl --user -u holophyte-sweep.service -n 200
python3 factory.py --status                     # the home lock, sweep.json, every project
```

A run that exits 1 had a project error; the timer fires again regardless,
and `sweep.json`'s `projects` names which project and why. A killed run
leaves the home lock with a dead pid, which the next run reclaims and
reports. To run one now, the console's **Run sweep** or `systemctl --user
start holophyte-sweep.service`; both record nothing in a store, and the
console's is written to `host-actions.jsonl` first. A project skipped
because its own `supervisor.lock` names a live pid has a project
supervisor still running: stop it (`systemctl --user disable --now
holophyte-supervise@NAME`), since the host sweep is its watcher now.

### Codex or Linear is down

The run fails on the route with the HTTP error in its reason. Wait,
`--requeue`, relaunch. Two relaunches, then diagnose.

### The store refuses to open: schema is newer

A build older than the store's schema refuses on purpose. Pull the
checkout: the next sweep run is the new build, the daemon exits on the
`HEAD` move and the next request starts the new code, and the loop is
relaunched. On the host daemon that one project answers 503 with `schema
newer than build` until then, and the others answer whole.

### Move a project off Linear to the native board

A native store is the only copy of its tickets, so the move copies every
open issue in first and backs the store up before it.

```
python3 factory.py PROJECT --hold --note "moving to the native board"
python3 factory.py PROJECT --status          # drain: wait until no run is live
python3 factory.py PROJECT --board-import --dry-run  # deliver: wait for 0 pushes and 0 notes pending
python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); print(d.execute('pragma integrity_check').fetchone()[0])" ~/.holophyte/<slug>/store.db ~/.holophyte/<slug>/store.db.pre-native
python3 factory.py PROJECT --board-import --dry-run
python3 factory.py PROJECT --board-import
# config.toml [board]: kind = "native", prefix = "PREFIX"; keep team, drop project_id and label
python3 factory.py PROJECT --release-hold --note "on the native board"
```

Hold, then drain: the loop claims nothing new, and `--status` shows the
live runs finishing. Parked pull requests are still reconciled during the
hold, so a woken run waits in the ready queue until the release. The
host sweep delivers the pending board pushes and
notes; the import's summary counts them, and the switch waits for both to
read 0, since nothing posts to Linear afterwards. The import upserts by
board id, so rows, runs, ledger and `dependsOn` stay, old tickets keep
`KO-n` and new ones take `PREFIX-n`; a failure rolls back and rerunning it is
the restart. `team` is the store's key for the project, so it stays as it
was. Nothing in Linear is deleted or archived.

### A maintainer steers a run from a Lotuspod page comment

The operator seat relays a page comment into `holo steer`, through the
command or its `steer` MCP tool, only when the comment was written by the
maintainer's own handle, names exactly one ticket key, and asks for a change
to that ticket's work.

```
holo steer -p PROJECT HOLO-n -n "the comment's text, as written" --author "maintainer via Lotuspod"
```

The note is the comment's text as written. Add `--hint` only when the
comment says hint or advice, and `--now` only when it says now. Reply on the
page with the line the verb printed, a refusal included. Any other comment,
by another handle, naming no ticket or several, or asking no change, is
answered on the page and not relayed.

### A manual merge to `main`

A merge outside the loop goes through a pull request, never a push straight
to `main` (an admin push skips the required `unit` check). It needs the
suite green, `ruff` clean and an independent review of the final branch
state; a ticket the factory ran is then closed with
`--close HOLO-n --landed URL`. Ask before it, and before the first
out-of-band state edit, when the human is present. When absent, freeing a
work-blocking lease and preserving at-risk work are authorized; the merge
waits `blocked_on_operator`.

## Reading the state

```
python3 factory.py PROJECT --report          # estimate vs actual per run, supervisor liveness
python3 factory.py PROJECT --sweep           # what would trip, without acting
python3 factory.py --status                  # every registered project, the last sweep, the locks
curl -s -H "Authorization: Bearer $(cat TOKEN_FILE)" http://WRITER:7710/status | python3 -m json.tool
curl -s -H "Authorization: Bearer $(cat TOKEN_FILE)" http://WRITER:7710/projects/NAME/runs?limit=5
```

`TOKEN_FILE` is your copy of the host's machine token (`host.toml`'s
`[serve] machine_token_file`); a daemon bound beyond loopback answers 401
to a bare request (see [Across machines](hosts.md#what-listens-where)).
The root `/status` lists every project; each project's routes are under
`/projects/NAME`, `NAME` its `[serve] name`. The drawer on the operator's Mac
and the console in a browser show the same through the daemons; a
coloured dot on the glyph means something in "needs you".

## Close the loop afterwards

Reconcile every touched surface before ending an incident: store status,
board status, branches and stashes. File one ticket per gap the incident
revealed; every recipe above started as one.

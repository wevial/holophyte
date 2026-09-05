# Runbook

What to do when the factory is stuck, in the order to do it. The rule
behind every recipe: **record before acting**. Every out-of-band state
change gets an `interventions` row, with a truthful action and a real
timestamp, before the change; the operator commands write it for you.

## The escalation ladder

1. **Relaunch or unblock through the factory's own paths.** Relaunch the
   loop; `--requeue` a failed ticket; fix the ticket file and
   `--file-ticket --update`.
2. **`--sweep`, then `--sweep --act`** once a trip is confirmed. A stuck or
   refused lease is a sweep question, never a SQL question.
3. **Store API from a Python REPL:** `release`, `resume`, `transition`,
   `record_intervention`, `walk_ticket`, `requeue`. Kept public for exactly
   this rung and pinned by `tests/test_store_surface.py`.
4. **Raw SQL** only where no API exists, paired with a ticket for the
   missing API filed the same day.

Never skip a rung downward. At most two relaunches against the same
infrastructure failure; the third response is a written diagnosis.

## Daily shape

```
# one tmux session per process
tmux new-session -d -s holo-loop  "cd /path/to/holophyte && python3 -u factory.py /path/to/holophyte 2>&1 | tee -a loop.log"
tmux new-session -d -s holo-sup   "cd /path/to/holophyte && python3 -u factory.py /path/to/holophyte --supervise 2>&1 | tee -a supervise.log"
# the serve daemons are systemd user units; see Serving standing
```

The loop idle-exits when the board is empty and stops after a failed run,
so relaunching it is routine. The supervisor stays up and re-execs itself
after self-merges. After each loop merge the operator pushes `main` by
hand; the factory never pushes.

## Recipes

### A run failed and the ticket needs to go back in the queue

```
python3 factory.py TARGET --requeue KO-n --note "why: contract fixed / infra outage / …"
# then relaunch the loop
```

Refuses a ticket that is merged, has a live run, or does not exist; writes
an `interventions` row with action `requeue`. If the failure was a contract
defect, fix the file and `--file-ticket FILE --update KO-n` first; the
rerun reuses the preserved branch.

### A run is parked awaiting merge approval

```
python3 factory.py TARGET --approve KO-n --note "looked at the diff; merge"
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

### The supervisor ended a run that was fine

Read the round findings (`FINDINGS.md`, or the store) and the sweep reason
in the ticket's Linear comment. If the trip was wrong, `--requeue` with a
note saying so and file a ticket for the trip rule; the false
`review_stuck` of 2026-09-03 became a finding-key fix the same day.

### A preserved branch conflicts with a moved-on `main`

The loop refuses to reuse it and says so. In the worktree:

```
git merge --no-ff main        # resolve, keep the branch's intent
git commit -q -m "KO-n: merge main (why)"
python3 factory.py TARGET --requeue KO-n --note "merged main into the preserved branch by hand"
```

### The loop died mid-review

Its signal handler removes the review container on SIGTERM. If it was
killed harder, `--sweep` lists strays under `review containers` and
`--sweep --act` removes them.

### Two loops on one host claimed the same ticket

Each target's `[board]` table names its own project; a target without one
refuses to start. If it happens anyway, kill the wrong loop, release its
run as `failed` with `outcome_class="infra"`, remove its worktree and
branch, and record the kill.

### A ticket was skipped as `needs_spec`

The loop prints the first validator problem. Common causes: an unfilled
`<placeholder>` (any angle-bracket token outside a link is one, HTML tags
included), a criterion naming a path the target gitignores, a section
after `## Open questions` (it must read exactly `- None`), a bold key that
Linear rewrote. Fix the file, `--file-ticket FILE --update KO-n`, relaunch.

### Codex or Linear is down

The run fails on the route with the HTTP error in its reason. Wait,
`--requeue`, relaunch. Two relaunches, then diagnose.

### The store refuses to open: schema is newer

A build older than the store's schema refuses on purpose. Pull the
checkout; the supervisor re-execs itself on the newer schema, the loop is
relaunched, the daemon units are restarted.

### A manual merge to `main`

Named event with a gate: suite green, `ruff` clean, an independent review
of the final branch state, `--no-ff` with a message naming the why, the
store and Linear walked to their terminal states in the same sitting, the
FINDINGS window committed. Ask first when the human is present; when
absent, freeing a lease and preserving at-risk work are authorised, the
merge waits.

## Reading the state

```
python3 factory.py TARGET --report          # estimate vs actual per run, supervisor liveness
python3 factory.py TARGET --sweep           # what would trip, without acting
curl -s http://WRITER:7710/status | python3 -m json.tool
curl -s http://WRITER:7710/runs?limit=5
```

The drawer on the operator's Mac shows the same through the daemons; a
coloured dot on the glyph means something in "needs you".

## Close the loop afterwards

Reconcile every touched surface before ending an incident: store status,
board status, `FINDINGS.md`, branches and stashes. File one ticket per gap
the incident revealed; every recipe above started as one.

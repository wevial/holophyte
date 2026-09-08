# Glossary

**Across machines.** The optional second-machine setup. Its roles, the
private network and the port convention live on one page,
[Across machines](../operating/hosts.md); a single machine needs none of
it.

**Adjudication.** The terminal PASS/FAIL round after the run's cap of
review rounds has been spent asking for changes. Recorded as the round
after the cap.

**Attention.** The drawer's top section and glyph colour: what needs the
operator now. Computed from stale heartbeats, stale supervisors, blocked
tickets and recent failures.

**Board.** The Linear project a target claims from, named by the target's
`[board]` table. Status flows one way, store to board.

**Candidate.** The commit under review: the worktree's HEAD, exported
read-only for the reviewer.

**Console.** The browser page the serve daemon answers at `/`: every
project on every host, fanned out to the peers named in `[console]
daemons`.

**Contract.** The ticket body as frozen at claim: title, criteria, verify
commands, estimate. The merge gate refuses a run whose contract drifted.

**Contract defect.** A ticket whose criteria cannot be witnessed from the
candidate tree, or whose verify command cannot pass as written. The
operator's error, fixed by revising the ticket and requeueing.

**Drawer.** The menu-bar menu on the operator's Mac, a SwiftBar plugin
over the serve daemons.

**FINDINGS.md.** The rendered window over the store in each target
repository: newest twenty-five entries, regenerated at every close-out,
never hand-edited.

**Finding.** One structured complaint from a reviewer: path, line,
severity, message. Keyed by `(path, line, severity)` for comparison
across rounds.

**Host label.** `[report] host_label`: what the factory prints instead of
the machine's hostname, so a public repository names roles, not
machines.

**Intervention.** A row recording an operator or supervisor decision on a
run: `redirect`, `kill`, `extend_time_box`, `resume`, `close_out`,
`requeue`, `approve`, `repoint`, `shepherd`. Written before the change it
describes.

**Lease.** `projects.activeRunId`: the one run a project may have in
flight. Taken at claim, released at close-out or by the supervisor.

**Ledger.** The store table recording each run's rounds, adjudications,
merge line and operator steps, one prose row per entry; the comment thread
on the Linear ticket is its projection.

**Park.** A clean merge gate that stops short of merging: the run enters
`awaiting_merge_approval` with branch, worktree and candidate sha kept and
the lease released, and the ticket asks the operator `merge?` or `PR open:
URL`. Answered by `--approve KO-n` or `--shepherd KO-n`.

**PR mode.** `[merge] mode = "pr"`: the clean gate pushes the branch and
opens a pull request against `main` instead of merging locally, then the
shepherd takes over.

**Preserved branch.** A failed run's branch and worktree, left for a
human; reused by the next run of the same ticket. Named
`PREFIX/IDENT-SLUG`, where the prefix is `[worktree] branch_prefix`
(`task` by default) and the identifier keeps it traceable to its ticket.

**Provider.** The board protocol: `claim_next`, `fetch_task`,
`set_state`, `comment`. Linear in production, files in tests.

**Requeue.** `--requeue KO-n --note TEXT`: walk a failed ticket back to
`ready` with its intervention row.

**Review stuck.** The supervisor's trip when two consecutive rounds share
findings above the overlap threshold: the review is circling.

**Run.** One attempt at a ticket. Has phases, a heartbeat, a time box, an
outcome and a class (`work` or `infra`).

**Self-merge.** A merge whose target is the factory itself. The loop
re-execs from the new `main`; the supervisor follows on its next pass.

**Serve token.** `[serve] token_file`: the bearer token a daemon bound
beyond loopback demands on every request except the console's files and
`/peers`. A `chmod 600` file, never printed or logged.

**Shepherd.** The loop's passes over an open pull request, up to `[merge]
pr_rounds`: each reads the unresolved threads and the checks, adjudicates
every thread `ADDRESS`, `DECLINE` or `HUMAN`, fixes and pushes, and merges
through the API when green and quiet or parks. `--shepherd KO-n` asks for
another pass.

**Slug.** A target's basename plus eight hex digits of its path's SHA-1;
names its state directory under `~/.holophyte/`.

**Store.** The SQLite file per target that everything else is rendered
from.

**Strike.** One supervisor sighting of a run with a stale heartbeat; a
configured number in a row ends the run.

**Sweep.** One supervisor pass over live runs. `--sweep` reports,
`--sweep --act` acts.

**Target.** A repository the factory works on, with its store, config and
board. A value in code (`Target`), never a global.

**Time box.** A run's wall-clock budget, from the ticket's estimate.

**Verify gate.** The ticket's own command, run clause by clause before
every review and before the merge.

**Witness.** The test or check a reviewer names as proof a criterion is
met. Must exist in the candidate tree.

**Worktree.** The sibling checkout under `<repo>.worktrees/` a run works
in, so the main checkout is untouched until the merge gate.

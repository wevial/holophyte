# Development

The package map as it is at merge time, the tests, and linting. Back to the
[README](index.md).

## Files

The `holophyte/` package is the factory; `factory.py` is its entry point.
Each module, one line:

- `holophyte/__init__.py` — the package docstring: which module owns what.
- `holophyte/cli.py` — the argument parser and mode dispatch: `--report`,
  `--requeue`, `--approve`, `--babysit`, `--repoint`, `--file-ticket`,
  `--sweep [--act]`, `--supervise`, `--serve` and the loop itself.
- `holophyte/target.py` — where a target's state lives (`HOLOPHYTE_HOME`,
  the `<slug>` directory, legacy adoption) and the `Target` value.
- `holophyte/config.py` — `config.toml` and every table it can set, checked
  at startup.
- `holophyte/config_tables.py` — the per-table readers out of
  `holophyte/config.py` (KO-397): the `[supervisor]`, `[loop]`, `[board]`,
  `[merge]` and `[report]` tables with `CONSOLE_KEYS` and
  `split_address()`, the namedtuples and defaults they own.
- `holophyte/failure_reason.py` — bounded failure reasons and their structured
  event facts for verify, adjudication and fix rounds.
- `holophyte/gates.py` — the verify gate: a ticket's command in, a red or
  green fail-loud report out.
- `holophyte/agent_routes.py` — process-owned route state and live console snapshots.
- `holophyte/agent_turns.py` — per-turn command labels, timing and run events.
- `holophyte/isolation.py` — host/container implementer launch seam, validated
  isolation settings, credential boundary and container cleanup.
- `holophyte/isolation_git.py` — Git metadata validation and atomic object copying.
- `holophyte/isolation_return.py` — Git ref and index locks for atomic turn returns.
- `holophyte/isolation_clone.py` — disposable container checkouts and sanitized,
  fast-forward-only commit imports with environment-file exclusions.
- `holophyte/agents.py` — the agent routes and the `agent()` call, one turn
  of a role.
- `holophyte/review.py` — reviewer output as structured findings and a
  verdict.
- `holophyte/findings.py` — `FINDINGS.md` as a bounded window over the
  store's rows.
- `holophyte/report.py` — `--report`: estimate vs actual per finished run
  (actual, estimate, ratio, rounds, outcome) with mean and median ratio, a
  read-only query over the store that claims no ticket, cuts no worktree and
  calls no one.
- `holophyte/runs.py` — the store seam: a run's progress as store rows.
- `holophyte/board.py` — Linear as the notice board: the ticket mirror, its
  pushes, `--file-ticket` and the escalation. Ticket status lives in the
  store and is projected onto a Linear workflow state by `mirror_push()` —
  one way, last write wins, never read back — so the provider's `set_state`
  is the only writer of that state, and the mapping table beside
  `mirror_push` says which state each status shows as.
- `holophyte/supervisor.py` — the stale-run sweep and the `--supervise`
  loop.
- `holophyte/sweep_report.py` — the sweep's report lines (KO-396): the
  `SWEEP_HEADERS` table, the per-run and restart lines, `sweep_report()`
  as `--sweep`'s whole body, and the review-container and merge-lock
  sections.
- `holophyte/supervisor_lock.py` — the one-supervisor-per-target lock
  (KO-396): the lockfile's path, read, acquire and release, the
  `SupervisorHeld` refusal and the `supervisor_running()` probe.
- `holophyte/serve.py` — `--serve PORT|HOST:PORT`, the read-only HTTP daemon.
- `holophyte/serve_config.py` — the daemon's `/config` routes (KO-394):
  `GET`'s redacted read and `PUT`'s validated, recorded and backed-up
  write, `text` and `{"patch": ...}` bodies alike.
- `holophyte/serve_actions.py` — the daemon's `POST /actions/...` routes
  (KO-395): the body parser, the two unit actions, `requeue`, and the
  interventions row each records before it acts.
- `holophyte/serve_runs.py` — the daemon's run and ledger read routes
  (KO-395): `/runs`, `/shipped`, `/ledger`, `/runs/N`, `/runs/N/ledger`
  and `/runs/N/files`, their query parsers and the origin-link pair.
- `holophyte/redact.py` — secret values in a `config.toml` text, found by
  walking its TOML syntax: hidden for `GET /config`, put back for `PUT`.
- `holophyte/files.py` — the files a run touched, read from git in the
  target's checkout under a timeout: what `/runs/N/files` answers.
- `holophyte/loop.py` — the loop: `run_task`'s stages.
- `holophyte/fix_session.py` — review fix-session arms, resume argv and fresh retry.
- `holophyte/session_arms.py` — shared run-number assignment for session experiments.
- `holophyte/review_session.py` — reviewer session-file capture and resume requests.
- `holophyte/dispatch.py` — the dispatcher out of `holophyte/loop.py`
  (KO-412): one claim run under its crash containment and close-out,
  the startup sweep and the queue mirror.
- `holophyte/merge_gate.py` — the merge gate out of `holophyte/loop.py`
  (KO-424): `main` merged into the branch, the pre-merge verify and the
  drift check under the merge lock, the park for a human's approval, the
  approved candidate's resumed run, and the `--no-ff` merge onto main.
- `holophyte/merge_lock.py` — the bounded wait for a live merge-lock holder,
  with waiter heartbeats and paired wait events (KO-496).
- `holophyte/operator.py` — the operator commands and the entry point:
  `main` (the serial pass or the pool's scheduler) and the self-merge
  re-exec, `report`, and the `--requeue`/`--approve`/`--babysit`/
  `--repoint` store verbs.
- `holophyte/claim.py` — claiming a ticket and cutting its worktree:
  `_claim_next` over `_admit_ticket`'s questions and `_claim_run`'s lease
  and board label, then `_cut_worktree`/`reuse_leftover`,
  `run_worktree_setup` under `_setup_worktree`, and the mid-merge
  hand-off (`merge_conflicts`, `conflict_brief`, `_resolve_merge_conflict`).
- `holophyte/environment_git.py` — excludes the filtered `.env` from Git
  staging and refuses candidate pushes containing it.
- `holophyte/commit_hygiene.py` — removes configured attribution lines from
  unpublished commits before task pushes and local merges.
- `holophyte/pr.py` — `[merge] mode = "pr"`'s GitHub writes: the startup
  route check, the push, the pull request and its body, the babysitter's
  replies and resolves, the merge through the PR API, and the `gh`/API
  transport they ride on.
- `holophyte/pr_contexts.py` — page the head rollup's commit statuses and
  normalise them beside check runs for required-context folding (KO-485).
- `holophyte/pr_head.py` — bounded settling after a push and the foreign-head
  guard before the babysitter judges or merges a candidate.
- `holophyte/pr_activity.py` — authored PR activity and event-backed wake guards.
- `holophyte/pr_status.py` — reading a pull request's state (KO-426), out
  of `holophyte/pr.py`: `pull_status()` for the parked-run reconcile,
  `pr_state()` and `fold_checks()` for the babysitter, `parse_pr_url()`.
- `holophyte/media_store.py` — standard-library SigV4 PUTs for evidence in an
  S3-compatible bucket. Operator setup lives in the
  [bucket configuration reference](config.md#mergemedia_bucket).
- `holophyte/pr_media.py` — user-facing diff matching, timed capture, bounded
  media publishing and evidence shared by PR descriptions and review prompts.
  See [merge configuration](config.md#merge) for capture commands, destinations
  and size limits.
- `holophyte/pullrequest.py` — the pull-request stage of the loop: the PR
  open or adopt, every park on the PR, the merge through the PR API and
  its ledger line, and the resume of a run parked on its PR.
- `holophyte/maintainer_notes.py` — private maintainer instructions as pre-approved
  babysitter threads, contract amendments, and commit event references.
- `store/operator_notes.py` — atomic send-back and append-only note consumption
  evidence across attempts on the same pull request.
- `holophyte/bot_threads.py` — advisory bot findings and human-reply escalation.
- `holophyte/conversation_comments.py` — paged human conversation instructions
  and recognition of quoted factory replies.
- `holophyte/thread_answers.py` — read-only mention answers and thread replies.
- `holophyte/thread_mentions.py` — mention markers and typed intent triage.
- `holophyte/questions.py` — typed choice requests and safe service failures.
- `holophyte/thread_findings.py` — structured PR findings, bounded originals,
  and read-only normalization of legacy thread rows.
- `holophyte/babysit_steps.py` — records PR babysitting step changes without
  repeating unchanged steps during polling.
- `holophyte/babysitter.py` — the babysit pass over a pull request: the
  adjudicator's brief over its threads, the `ADDRESS`/`DECLINE`/`HUMAN`
  verdict parser, the `---- Comment by MODEL ----` replies, the round
  text, the parked question, and the passes that drive them.
- `holophyte/reconcile.py` — the startup reconciles and the GitHub read
  budget: parked pull requests asked about on GitHub, mirrored tickets
  Linear closed walked to their terminal status, and the GraphQL budget
  the reads spend from.
- `holophyte/startup.py` — eager package imports, build identity and live-worker checkout guard.
- `holophyte/pool_handoff.py` — schema-aware restart and child ownership across exec.
- `holophyte/pool.py` — the worker pool and its scheduler (KO-343):
  `scheduler()` over `factory.py --worker` children under
  `[loop] workers > 1`, `worker()` as one child, the spawn and wait
  seams, the claimable count and the `[holo2 wN]` line prefixing.
- `holophyte/reexec.py` — `reexec_self`, the shared self re-exec the loop
  and the supervisor both restart themselves through.

The store is its own package:

- `store/__init__.py` — the v2 durable state store, one WAL-mode SQLite
  file: claims and leases, run-phase transitions, review rounds and
  the ledger.
- `store/operate.py` — the operator API: `release()`/`resume()`/
  `requeue()`/`repoint()`/`approve()`/`babysit()`, `record_intervention()`
  and the `runEvents` writers, re-exported from the package.
- `store/enums.py` — canonical store vocabularies and generated SQL CHECK clauses.
- `store/schema.py` — the schema, its migration ladder and the
  connection (`open`/`init`/`transaction`), re-exported from the package.
- `store/tickets.py` — the ticket state machine: `ensure_project`, the §3
  `TICKET_TRANSITIONS` table and `transition()`/`walk_ticket()`,
  `mirror_ticket`, §2's `pickable()`/`pickable_tickets()` and the Mermaid
  state-graph renderer, re-exported from the package.
- `store/instructions.py` — persist replies on recorded thread instructions.
- `store/agent_routes.py` — atomic fallback intervention and event records.
- `store/launch_backoff.py` — persistent route outages, retry deadlines and
  project-owned startup evidence before a first claim (KO-466).
- `store/working.py` — persisted work intervals, finally-safe settlement and
  non-mutating effective-work reads; excludes orchestration waits.
- `store/read.py` — typed read views over the store: one query, one row
  type, no SQL elsewhere.

At the root:

- `factory.py` — the entry point: imports `cli` from the package and calls
  it. Holds no `def` or `class` of its own.
- `provider.py` — the `Provider` protocol the loop talks to a board through.
- `linear_provider.py` — the Linear GraphQL client: claim/fetch_task/
  set_state/comment, ready-ticket and blocker resolution, issue creation.
- `review_runner.py` — exact-SHA staging and the model-neutral local
  reviewer boundary (see [Reviewing](reviewing.md)).
- `ticket_template.py` — parser/validator for the ticket shape;
  `python3 ticket_template.py TICKET.md [...]` exits 0 iff the ticket is
  pickable-ready.
- `ticketTemplate.md` — the ticket shape. Verify commands go in the
  "Verify command(s)" section (exit 0 = pass, relative paths only);
  estimate is the budget in minutes. The optional "Contract checks" section
  declares `relative/path: exact literal` lines the gate asserts verbatim, so
  a required value (a port, a URL) cannot drift while the commands still pass.
- `docker/reviewer.Dockerfile` — pinned minimal reviewer image.
- `FINDINGS.md` (generated, not kept in this repository) — in a target with
  `[report] findings = "repo"`, a rendered window over the store, not a log:
  the factory regenerates it at each close-out from `runs`/`reviewRounds` as
  the newest 25 entries below a `<!-- store-rendered below -->` marker, with
  everything older counted in one archive line and kept in the store.
  Text above the marker is frozen pre-store history and is never rewritten;
  Linear ticket comments stay the full per-ticket archive.
- `tests/` — the stdlib unittest suite, one `test_*.py` per surface, with
  `tests/fake_agent.py`, `tests/procs.py` and `tests/waiting.py` as shared
  helpers. Run it
  with `HOLOPHYTE_HOME=$(mktemp -d) python3 -m unittest discover -s tests`.

## Linting

`ruff check .` from the repo root; it exits 0 when the tree is clean. Run it
alongside the tests — the developer verify path is:

```
ruff check .
python3 -m unittest discover -s tests
```

The configuration lives in `ruff.toml`: line length
88, target `py311`, and rule sets `E`, `F`, `W`, `I`, `C90` (pycodestyle
errors and warnings, pyflakes, import ordering, McCabe complexity). Nothing is formatted, only checked.
Every enabled rule is a promise the factory keeps forever, so the selection
stays small, and a violation that has to stand is suppressed with a per-line
`# noqa: <CODE>` rather than a file-level or blanket ignore.

Cyclomatic complexity above 12 is a lint failure (ruff `C901`); an exemption
is a per-function `noqa: C901` that names its reason and the ticket that
retires it.

Module size is a ratchet on the same terms: no source module over
1000 lines, no test module over 1500. `tests/test_file_sizes.py` holds a
table of the modules over their ceiling, each entry its module's count
as `wc -l` measures it, and walks every tracked Python file against it —
a module that grows past its entry, a module with no entry that passes
its ceiling, and an entry whose module is back under the ceiling all
fail the suite, and a second check holds every entry to `wc -l`'s exact
count. A slice that changes a listed module's size rewrites its entry in
the same commit; a module back under its ceiling leaves the table, so
the numbers only move down.

ruff is a developer tool, not a dependency: install it on the host with
`pip install --user ruff` (or `uv tool install ruff`). It is never vendored.


## Console

The console is the browser page the daemon serves at `/`. It lives in
`console/` as its own package: Bun is the runtime, test runner and bundler;
the page is React 19 with TypeScript, styled with Tailwind v4. The three
commands, run from the repo root:

```
bun --cwd=console install --frozen-lockfile
bun --cwd=console test
bun --cwd=console run build
```

`--cwd=console` is the equals form of Bun's global flag and the only form
that works here: Bun 1.4 reads the space-separated `bun --cwd console X` as
`bun run X` inside the directory, so `install` and `test` would resolve to
package scripts and `run build` would print usage without building.

`install` reads the committed `console/bun.lock` and refuses to drift from
it; it never builds — there is no install lifecycle script, and
`run build` is the one path to `console/dist/`. `test` runs `bun test` with
`console/tests/setup.ts` preloaded and discovery rooted at `console/tests/`
(see `console/bunfig.toml`, so the nested electron package's tests stay its
own), which
registers `happy-dom` so component tests render with
`@testing-library/react` and no browser; `console/src/lib/` tests stay free
of the DOM. `build` runs `console/build.ts`, which hands `console/index.html`
to `Bun.build` with `bun-plugin-tailwind` and writes the static bundle to
`console/dist/` — `index.html` is the stable entry the daemon serves, next
to its hashed script and stylesheet. `console/dist/` and
`console/node_modules/` are git-ignored.

Bun is a developer tool like ruff, never vendored. The operator step for the
writer host: install it with the upstream installer
(`curl -fsSL https://bun.sh/install | bash`) as the factory's user, so the
loop's login shell sees `bun` on PATH. The factory's verify step inherits
that PATH, so nothing in the loop changes. The writer host runs Bun 1.4.2.

### Desktop wrapper

Some operators want the console in the dock with a menubar icon rather than
in a browser tab. `console/electron/` is a thin Electron shell around the
URL the daemon serves: one window (1280×860, never narrower than the 1100px
the design assumes), a tray icon built from `assets/menubar-template@1x.png`
and its `@2x` sibling whose menu carries the SwiftBar drawer's summary,
then "Show console", an "Open at login" checkbox that
reads and sets the macOS login item, "Developer tools", which opens the
window's DevTools (the platform shortcut works too), and "Quit", and nothing
of its own — no renderer code, no preload, no IPC, so the app and the browser
tab never drift. The window's console errors and failed loads are appended,
one timestamped line each, to `console.log` in Electron's user-data
directory (beside `console.json`; source paths only, never query strings),
truncated once it passes 1 MB, so a blank view can be diagnosed afterwards.
On macOS closing the window leaves the tray in place and the dock or tray
reopens it; elsewhere closing the window quits.

The console URL comes from the first of three sources, and a bad value in
that source is a dialog naming it, never a fall-through to the next:

1. `HOLOPHYTE_CONSOLE_URL` in the environment.
2. `console.json` in Electron's user-data directory, `{"url": "…"}`; the
   operator's own URL lives there, never in the repo.
3. The default `http://127.0.0.1:7710/`, the first target's port on a host.

Only `http` and `https` are accepted.

The tray menu is rebuilt every ten seconds from `/peers` on that URL and
then `/status` and `/attention` on each daemon it names (and `/runs` on an
idle one, for its last merge), with the drawer's wording: what needs you
first ("Nothing needs you" when nothing does), one line per project
(`holophyte · working KO-n · hb 12s`, or `idle · last merge KO-n · 3m`),
a hosts line (`1 host · 3 daemons`), then the three fixed entries.
Clicking an attention or project line shows the console. The tray glyph is
the template when every daemon is idle and the drawer's `warn` or `bad`
variant for the worst state shown (something needs you; a daemon is
unreachable); `bun run icon` renders those variants from
`assets/menubar-{warn,bad}.svg` into `console/electron/dist/`, and `start`
and `package` run it first.

A daemon beyond loopback wants its serve token (see
`docs/reference/http.md`, Authentication); the tray never prompts for one.
It reads them from `console.json`, per `HOST:PORT` address as `/peers`
names it, either inline or as the path of the file the daemon's own
`[serve] token_file` holds, the same file the drawer's `[[daemon]]
token_file` points at:

```json
{
  "url": "http://127.0.0.1:7710/",
  "tokens": { "writer-2:7710": "…" },
  "token_files": { "writer-3:7710": "~/.holophyte/writer-3.token" }
}
```

A `token_files` path may start with `~` or be relative to the user-data
directory; `tokens` wins over `token_files` for the same address. A daemon
whose token is missing or wrong shows `needs token` on its line until the
file is fixed; one that does not answer within two seconds shows
`unreachable`. The window seeds the page's tokens from the same
`console.json` entries once it has loaded, under the keys the Hosts card
reads, and reloads once when that changed anything, so a host the tray can
see needs nothing pasted. The commands, run from the repo root:

```
ELECTRON_SKIP_BINARY_DOWNLOAD=1 bun --cwd=console/electron install --frozen-lockfile
bun --cwd=console/electron test
node console/electron/node_modules/electron/install.js
bun --cwd=console/electron run start
```

`test` exercises the pure URL resolver in `console/electron/config.ts`, the
summary builder in `console/electron/tray.ts` and the poll in
`console/electron/poll.ts` (with a fake `fetch`), and needs no Electron
binary, which is why the verify install skips the
download. `start` needs the binary, and a second `install` after the
skipped one reports no changes and leaves it absent, so fetch it by running
Electron's own postinstall script directly (the `node …/install.js` line
above; it is a no-op once the binary is present). Then `run start` builds
`main.ts` to `console/electron/dist/`
(`main.cjs`, CommonJS with `electron` left external, since the package is
`"type": "module"`) and launches it through the top-level `main.cjs`.
`console/electron/dist/` and `console/electron/node_modules/` are
git-ignored.

To put the console in the Dock like any app, package it:

```
bun --cwd=console/electron run icon
bun --cwd=console/electron run package
```

`icon` renders `assets/logo.svg` to `console/electron/dist/icon.png` at
1024×1024 through `@resvg/resvg-js`, so no image tool has to exist on the
machine. `package` runs `build` and `icon` itself, then `electron-builder`
(configured in `console/electron/electron-builder.yml`) writes
`console/electron/dist/mac-arm64/Holophyte.app` and, one level up,
`console/electron/dist/Holophyte-<version>-arm64.dmg` (the builder puts a
target's artifact in the output directory and the unpacked app in its
per-arch subdirectory). The app is ad-hoc signed: the config sets
`identity: "-"` explicitly, since the builder's default is to skip signing
when no certificate is in the keychain; notarisation, auto-update and other
platforms are not covered. The packaged app finds the console URL exactly as
the repository run does: from `HOLOPHYTE_CONSOLE_URL` or `console.json`,
never from anything baked into the bundle.

# Holophyte v2 — Roadmap and standing decisions

Updated 2026-08-30. This is the durable record of phase sequencing and the
design decisions behind it. The frozen store/state design lives in
`docs/v2/state-model.md` (local-only, gitignored); this file is the part
that belongs in the repo: what we decided, in what order, and why.

## Phases

### Phase 1 — Trustworthy loop ✅ (completed 2026-08-30)

One ticket in, one honest merge out. Landed: fail-loud verify gates
(per-clause diagnostics, vacuous-green = RED, literal contract checks),
review flow (2 fix rounds + terminal PASS/FAIL adjudication), SQLite store
(claims, phases, findings rows, windowed FINDINGS.md, Linear mirror,
telemetry + `--report`), scope caps, ruff gate, FakeAgent test harness,
failure-pattern escalation, claim-time ticket snapshot + merge-time drift
check.

### Phase 1.5 — Portable substrate (next)

Give configurable behavior an address before building on top of it.

- Per-target config file: `<repo>.holophyte.toml`, sibling of the target
  (same convention as `<repo>.holophyte.db` / `<repo>.worktrees`).
  Parsed with stdlib `tomllib`. **Absent file = current behavior.**
- `[agents]` table: implementer/reviewer/adjudicator commands become
  config. Long term no harness is hardcoded — claude/codex are defaults,
  not assumptions.
- `[worktree]` table: `setup = [...]` bash commands run in the fresh
  worktree through the existing instrumented verify-gate machinery
  (fail-loud, budgeted, phase-tracked; setup failure fails the run
  before any agent tokens are spent).
- Known wart this must fix: Lotuspod worktree tests execute against the
  main checkout's `.venv` — an editable-install dependency change would
  break confusingly. Worktree setup commands are the cure and the proof.

### Phase 2 — Unattended operation

- Supervisor loop: separate process reading `runs` + heartbeats; purely
  mechanical trip conditions first (time-box, stale heartbeat two-strike,
  `review_stuck` via findings-fingerprint overlap). Knobs live in a
  `[supervisor]` config table — which is why Phase 1.5 comes first.
- Split step: non-converging reviews draft child tickets from unresolved
  findings; depth cap 2.
- Estimate calibration: recalibrate scope caps from `--report` data once
  ~20 instrumented runs exist.
- Design inputs already mined: Ralph-loop discipline (iteration caps,
  drift guards), v1 TUI patterns (singleton arbitration, two-strike
  liveness, crash-safe resume ordering) — see `tmp/mine-*.md` reports and
  the published mining artifacts on Lotuspod.

### Phase 3 — Harder targets, then a second consumer

- Next target: **Croton** — a genuinely complex project (real deps, env
  setup, existing conventions) but still ours. Deliberately NOT Relos
  yet; Croton surfaces portability gaps without a coworker watching.
- Relos comes after Croton proves the substrate. Fork-vs-contribute
  resolves itself: if Relos needs fit in a `.toml`, nothing to fork —
  Relos is just a config. Only mechanism gaps reopen the question.

## Standing decisions

1. **KISS / evidence-first.** No mechanism without a demonstrated
   consumer. Applies to config knobs, plugin seams, and supervisor
   features alike.
2. **Pluggable by shape, not by system.** North star: easy to adopt and
   adapt (the pi-harness property — small enough to read, obvious
   seams). Hardcoded behavior migrates to module-level seams (callables,
   lists, config) as tickets touch it. A real plugin/registry mechanism
   waits for the second consumer's evidence.
3. **Tickets are atoms; Linear is authoritative for scope.** Narrow
   15–30 min leaves; parents are tracking-only and unclaimable; the
   ticket body is a frozen contract (machine-enforced since KO-141).
4. **Every failure becomes a mechanical gate.** Failure classes 1–6 and
   their gates are documented in FINDINGS.md history; new failure
   classes get the same treatment.
5. **The factory never pushes.** Pushes are manual, human-initiated.

## Failure lineage (why each gate exists)

| Wreck | Gate it bought |
|---|---|
| KO-106 empty verify output | per-clause fail-loud diagnostics |
| KO-107 `Ran 0 tests OK` | vacuous-green = RED |
| port 8622→8000 drift | literal contract checks |
| KO-109 round-2 death | fix rounds + terminal adjudication |
| KO-110 180-min blob | mechanical scope caps |
| 53KB FINDINGS in a day | windowed deterministic rendering |
| KO-111 absolute-`cd` verify | relative-path rule |
| KO-138 unwitnessed default | behavioral claims need a witness test |
| retry-loop token burn (anticipated) | failure-pattern escalation |
| mid-run goalpost edits (anticipated) | claim-time snapshot + drift abort |

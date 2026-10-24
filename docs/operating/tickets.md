# Tickets as contracts

A ticket is the whole specification the loop works from. Its body is frozen
at claim, held against at merge, and read by a reviewer who can witness
only what is in the candidate tree. Writing one well is most of the
operator's job.

## The shape

`ticketTemplate.md` is the canonical structure and `ticket_template.py`
validates it. A ticket opens with a header of typed fields in TOML between
two `+++` lines, before its H1:

```
+++
ui_change = "major"
estimate = 45
depends_on = ["KO-7"]

[mockup]
url = "https://claude.ai/artifact/ID"
approved = 2026-10-19
note = "the two-column settings page"

[[mockup.boards]]
url = "https://media.example/settings.png"
caption = "settings, desktop"

[[evidence]]
file = "01-settings.png"
state = "the settings page, saved"
+++
# Add a settings page
```

The fields, each optional but `estimate`:
- `ui_change`: `"none"`, `"minor"` (copy, a button style, spacing) or
  `"major"` (a new or reworked layout).
- `blast_radius`: `"high"` or `"medium"`, when the change is riskier than its
  diff shows; it can raise a review round's tier and never lower it.
- `estimate`: whole minutes from 1 to 90.
- `depends_on`: a list of ticket keys, like `["KO-7"]`.
- `orchestration`: `"off"`, `"subagents"` or `"workflow"`, overriding the
  project's `[agents.implementer] orchestration` for this ticket.
- `implementer`: a profile name of letters, digits, `_` and `-`, run on the
  project's `[agents.implementer_profiles.NAME]`.
- `[mockup]`: `url`, the approved mock-up (a `https://claude.ai/artifact/ID`,
  `https://claude.ai/code/artifact/ID` or `https://lotuspod.DOMAIN/NAME.html`
  page), `approved`, a date like `2026-10-19`, and `note`, what was approved.
  Each `[[mockup.boards]]` entry has a `url`, a `.png` snapshot of a board or
  a `.zip` bundle of the boards' `.dc.html` sources, `canvas.json` and PNGs,
  under the project's `[merge.media_bucket] public_base`, and an optional
  `caption` of one line without brackets; the run downloads them into the
  task worktree's ignored `.holophyte/mockup/` and lists them in the
  implementer's goal.
- `[[evidence]]`: at most six entries in capture order, each a `file`,
  `NN-slug.png` for a state or `NN-slug.webm` for a recorded flow, numbered
  from `01`, and a `state`, the page, state and interaction it shows.

A field that fails its check is a problem naming it, as in `header estimate
must be whole minutes from 1 to 90, not 120`.

Sections, in order: an H1 title; Summary; What / Why / How (the bold keys
`**What:**`, `**Why:**`, `**How:**`, plain `What:` also accepted); optional
Reproduce (a bug's steps and where it was seen; its presence makes the first
turn a reproduce turn that commits a failing test before any fix); In scope
(at most six entries); Out of scope; Acceptance criteria (at most ten, each
`Given … when … then …`); Verify command(s) (a fenced block of
relative-path, non-interactive commands; exit 0 is pass); optional Contract
checks (`relative/path: exact literal`); Implementation notes; optional
Story; Open questions (exactly `- None` to be claimable).

During the transition a body without a header still files in the magic-line
form: a `**UI change:** major` or `minor` and a `**Blast radius:**` line under
What / Why / How, a `## Mock-up` section (one bare mock-up link, one
`Approved YYYY-MM-DD: what` line, then its media links, each bare,
`![caption](URL)` or `[caption](URL)`), a `## Evidence` section (one state per
line) and a `## Estimate & dependencies` section (`Estimate: N min · Depends
on: KO-n` or `none`). A ticket uses one form or the other: any of those lines
or sections beside a header is refused, as in `'## Mock-up' beside a header:
give [mockup] in the header only`.

Validate before filing, against the project repository:

```
python3 ticket_template.py TICKET.md --repo /path/to/project
```

Blockers make the ticket INVALID and the loop skips it as `needs_spec`.
Advisories print and let it through.

With `--repo`, the checker also runs the landmark half of the claim's
freshness check against that repository's `main`: every path the criteria
and Implementation notes name must be on `main`, and every function or class
an Implementation notes item names must be in one of the files on `main`
that item names. A path
is exempt when "new" governs it: "new", at most three words with no
preposition among them, an optional comma, colon or opening parenthesis,
then the path or a list of paths joined by commas, `and` or `or`. "a new test
file `tests/x.py`" and "new modules `a.py` and `b.py`" declare; "a new test
in `tests/x.py`" does not. A repository with no `main` commit skips the
check.

## What the validator refuses, and why

| Refusal | Why it exists |
| --- | --- |
| Unfilled placeholder: any `<…>` or `{{…}}` outside a markdown link | KO-165 was claimed with template placeholders in its title and criteria and merged anyway. HTML tags count; write "the `main` element", not `<main>`. HTML comments in a draft's "Open questions" section are stripped before validation, so the template's guidance comment there is not a placeholder; a comment in any other section is refused like any other tag. |
| More than six in-scope items, ten criteria, or 90 minutes | KO-110 was a 180-minute blob. Small tickets converge; big ones burn rounds. The caps were raised from three, five and 30 after the 2026-09-29 measurement: across 74 merged tickets, implementation was about a tenth of claim-to-merge time, and the rest is a fixed cost paid once per ticket. Small tickets remain the norm for fixes. |
| A non-relative path in a verify command | KO-111 `cd`'d to an absolute path and verified the wrong tree. |
| A path a criterion names that the project repository gitignores | KO-166 named a rendered file under a gitignored `artifacts/`; the reviewer's export cannot contain it and the implementer force-tracked it. |
| A `depends_on` or `Depends on` that is not a ticket id (or `none`) | dependencies are machine-checked through Linear `blocks` relations |
| Open questions not exactly `- None` | an open question is not a frozen contract |
| `**UI change:**` other than `major` or `minor`, or a `## Mock-up` without exactly one accepted bare URL and one `Approved YYYY-MM-DD: what` line | a markdown link keeps only its text, and an unnamed approval is no agreement |
| `ui_change = "major"` (or `**UI change:** major`) with no `[mockup]` or `## Mock-up`, on a project whose `[merge]` sets `ui_paths` (`--file-ticket` and the claim) | a layout agreed before the run is cheaper than one corrected after the Evidence screenshots |
| A mock-up board or media link outside `[merge.media_bucket] public_base`, or on a project without a bucket; or a major UI change whose mock-up has no snapshot or bundle, on a project that sets `ui_paths` and a bucket (`--file-ticket` and `--repo`, not the claim: a ticket filed earlier still claims and its run records a `mockup` warning) | no agent can open a claude.ai artifact; the implementer matches the design only from files the factory downloads |
| `header has no closing '+++' line`, or `header is not valid TOML: …` | a header that does not parse sets no field, so its ticket would run without an estimate or a mock-up |
| `header field "X" is not a ticket field`, at the top level or inside `[mockup]`, `[[mockup.boards]]` or `[[evidence]]` | a misspelled field would be dropped silently |
| `header estimate is missing` | every ticket is budgeted |
| A header field's value out of its range, as in `header ui_change must be …` or `header evidence[1].file must read 01-slug.png …` | a typed field holds only what the loop can act on |
| A `**UI change:**` or `**Blast radius:**` line, or a `## Mock-up`, `## Evidence` or `## Estimate & dependencies` section, beside a header | two places for one field drift apart |
| A named path `main` lacks, or a named symbol not in the file it is paired with, in a body with `Depends on: none` (`--repo` and `--file-ticket`; with a dependency each reason is an advisory, or a `warning:` line at filing, and the claim re-checks it) | the claim parks such a ticket as stale; LOTUS-95, LOTUS-96, LOTUS-97 and HOLO-182 passed filing and were parked at claim |

Advisories: a `What:` that chains two deliverables; a bare `python3` in a
verify command on a project with a venv; a criterion that reads as an
operator or post-merge witness; three or more evidence states with no UI
change declared and no mock-up (declare `major` and link the mock-up, or
declare `minor`).

## What the reviewer can witness

The reviewer sees a clean export of the candidate commit, read-only, and
nothing else: not `main` after the merge, not the host it runs on, not a
screen, not a store. Every criterion must be witnessable from that tree,
and the reviewer must name the witness:

```
CRITERION 1: met — tests/test_serve.py::StatusTests::test_lists_live_runs
CRITERION 2: not met — production requests /runs, not /runs?limit=1
CRITERION 3: unwitnessed — no test exercises the 404 path
```

A named test that does not exist in the tree is unwitnessed. A near-miss
citation resolves only to a single match: the same test name on exactly one
other class of the named file (`path::Wrong::test_x` when only `Right`
defines `test_x`), or a dotted module whose file exists
(`tests.core.test_sources` read as `tests/core/test_sources.py`). The
criterion's `criteria` event records the correction under `resolved`. An
ambiguous match (the name on two or more other classes, which the note
lists) or an absent one stays unwitnessed. Any criterion
not met or unwitnessed makes the round `changes_requested` whatever the
verdict line says. So:

- Write criteria as "a test witnesses this", and mean it. The full list of
  rules the reviewer enforces sits in the comment above the acceptance
  criteria in `ticketTemplate.md`, where the author reads it before drafting.
- Keep visual passes, re-renders of gitignored output, and "on the writer
  host" checks out of the criteria; they are operator steps, recorded in
  the ledger after the merge.
- A criterion may ask for a RED baseline: "On main the test fails, and
  that run's output is in the implementer output." The factory makes that
  run itself before each review round, for three forms of the sentence:
  one that starts "On main", one that says "as on main" (`With X as on
  main ...`, which runs the whole base tree, not only `X` reverted), and
  one that starts "With the change reverted". It runs the verify lines up
  to the first one that names the criterion's test file, at the review
  base with the candidate's copy of that file, and hands the reviewer the
  output in place of the implementer's. The criterion must name a test
  file; one that names none, and any other state (another ticket's
  branch, a scratch commit, a forced failure), still needs the
  implementer's output, and the ticket checker advises so.
- Verify commands must be true of the candidate, not of a fixture you
  imagined: a negated grep that also matches a legitimate line (`---`)
  and a plural that a one-item fixture cannot produce both failed real
  runs.

## Filing and editing

```
python3 factory.py PROJECT --file-ticket TICKET.md --priority high [--state Todo|Backlog]
python3 factory.py PROJECT --file-ticket TICKET.md --update KO-n
```

Both validate the file against the project, act on Linear, read the stored
body back and validate that again, so a transfer that rewrites bold or
autolinks an example identifier is caught at filing time rather than at
claim time. Exit 1 means nothing was changed; exit 2 means the issue exists
but its stored body needs a fix. Filing adds a `blocks` relation per
`Depends on` id; `--update` adds the blockers the file names that the
board does not hold yet, removes none, and prints what the board still
holds beyond the file (`board also holds KO-m`) for you to clear by hand.
Todo and In Progress are claimable;
Backlog and Done are not. `[loop] order = "priority"` makes an Urgent or
High ticket run first.

On a native board (`[board] kind = "native"`) the column is yours to move,
at the revision you read the ticket at:

```
python3 factory.py PROJECT --move KEY-n ready|backlog --revision N [--note TEXT]
python3 factory.py PROJECT --cancel KEY-n --revision N --note TEXT
```

A move takes an idle or live ticket between Ready and Backlog; a live run
continues and the ticket is not claimed again. A draft is refused Ready.
A cancel records an abort on a live run, which ends `abandoned` at its
next safe point, and the printed line names the run. A ticket that moved
past `N` is left as it is, its current revision printed, exit 1. Nothing
moves a ticket out of Canceled. On a Linear project both are usage errors:
move its tickets in Linear.

A ticket the claim parked `needs_spec` keeps its column. A park for stale
landmarks or by the critic is re-readied by `--file-ticket --update` with a
clean body, or by `--move KEY-n ready`, which re-checks the stored body
against `main` but does not ask the critic again: with reasons remaining it
lists them and changes nothing, and with none it re-derives the ticket and
records a `requeue` intervention. A move to Ready of a ticket already in
Ready names its status, as in `KEY-n is already in ready at status ready;
nothing changed`: a ticket at `ready` is already claimable.

## The ledger

Every run leaves a comment on its ticket: the rounds, their findings, the
adjudications (`ADDRESS`, `FOLLOW_UP`, `DECLINE`), and any operator step
taken after the merge with its time. A fix commit writes each `FOLLOW_UP`
as one unwrapped line, `FOLLOW_UP(feature): TEXT @ PATH:LINE` or
`FOLLOW_UP(guardrail): TEXT @ PATH:LINE`, the ` @ PATH:LINE` tail optional;
when the run merges, a feature is filed as a `Draft follow-up:` ticket in
Backlog, unless an open draft already has its fingerprint, and a guardrail
is kept as a findings-ledger row in the store's `followUps` table. The store holds the rows;
`FINDINGS.md`, in a project that opts in, renders the window; the ledger is
the narrative. A contract
revision is recorded there too, with what was wrong and why, so a rerun's
reviewer can read the history.

## Tickets the loop must not take

Tracking and design tickets have no verify command by design and stay in
Backlog. A parent stays Backlog while its leaves run. A ticket whose
contract needs a human decision is Backlog until the decision is in the
body.

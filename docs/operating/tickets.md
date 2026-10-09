# Tickets as contracts

A ticket is the whole specification the loop works from. Its body is frozen
at claim, held against at merge, and read by a reviewer who can witness
only what is in the candidate tree. Writing one well is most of the
operator's job.

## The shape

`ticketTemplate.md` is the canonical structure and `ticket_template.py`
validates it. Sections, in order: an H1 title; Summary; What / Why / How
(the bold keys `**What:**`, `**Why:**`, `**How:**`, plain `What:` also
accepted, and an optional `**UI change:** major` or `**UI change:** minor`
declaring the size of a UI change); optional Reproduce (a bug's steps and
where it was seen; its presence makes the first turn a reproduce turn that
commits a failing test before any fix); optional Mock-up (one bare link to
the approved mock-up, a `https://claude.ai/artifact/ID`,
`https://claude.ai/code/artifact/ID` or `https://lotuspod.DOMAIN/NAME.html`
page, and one `Approved YYYY-MM-DD: what` line); In scope (at most six entries); Out of scope; Acceptance
criteria (at most ten, each `Given … when … then …`); Verify command(s)
(a fenced block of relative-path, non-interactive commands; exit 0 is
pass); optional Contract checks (`relative/path: exact literal`); optional
Evidence (at most six states to capture, one per line);
Implementation notes; Estimate & dependencies (`Estimate: N min · Depends
on: KO-n` or `none`, 90 minutes at most); Open questions (exactly
`- None` to be claimable).

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
| A `Depends on` that is not a ticket id or `none` | dependencies are machine-checked through Linear `blocks` relations |
| Open questions not exactly `- None` | an open question is not a frozen contract |
| `**UI change:**` other than `major` or `minor`, or a `## Mock-up` without exactly one accepted bare URL and one `Approved YYYY-MM-DD: what` line | a markdown link keeps only its text, and an unnamed approval is no agreement |
| `**UI change:** major` with no `## Mock-up`, on a project whose `[merge]` sets `ui_paths` (`--file-ticket` and the claim) | a layout agreed before the run is cheaper than one corrected after the Evidence screenshots |
| A named path `main` lacks, or a named symbol not in the file it is paired with, in a body with `Depends on: none` (`--repo` and `--file-ticket`; with a dependency each reason is an advisory, or a `warning:` line at filing, and the claim re-checks it) | the claim parks such a ticket as stale; LOTUS-95, LOTUS-96, LOTUS-97 and HOLO-182 passed filing and were parked at claim |

Advisories: a `What:` that chains two deliverables; a bare `python3` in a
verify command on a project with a venv; a criterion that reads as an
operator or post-merge witness; an Evidence section of three or more states
with no `**UI change:**` line and no Mock-up (declare `major` and link the
mock-up, or declare `minor`).

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

A named test that does not exist in the tree is unwitnessed. Any criterion
not met or unwitnessed makes the round `changes_requested` whatever the
verdict line says. So:

- Write criteria as "a test witnesses this", and mean it. The full list of
  rules the reviewer enforces sits in the comment above the acceptance
  criteria in `ticketTemplate.md`, where the author reads it before drafting.
- Keep visual passes, re-renders of gitignored output, and "on the writer
  host" checks out of the criteria; they are operator steps, recorded in
  the ledger after the merge.
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

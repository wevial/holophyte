# Tickets as contracts

A ticket is the whole specification the loop works from. Its body is frozen
at claim, held against at merge, and read by a reviewer who can witness
only what is in the candidate tree. Writing one well is most of the
operator's job.

## The shape

`ticketTemplate.md` is the canonical structure and `ticket_template.py`
validates it. Sections, in order: an H1 title; Summary; What / Why / How
(the bold keys `**What:**`, `**Why:**`, `**How:**`, plain `What:` also
accepted); In scope (at most three entries); Out of scope; Acceptance
criteria (at most five, each `Given … when … then …`); Verify command(s)
(a fenced block of relative-path, non-interactive commands; exit 0 is
pass); optional Contract checks (`relative/path: exact literal`);
Implementation notes; Estimate & dependencies (`Estimate: N min · Depends
on: KO-n` or `none`, 30 minutes at most); Open questions (exactly
`- None` to be claimable).

Validate before filing, against the target repository:

```
python3 ticket_template.py TICKET.md --repo /path/to/target
```

Blockers make the ticket INVALID and the loop skips it as `needs_spec`.
Advisories print and let it through.

## What the validator refuses, and why

| Refusal | Why it exists |
| --- | --- |
| Unfilled placeholder: any `<…>` or `{{…}}` outside a markdown link | KO-165 was claimed with template placeholders in its title and criteria and merged anyway. HTML tags count; write "the `main` element", not `<main>`. |
| More than three in-scope items, five criteria, or 30 minutes | KO-110 was a 180-minute blob. Small tickets converge; big ones burn rounds. |
| A non-relative path in a verify command | KO-111 `cd`'d to an absolute path and verified the wrong tree. |
| A path a criterion names that the target repository gitignores | KO-166 named a rendered file under a gitignored `artifacts/`; the reviewer's export cannot contain it and the implementer force-tracked it. |
| A `Depends on` that is not a ticket id or `none` | dependencies are machine-checked through Linear `blocks` relations |
| Open questions not exactly `- None` | an open question is not a frozen contract |

Advisories: a `What:` that chains two deliverables; a bare `python3` in a
verify command on a project with a venv; a criterion that reads as an
operator or post-merge witness.

## What the reviewer can witness

The reviewer sees a clean export of the candidate commit, read-only, and
nothing else: not `main` after the merge, not the writer host, not a
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

- Write criteria as "a test witnesses this", and mean it.
- Keep visual passes, re-renders of gitignored output, and "on the writer
  host" checks out of the criteria; they are operator steps, recorded in
  the ledger after the merge.
- Verify commands must be true of the candidate, not of a fixture you
  imagined: a negated grep that also matches a legitimate line (`---`)
  and a plural that a one-item fixture cannot produce both failed real
  runs.

## Filing and editing

```
python3 factory.py TARGET --file-ticket TICKET.md --priority high [--state Todo|Backlog]
python3 factory.py TARGET --file-ticket TICKET.md --update KO-n
```

Both validate the file against the target, act on Linear, read the stored
body back and validate that again, so a transfer that rewrites bold or
autolinks an example identifier is caught at filing time rather than at
claim time. Exit 1 means nothing was changed; exit 2 means the issue exists
but its stored body needs a fix. Todo and In Progress are claimable;
Backlog and Done are not. `[loop] order = "priority"` makes an Urgent or
High ticket run first.

## The ledger

Every run leaves a comment on its ticket: the rounds, their findings, the
adjudications (`ADDRESS`, `FOLLOW_UP`, `DECLINE`), and any operator step
taken after the merge with its time. The store holds the rows;
`FINDINGS.md` renders the window; the ledger is the narrative. A contract
revision is recorded there too, with what was wrong and why, so a rerun's
reviewer can read the history.

## Tickets the loop must not take

Tracking and design tickets have no verify command by design and stay in
Backlog. A parent stays Backlog while its leaves run. A ticket whose
contract needs a human decision is Backlog until the decision is in the
body.

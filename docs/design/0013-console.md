# The console's shape

**Status:** accepted · 2026-09-05 (opened 2026-09-04)

## Context

[Note 5](0005-frontend-before-rust.md) settles that the console is a page
served by the daemon, read-only first. What it looks like was open. Five
mocks existed, all on the same real data; Ko's reaction on 2026-09-04 was
that none of them was it, and that the answer was dashboard-like, "for the
ability to see into what is going on".

On 2026-09-05 Ko brought a high-fidelity design made with Claude Design.
It is kept under [console/](console/README.md) with its screenshots. It
answers, in order: what needs a human right now, what runs are on the
floor and how they are doing, where tickets sit on the path to merge, and
whether the hosts and daemons are alive.

## Decision

The design is the shape. One window, a left rail and four views:

- **Now** (default): a needs-you band over `/attention` with filter chips
  and a four-row cap, a resolved-today fold, then the floor: one block per
  project with its runs, each showing phase, time box, heartbeat and
  strikes, expandable to a round timeline, open findings, files touched
  and the run log.
- **Board**: tickets by state, left to right the path to merge. Linear
  stays the board; this view is a courtesy and is filed last.
- **Hosts**: one card per host with daemon, supervisor and runs.
- **Shipped**: the merge ledger newest first, grouped by day, with
  rounds, findings, actual against time box and the merge sha.

The rail lists projects and hosts, filters every view to one project, and
carries the theme toggle. The console says "project" where the code says
target. Writes render disabled until the serve token exists.

Two things the handoff did not settle, settled here: the design ships a
paper theme only, and the console adds a dark theme with the same care;
and the handoff assumed Electron, which is a wrapper, not the app (note
5, amended).

## What the daemon must grow

The design reads fields the daemon does not serve yet, all additive:
run strikes, round, start and title on `/status`; a `daemon` block with
uptime; `asked_ms` on blocked items; a newest-first merge list with
findings counts; a run-detail endpoint over the store's rounds and
events; files touched from git; a peers list so one page reaches every
daemon; and the ledger window that [note 9](0009-ledger.md) already
plans. The board needs the provider's blocks relations.

## Tickets

Filed 2026-09-05 in two tracks, daemon and renderer, interleaved so the
first renderer ticket runs against the daemon as it is. The handoff is
kept under [console/](console/README.md).

Daemon: KO-263 (`/status` run fields and the daemon block), KO-264
(`/attention` asked time, run, attempt), KO-265 (the daemon serves
`console/dist/`), KO-269 (`/runs/N`), KO-272 (`/runs/N/files`), KO-274
(`/shipped`), KO-276 (`[console] daemons`, `/peers`, CORS); in Backlog
KO-278 (`/ledger` window, after the ledger notes) and KO-280 (`/board`,
low).

Renderer: KO-266 (scaffold), KO-267 (shell and themes), KO-268
(needs-you band), KO-270 (floor), KO-271 (run detail), KO-273 (files and
run log), KO-275 (Shipped), KO-277 (hosts and fan-out); in Backlog
KO-279 (threads and resolved fold), KO-281 (Board, low), KO-282
(Electron wrapper).

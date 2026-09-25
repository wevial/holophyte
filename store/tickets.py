"""store.tickets: the ticket state machine and §2's pickability predicate.

Moved verbatim out of `store/__init__.py` (KO-392): `ensure_project()` for
the repo's projects row, the §3 `TICKET_TRANSITIONS`/`TICKET_STATUSES`
table with `transition()`/`walk_ticket()` and their `IllegalTransition`,
`mirror_ticket()`, `pickable()`/`pickable_tickets()` and the `Pickability`
verdict they return, the Mermaid `render_state_graph()`/
`render_state_graph_section()` renderer and its `STATE_GRAPHS` registry,
and the private helpers only they call. `_json_list` is shared with the
run API and stays in the package, which re-exports every public name so
`store.transition()` keeps working.
"""
from __future__ import annotations

import collections
import json
import time
from pathlib import Path

from . import _json_list
from . import enums as _enums
from .enums import TicketStatus as _Status
from .project_paths import canonical_projects
from .revisions import record_board_fields
from .schema import _transaction


def ensure_project(conn, linear_team_id, repo_path, default_branch="main",
                   autonomy_profile="personal"):
    """Return the id of the projects row for `linear_team_id`, creating it once.

    Every other write in this module needs a `projectId`, and §2 gives no
    other way to make the first one: a loop starting against a fresh store has
    to be able to bootstrap its own project row. `linearTeamId` is the row's
    natural key here because it is the table's UNIQUE column, so a second call
    for the same team returns the existing row rather than racing a duplicate.

    Existing rows are returned untouched. Re-pointing a project at another repo
    path or another autonomy profile is a policy change, not a side effect of
    starting a loop, so it is not done here.
    """
    path = str(Path(repo_path).resolve())
    with _transaction(conn):
        canonical_projects(conn)
        row = conn.execute(
            "SELECT id FROM projects WHERE linearTeamId = ?", (linear_team_id,)
        ).fetchone()
        if row is not None:
            return row[0]
        return conn.execute(
            "INSERT INTO projects"
            " (linearTeamId, repoPath, defaultBranch, autonomyProfile)"
            " VALUES (?, ?, ?, ?)",
            (linear_team_id, path, default_branch, autonomy_profile),
        ).lastrowid


def register_project(conn, linear_team_id, repo_path):
    """Explicit registration: a new row, or the adoption of the row that
    already holds this team at this canonical path -- the row a loop's
    `ensure_project()` wrote before the project was registered. A row is
    recorded as `register_project` once, however often it is adopted.
    The same team at another path, or the path under another team, is
    refused naming the row."""
    path = str(Path(repo_path).resolve())
    with _transaction(conn):
        paths = canonical_projects(conn)
        row = next((row for row in conn.execute(
            "SELECT id, repoPath, linearTeamId FROM projects ORDER BY id")
            if row[2] == linear_team_id or paths[row[0]] == path), None)
        if row and row[2] == linear_team_id and paths[row[0]] == path:
            project = row[0]
            if conn.execute(
                    "SELECT 1 FROM interventions WHERE projectId = ?"
                    " AND action = 'register_project'", (project,)).fetchone():
                return project
        elif row:
            raise ValueError(f"project {row[0]} already registered: {row[1]}")
        else:
            project = ensure_project(conn, linear_team_id, path)
        conn.execute(
            'INSERT INTO interventions (projectId, source, "trigger", action, note, at)'
            " VALUES (?, 'human', 'manual', 'register_project', ?, ?)",
            (project, f"registered {path}", int(time.time() * 1000)))
        return project


def list_projects(conn):
    """Return projects in stable name/path order, including their newest run."""
    rows = conn.execute(
        "SELECT id, repoPath, admission, holdNote, "
        "(SELECT id FROM runs WHERE projectId = projects.id "
        "ORDER BY startedAt DESC, id DESC LIMIT 1) FROM projects").fetchall()
    return sorted(rows, key=lambda row: (Path(row[1]).name, row[1], row[0]))


def set_admission(conn, project_id, admission, note):
    """Set admission with the same recorded actions as the legacy flags."""
    from .operate import _set_admission
    actions = {"enabled": "release_hold", "held": "hold", "disabled": "disable"}
    if admission not in actions:
        raise ValueError(f"unknown admission {admission!r}")
    return _set_admission(conn, project_id, note, admission, actions[admission])


# The §3 status diagram, transcribed edge for edge, plus the one edge below
# that the diagram does not draw:
#
#     needs_spec → ready → in_flight → merged
#                     ↕         ↓        ↘
#              blocked_on_deps  │   abandoned
#                     ↕         │
#             blocked_on_operator ←┘
#
# Read as data so the table below can be diffed against the drawing: each key
# is a status, each value the statuses it may move to. `merged` and
# `abandoned` are terminal, so they map to empty sets rather than being left
# out — a missing key and "nowhere to go" would otherwise be the same lookup.
#
# The one reading the drawing does not spell out is the `↘`, which hangs off
# the `in_flight → merged` edge: a run either merges or is given up on, and
# `merged` is done (§3's table: "done"), so it is `in_flight → abandoned`, not
# `merged → abandoned`.
#
# `in_flight → blocked_on_operator` is the added edge, and it is Holophyte's
# rather than the doc's: §3 gives an in-flight ticket only the two endings
# above, so a ticket the loop keeps failing on had nowhere to go that says
# "stop claiming this, a human owns it now". `abandoned` will not do — it is
# terminal, and a repeatedly failing ticket is not a decision to give up, it is
# a decision nobody has made yet. `blocked_on_operator` already means exactly
# that, and already has the way back out (→ blocked_on_deps → ready) an
# unblocking human needs, so failure-pattern escalation walks into it from
# in_flight instead of inventing a seventh status.
#
# A status is not in its own set: `ready → ready` is refused like any other
# non-edge, so a no-op status write cannot pass for a real transition.
TICKET_TRANSITIONS = {
    _Status.NEEDS_SPEC.value: frozenset({_Status.READY.value}),
    _Status.READY.value: frozenset({
        _Status.IN_FLIGHT.value,
        _Status.BLOCKED_ON_DEPS.value}),
    _Status.IN_FLIGHT.value: frozenset({
        _Status.MERGED.value, _Status.ABANDONED.value,
        _Status.BLOCKED_ON_OPERATOR.value}),
    _Status.BLOCKED_ON_DEPS.value: frozenset({
        _Status.READY.value,
        _Status.BLOCKED_ON_OPERATOR.value}),
    _Status.BLOCKED_ON_OPERATOR.value: frozenset({_Status.BLOCKED_ON_DEPS.value}),
    _Status.MERGED.value: frozenset(),
    _Status.ABANDONED.value: frozenset(),
}

# The status vocabulary is shared by the graph and the SQLite constraint.
TICKET_STATUSES = tuple(e.value for e in _enums.TicketStatus)


def render_state_graph(transitions):
    """Render a `{state: {next, ...}}` table as Mermaid `stateDiagram-v2` text.

    Pure and deterministic: nodes are the table's keys in sorted order, edges
    are every `(from, to)` pair in sorted order, one per line, and nothing
    else is drawn. README embeds the output for `TICKET_TRANSITIONS` and
    `RUN_PHASE_TRANSITIONS` between marker comments, and a test re-renders
    both from the live tables and asserts byte equality, so the drawing
    cannot drift from the code the way the prose diagram did. A state that
    appears only as a target is still declared as a node by its own key, so
    a table missing a key renders that state as an edge end only.

    `python3 store.py --state-graph` prints both blocks with their markers,
    ready to paste over README's sections.
    """
    lines = ["stateDiagram-v2"]
    lines.extend(f"    {state}" for state in sorted(transitions))
    lines.extend(f"    {src} --> {dst}"
                 for src in sorted(transitions)
                 for dst in sorted(transitions[src]))
    return "\n".join(lines) + "\n"


# The README sections `--state-graph` prints, in README order: marker name to
# the table it draws. The markers are HTML comments so GitHub renders only the
# fenced Mermaid between them.
STATE_GRAPHS = (
    ("state-graph: tickets", "TICKET_TRANSITIONS"),
    ("state-graph: runs", "RUN_PHASE_TRANSITIONS"),
)


def render_state_graph_section(name, transitions):
    """The exact README text for one marked section, markers included."""
    return (f"<!-- {name} -->\n```mermaid\n{render_state_graph(transitions)}"
            f"```\n<!-- end {name} -->\n")


class IllegalTransition(ValueError):
    """A ticket status or run phase edge the graph refuses; nothing was written.

    Also raised for an unknown target status and for a ticket id that does not
    exist — neither names an edge of the diagram, and both are the same
    mistake from the caller's side: a status change that will not happen.
    """

    def __init__(self, run_id, previous=None, phase=None):
        # Ticket callers retain their existing message-only exception API.
        if previous is None:
            super().__init__(run_id)
        else:
            super().__init__(
                f"run {run_id}: illegal phase transition {previous} -> {phase}")
            self.run_id, self.previous, self.phase = run_id, previous, phase


def transition(conn, ticket_id, to_status):
    """Move `ticket_id` to `to_status`; return the status it came from.

    Legality is `TICKET_TRANSITIONS`, i.e. state-model §3, and nothing else.
    An illegal move raises `IllegalTransition` and leaves the row untouched.

    One `_transaction()` for the same reason as `claim()`: this is a read
    (where is the ticket now?) followed by a write (move it), and two
    concurrent callers must not both read the same `from` status and both act
    on it. `BEGIN IMMEDIATE` takes the write lock up front, so they serialize
    and the second one validates against the status the first one wrote. When
    the caller already owns a transaction the move joins it and commits with
    it, so a `transaction()` block can record a status change together with
    its other writes or not at all.

    The previous status is returned because the caller usually has to log or
    mirror the change, and re-reading it afterwards cannot recover it.
    """
    with _transaction(conn):
        row = conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise IllegalTransition(f"ticket {ticket_id} does not exist")
        (from_status,) = row
        if to_status not in TICKET_TRANSITIONS.get(from_status, frozenset()):
            raise IllegalTransition(
                f"ticket {ticket_id}: {from_status} -> {to_status} is not a"
                " transition the state-model §3 diagram draws"
            )
        conn.execute(
            "UPDATE tickets SET status = ? WHERE id = ?", (to_status, ticket_id)
        )
        if from_status == "blocked_on_operator":
            conn.execute("UPDATE runs SET parkKind = NULL WHERE ticketId = ?",
                         (ticket_id,))
    return from_status


def _status_path(from_status, to_status):
    """Shortest §3 path as the statuses to walk through, or None."""
    frontier, seen = [(from_status, ())], {from_status}
    while frontier:
        status, path = frontier.pop(0)
        for nxt in sorted(TICKET_TRANSITIONS[status]):
            if nxt == to_status:
                return (*path, nxt)
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, (*path, nxt)))
    return None


def walk_ticket(conn, ticket_id, to_status):
    """Move `ticket_id` to `to_status` along §3 edges; return the path taken.

    The operator's `transition()`. The diagram stays the only authority: the
    walk is a shortest path over `TICKET_TRANSITIONS` taken edge by edge
    through `transition()` inside one `_transaction()`, so no edge exists
    here that the diagram does not draw — and the KO-146-style repair
    ("ready but the work is merged") is one call instead of a hand-found
    path taken in raw SQL. Already there returns `()`; no path raises
    `IllegalTransition` before any edge is taken, so nothing is written.
    A path may transit blocked statuses where the diagram routes through
    them (`in_flight -> ready` passes `blocked_on_operator`): the walk is
    §3 legality, and what a transit status *means* stays the caller's
    judgment.
    """
    if to_status not in TICKET_TRANSITIONS:
        raise IllegalTransition(f"unknown status {to_status!r}")
    with _transaction(conn):
        row = conn.execute("SELECT status FROM tickets WHERE id = ?",
                           (ticket_id,)).fetchone()
        if row is None:
            raise IllegalTransition(f"ticket {ticket_id} does not exist")
        (from_status,) = row
        if from_status == to_status:
            return ()
        path = _status_path(from_status, to_status)
        if path is None:
            raise IllegalTransition(
                f"ticket {ticket_id}: no §3 path from {from_status}"
                f" to {to_status}")
        for status in path:
            transition(conn, ticket_id, status)
    return path


def mirror_ticket(
    conn,
    project_id,
    linear_issue_id,
    linear_identifier,
    title,
    acceptance_criteria=(),
    verification_commands=(),
    time_box_ms=None,
    affinity="any",
    depends_on=None,
    now=None,
    body="",
    url=None,
    board_state=None,
    priority=None,
    labels=None,
    board_column=None,
    filed_at=None,
    board_updated_at=None,
    expected_revision=None,
):
    """Upsert the Holophyte mirror of a Linear issue; return its ticket id.

    `body`, `url` and `board_state` are refreshed on every mirror. The body is the
    text the loop read, served by `ticket_by_identifier()` rather than live Linear.
    A missing body is empty; a missing URL or board state is null.

    `priority`, `labels`, `board_column`, `filed_at` and `board_updated_at`
    are the board's other fields (KO-736), and None for each means what it
    means for `depends_on`: no opinion, keep what the row holds. Title, body,
    priority, labels and column are the board-owned fields a revision holds:
    the stored row is first healed against its latest revision (authored
    `unrecorded`, for an older build's write that recorded none), then
    written, then recorded as the next revision (authored `board`) when it
    changed -- so an older build's edit and this one are two revisions.

    The routing rule, state-model §2: a ticket lacking acceptance criteria or
    a verification command is **not pickable**, so a new one lands in
    `needs_spec`; one carrying both lands in `ready`. That is a data
    invariant, not a prompt instruction — the loop cannot pick up an
    under-specced ticket because the store never gives it a pickable one.
    The criteria/command *defaults are empty* for the same reason: a caller
    that forgets to pass them gets the unpickable answer, not the pickable one.

    On re-mirror the Linear-owned fields are refreshed and the status is left
    alone, with one exception: between `needs_spec` and `ready` the status
    follows the body, in both directions. Those two are the statuses §2
    derives from the body alone — `ready` *means* the row carries both lists
    — so a ticket promoted once the body finally carries them is demoted
    again if an edit empties them, or if the caller withholds them because
    the body failed the template (KO-188): a row that says `ready` about a
    contract it no longer holds is the invariant broken. Neither ticket is
    anybody's work yet. §1 gives Holophyte the in-flight substate, so a
    mirror push may not drag a running ticket backwards, and every other
    status change is somebody's decision and goes through `transition()`.
    In particular `blocked_on_deps → ready` is *not* taken here: it is the
    dependency resolver's call, not a side effect of a body edit.

    `depends_on=None` (the default) means the caller has no opinion about the
    dependency list, not that the list is empty: a new ticket gets `[]`, and a
    re-mirror keeps whatever the row already holds. The loop's claim-time
    re-mirror carries the live body and nothing about dependencies — the
    provider does not parse them — so a default that wrote `[]` would clear a
    blocked ticket's list in the very row `pickable()` reads next, and the
    gate would let it through. A caller that does know the list passes it,
    `[]` included, and that replaces the stored one.

    `expected_revision` is the revision a store-built task was read at
    (Phase 3 stage 3): when the row has moved past it, the task is older
    than the row, so nothing is written -- no heal, no update, no
    revision -- and the id is returned. None, the default, always writes.

    Lookups are scoped to `project_id`, so re-mirroring another project's
    issue does not overwrite it — it fails on the `linearIssueId` uniqueness
    constraint instead. `now` is epoch milliseconds for `mirroredAt`,
    defaulting to the clock.

    The upsert runs in one `_transaction()`, so it joins a transaction the
    caller already owns rather than opening its own and commits with it — the
    argument validation above still raises before any transaction is touched.
    """
    criteria = _json_list("acceptance_criteria", acceptance_criteria)
    commands = _json_list("verification_commands", verification_commands)
    depends = None if depends_on is None else _json_list("depends_on", depends_on)
    labels = None if labels is None else _json_list("labels", labels)
    specced = bool(json.loads(criteria)) and bool(json.loads(commands))
    derived = "ready" if specced else "needs_spec"
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT id, status, revision FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (linear_issue_id, project_id),
        ).fetchone()
        if row is not None and expected_revision not in (None, row[2]):
            return row[0]
        if row is None:
            ticket_id = conn.execute(
                "INSERT INTO tickets"
                " (projectId, linearIssueId, linearIdentifier, title, body,"
                "  status, acceptanceCriteria, verificationCommands, timeBoxMs,"
                "  affinity, dependsOn, mirroredAt, url, boardState, priority,"
                "  labels, boardColumn, filedAt, boardUpdatedAt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id, linear_issue_id, linear_identifier, title,
                    body, derived, criteria, commands, time_box_ms, affinity,
                    "[]" if depends is None else depends, now, url, board_state,
                    priority, "[]" if labels is None else labels, board_column,
                    filed_at, board_updated_at,
                ),
            ).lastrowid
        else:
            ticket_id, status, _ = row
            if status in ("needs_spec", "ready"):
                status = derived
            record_board_fields(conn, ticket_id, "unrecorded", now)
            conn.execute(
                "UPDATE tickets SET linearIdentifier = ?, title = ?, body = ?,"
                " status = ?, acceptanceCriteria = ?, verificationCommands = ?,"
                " timeBoxMs = ?, affinity = ?,"
                " dependsOn = COALESCE(?, dependsOn), mirroredAt = ?,"
                " url = ?, boardState = ?, priority = COALESCE(?, priority),"
                " labels = COALESCE(?, labels),"
                " boardColumn = COALESCE(?, boardColumn),"
                " filedAt = COALESCE(?, filedAt),"
                " boardUpdatedAt = COALESCE(?, boardUpdatedAt)"
                " WHERE id = ?",
                (
                    linear_identifier, title, body, status, criteria, commands,
                    time_box_ms, affinity, depends, now, url, board_state,
                    priority, labels, board_column, filed_at, board_updated_at,
                    ticket_id,
                ),
            )
        record_board_fields(conn, ticket_id, "board", now)
    return ticket_id


# The answer `pickable()` gives back. A namedtuple so a caller that wants the
# diagnostics can read `.reason`, with `__bool__` overridden so the common
# `if store.pickable(conn, t):` reads the verdict and not the mere existence
# of the tuple — a plain namedtuple is always truthy, which would turn every
# unpickable ticket into a pickable one at the one call site that matters.
class Pickability(collections.namedtuple("Pickability", ("pickable", "reason"))):
    """`bool(...)` is the predicate; `.reason` names the clause that failed.

    `reason` is None exactly when the ticket is pickable, and otherwise a
    human-readable string naming the *first* failing clause in the order §2
    writes them — a diagnostic, not a parseable code.
    """

    __slots__ = ()

    def __bool__(self):
        return self.pickable


def pickable(conn, ticket_id):
    """Is `ticket_id` claimable right now? Returns a `Pickability`.

    State-model §2's predicate, one function and one truth, transcribed
    clause for clause::

        status == 'ready'
          && activeRunId == null
          && acceptanceCriteria.length > 0
          && verificationCommands.length > 0
          && all(dependsOn).status == 'merged'

    The two list clauses are re-checked here rather than trusted to
    `mirror_ticket()`'s `needs_spec` routing. The rule is that an
    under-specced ticket is not pickable *ever*, and a status column that
    somebody wrote directly is not evidence about the lists — so the
    predicate reads the lists.

    `dependsOn` holds linearIssueIds, resolved within the ticket's own
    project. Empty passes vacuously. A dep naming an issue this store has not
    mirrored is *not* pickable: an unmirrored dep cannot be shown to be
    merged, and the fail-closed answer is the safe one for a gate. Cycle
    detection is out of scope (the doc defers it); a cycle here simply means
    neither ticket is ever pickable, which is the honest answer.

    A ticket id that does not exist is not pickable either, for the same
    reason: the gate answers "no" rather than raising, since every caller is
    asking whether to start work.

    Read-only, and deliberately not transactional. This is the gate's
    question, not its answer: `claim()` takes `BEGIN IMMEDIATE` and re-asserts
    the lease it needs, so a ticket that goes unpickable between the two calls
    loses at the claim, not on a lock held here.
    """
    row = conn.execute(
        "SELECT projectId, status, activeRunId, acceptanceCriteria,"
        " verificationCommands, dependsOn FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return Pickability(False, f"ticket {ticket_id} does not exist")
    project_id = row[0]

    def dep_status(dep):
        dep_row = conn.execute(
            "SELECT status FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (dep, project_id),
        ).fetchone()
        return None if dep_row is None else dep_row[0]

    return _pickability(row, dep_status)


def pickable_tickets(conn, project_id):
    """`pickable()` asked of every ticket in `project_id` at once, in one
    read: `{linearIdentifier: Pickability}`.

    The scheduler's tick (KO-343) counts how many of the board's ready
    listing a worker could claim, and the ticket holds it to one store read
    per tick: `pickable()` per listed ticket is a row fetch plus a select
    per dependency each. So the project's rows -- every status, since a
    dependency is resolved against a merged sibling -- are fetched once
    and §2's clauses are evaluated over them in memory, the dependency
    lookup answered from the same rows. The same `_pickability()` as the
    one-ticket predicate, so the two cannot disagree on a clause.

    Read-only and, like `pickable()`, not transactional: `claim()`
    re-asserts the lease.
    """
    rows = conn.execute(
        "SELECT projectId, status, activeRunId, acceptanceCriteria,"
        " verificationCommands, dependsOn, linearIssueId, linearIdentifier"
        " FROM tickets WHERE projectId = ?",
        (project_id,),
    ).fetchall()
    status_of = {row[6]: row[1] for row in rows}
    return {row[7]: _pickability(row[:6], status_of.get) for row in rows}


def _pickability(row, dep_status):
    """Evaluate §2's clauses over one already-fetched `tickets` row.

    Split out from `pickable()` so the row fetch and the predicate stay
    separate: `pickable_tickets()` evaluates it over a project's rows
    without re-deriving §2 in SQL. `dep_status(linearIssueId)` answers the
    dependency clause -- the dependency's status, or None when this store
    has not mirrored it.
    """
    project_id, status, active_run_id, criteria, commands, depends_on = row
    if status != "ready":
        return Pickability(False, f"status is {status}, not ready")
    if active_run_id is not None:
        return Pickability(False, f"run {active_run_id} is already active on it")
    if not json.loads(criteria):
        return Pickability(False, "it has no acceptance criteria")
    if not json.loads(commands):
        return Pickability(False, "it has no verification commands")
    for dep in json.loads(depends_on):
        dep_state = dep_status(dep)
        if dep_state is None:
            return Pickability(False, f"it depends on {dep}, which is not mirrored")
        if dep_state != "merged":
            return Pickability(
                False, f"it depends on {dep}, which is {dep_state}, not merged"
            )
    return Pickability(True, None)

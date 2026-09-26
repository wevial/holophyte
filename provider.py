"""The board seam: one protocol, two boards.

`factory.py` never names a board. It is handed a `Board` and drives it
through its members -- `team`, `claim_next()`, `ready_issues()`,
`fetch_task()`, `set_state()`, `comment()`, `closed_identifiers()`,
`states()`, `label_issue()`, `issue_labels()` and `unlabel_issue()`, and files a ticket
through `file()`, `update()` and `stored_body()` -- so which board a loop
runs against is the caller's choice, not a module import: every caller
that needs the target's board asks `board_for(target)`, the one place that
reads `[board] kind`. Two boards ship here: `LinearBoard`, which wraps the
functions `linear_provider.py` already has, and `FileProvider`, a
directory of ticket files for tests and offline runs. The conformance suite
in `tests/test_provider.py` holds both to the same observable behavior.

Kept deliberately plain for the Rust port -- a protocol with dict payloads,
no metaclass, no dispatch on module names.

Task dicts are the shape `linear_provider.parse_task()` produces, and both
boards hand over the same keys:

    id          the human identifier ("KO-12"); branches and prints use it
    issue_id    the board's canonical id for the ticket; the store mirrors
                under it and re-reads by it (the Linear UUID; for the file
                board, the identifier again -- a file has no second name)
    title       the ticket's title
    body        the description as approved, verbatim, in ticketTemplate.md
                shape (H1 title line included: the claim-time validator
                wants it)
    verify      the `## Verify command(s)` fence's contents, or None
    criteria    every acceptance-criteria list entry, checked ones included
    contracts   the `## Contract checks` pairs, [] when the section is absent
    budget_min  the time box in minutes
    priority    Linear's 0-4 priority integer on the Linear board (0/None
                is unprioritised); the file board has no such field
    labels      the names of the ticket's labels, [] without any; the loop
                reads another writer's `holo:` lease label here (KO-351)

FileProvider's on-disk format
-----------------------------

    <root>/<IDENT>.md           the ticket: first line `# <title>`, then the
                                body in ticketTemplate.md shape (the whole
                                file is the body the loop is handed)
    <root>/<IDENT>.state        the workflow state name (`In Progress`,
                                `Done`, ...); absent means `Todo`
    <root>/<IDENT>.comments.md  comments, appended in order: a `## <UTC
                                timestamp>` line, a blank line, the body, a
                                blank line
    <root>/<IDENT>.labels       the ticket's labels, one name per line;
                                absent means none
    <root>/<IDENT>.title        the title `file()`/`update()` was given,
                                when it is not the body's H1; absent means
                                the H1
    <root>/<IDENT>.estimate     likewise the estimate in minutes, when it is
                                not the body's `Estimate:`; absent means that

A ticket file's name has exactly one dot (`KO-12.md`), which is what keeps
`KO-12.comments.md` from reading as a ticket called `KO-12.comments`. The
directory's name is the board's `team`. `claim_next()` offers the lowest
identifier -- plain string order, the order `linear_provider.claim_next()`
sorts in by default -- whose state is `Todo` and not in `skip`; a file has
no priority, so `order="priority"` is accepted and orders the same way. The
file board has no blocking relations: a ticket that must wait is one a
human leaves out of `Todo`. `budget_min` is the body's `Estimate: N min`
line, or 20 without one; a file has no other estimate field.

`file()` writes a new ticket as `<team>-<n>.md`, `n` one above the highest
number already filed under that prefix (1 on an empty board), and its
state to `<team>-<n>.state` unless it is `Todo`. The file is the body as
given; a `title` or `estimate` that differs from the body's own goes to
the `.title`/`.estimate` file, so the task answers the fields the call
named, as Linear's does. `priority` is ignored -- a ticket file has no
priority field. With no blocking relations to record, a non-empty
`blockers` is refused before anything is written.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import ticket_template
from holophyte.config_tables import board_config, board_mode

# The same fence `linear_provider.parse_task()` reads, so a body parsed by
# either board yields the same `verify`.
VERIFY_RE = re.compile(r"## Verify command\(s\)\s*```[^\n]*\n(.*?)```", re.S)
DEFAULT_BUDGET_MIN = 20
DEFAULT_STATE = "Todo"
# The file board's closed state names and the Linear state *type* each one
# is: `closed_identifiers()` answers in types on both boards.
CLOSED_STATE_NAMES = {"Done": "completed", "Canceled": "canceled",
                      "Cancelled": "canceled", "Duplicate": "canceled"}
# What `states()` says of an identifier the board holds no issue for.
GONE = "gone"


class Board(Protocol):
    """What the loop asks of a board. Task dicts: see the module docstring."""

    @property
    def team(self) -> str:
        """The board's identifier, recorded by `store.tickets.ensure_project()`."""
        ...

    def claim_next(self, skip=(), order="identifier") -> dict | None:
        """The first ready task whose `id` is not in `skip`; None when none.
        `order` is `[loop] order` (`"identifier"` or `"priority"`); the
        listing the ask chose from lands on `self.last_listing` (KO-425)."""
        ...

    def ready_issues(self) -> list[dict]:
        """Every task `claim_next()` would choose from, parsed, in no
        promised order; `updatedAt` is epoch ms or None. Raise on read failure."""
        ...

    def listing(self) -> list[dict]:
        """The ready column with its blocked tasks too, each carrying
        `blocked_by`: the `issue_id`s of its still-open blockers (KO-743).
        What a store-mode queue mirror writes `dependsOn` from."""
        ...

    def fetch_task(self, issue_id) -> dict | None:
        """The ticket as the board holds it now, with its `column` as
        `states()` names it and `updatedAt`; None when it has no such issue."""
        ...

    def set_state(self, issue_id, state_name) -> None:
        """Move the ticket to the workflow state called `state_name`; raise
        when the move did not happen."""
        ...

    def comment(self, task_id, body) -> None:
        """Record `body` as a comment on the ticket named by either id."""
        ...

    def closed_identifiers(self, identifiers) -> dict[str, str]:
        """Which of `identifiers` the board holds closed, as identifier ->
        `"completed"` or `"canceled"`; open or unknown is absent."""
        ...

    def states(self, identifiers) -> dict[str, dict]:
        """Each of `identifiers` as the board holds it, as identifier ->
        `{"state", "name", "column"}`: `open` with its workflow state name
        and column (`ready` or `backlog`), `completed` (column None),
        `canceled` (column `canceled`), or `GONE` (name and column None)
        where the board holds no such issue. Gone is said only on a
        complete, successful answer; raise when the board could not be
        asked. An identifier the board could not name is left out."""
        ...

    def label_issue(self, issue_id, name) -> None:
        """Add the label `name` to the ticket, creating it on first use and
        leaving other labels alone; raise when the board did not take it --
        the loop gives the store lease back then."""
        ...

    def issue_labels(self, issue_id) -> list[str]:
        """The ticket's labels as the board holds them now -- the read-back
        a lease write is judged by (KO-351)."""
        ...

    def unlabel_issue(self, issue_id, name) -> None:
        """Remove the label `name` from the ticket; raise when the board refused."""
        ...

    def file(self, title, body, estimate, state, priority=None,
             blockers=()) -> str:
        """Create the ticket in the workflow state named `state`, record each
        of `blockers` (identifiers) as blocking it, and answer the new
        identifier. `priority` is the board's integer, or None for none."""
        ...

    def update(self, identifier, title, body, estimate,
               blockers=()) -> tuple[list[str], list[str]]:
        """Replace the ticket's title, body and estimate, leaving its state
        and priority alone; then record the `blockers` the board does not
        hold yet. Answer `(added, kept)`: the blockers recorded, in
        `blockers` order, and the ones the board already held that
        `blockers` does not name, in the board's order -- left in place.
        Raise `RuntimeError` for an identifier the board does not hold."""
        ...

    def stored_body(self, identifier) -> str:
        """The body as the board stores it -- the read-back a filing is
        judged by. Raise `RuntimeError` for an identifier the board does
        not hold."""
        ...


class LinearBoard:
    """Linear, through the functions `linear_provider.py` already has.

    `project_id`, `team` and `label` are the target's `[board]` table, as
    `holophyte.config_tables.board_config()` resolves it -- `label` the one
    optional key (KO-432): set, the ready listing keeps only the issues
    carrying it. Stored here and passed to every module call, so the module
    itself holds no board and two targets on one host drive two projects.
    The module is imported at the first call that needs it rather than here
    -- the import reads no configuration, so `--report`, a read-only
    `--sweep` and a trip-less acting sweep never touch it. Construction does
    no I/O; the API key is read by the module on the first request.

    `store_mode` is the target's `[board] mode` being `store`: its status
    pushes are queued on the ticket for the host sweep rather than sent
    inline (KO-740). A board without the attribute reads as False.
    """

    def __init__(self, project_id, team, label=None, *, store_mode=False):
        self.project_id = project_id
        self._team = team
        self._label = label
        self.store_mode = store_mode
        self._module = None
        # The listing the last `claim_next()` saw; None until asked (KO-425).
        self.last_listing = None

    def _linear(self):
        if self._module is None:
            import linear_provider
            self._module = linear_provider
        return self._module

    @property
    def team(self):
        return self._team

    def claim_next(self, skip=(), order="identifier"):
        task, self.last_listing = self._linear().claim_next(
            self.project_id, self._team, skip, order, self._label)
        return task

    def ready_issues(self):
        return self._linear().ready_issues(self.project_id, label=self._label)

    def listing(self):
        return self._linear().listing(self.project_id, label=self._label)

    def open_issues(self):
        """Every open issue, Backlog included, for a board import (KO-751);
        not on `Board`, as only a Linear board is imported from."""
        return self._linear().open_issues(self.project_id, label=self._label)

    def fetch_task(self, issue_id):
        return self._linear().fetch_task(issue_id, label=self._label)

    def set_state(self, issue_id, state_name):
        self._linear().set_state(issue_id, state_name, self._team)

    def comment(self, task_id, body):
        self._linear().comment(task_id, body)

    def closed_identifiers(self, identifiers):
        return self._linear().closed_identifiers(identifiers)

    def states(self, identifiers):
        return self._linear().states(identifiers, label=self._label)

    def label_issue(self, issue_id, name):
        self._linear().label_issue(issue_id, name, self._team)

    def issue_labels(self, issue_id):
        return self._linear().issue_labels(issue_id)

    def unlabel_issue(self, issue_id, name):
        self._linear().unlabel_issue(issue_id, name)

    def file(self, title, body, estimate, state, priority=None, blockers=()):
        linear = self._linear()
        issue = linear.create_issue(self.project_id, self._team, title, body,
                                    estimate, state, priority=priority)
        for blocker in blockers:
            linear.add_blocker(issue["id"], blocker)
        return issue["identifier"]

    def update(self, identifier, title, body, estimate, blockers=()):
        linear = self._linear()
        issue_id = linear.update_issue(identifier, title, body, estimate)
        # Read after the body is stored: a refused update adds nothing, and
        # the difference is taken against the board as it stands then.
        held = linear.blockers_of(identifier)
        added = [b for b in blockers if b not in held]
        for blocker in added:
            linear.add_blocker(issue_id, blocker)
        return added, [b for b in held if b not in blockers]

    def stored_body(self, identifier):
        return self._linear().fetch_description(identifier)


def board_for(target):
    """The target's board: None without a `[board]` table, a `LinearBoard`
    for `kind = "linear"` (the default). `kind = "native"` is refused with
    the `SystemExit` `board_config()` raises for a bad value -- this build
    has no native board, and a native project must not run against Linear.
    """
    mode = board_mode(target)
    if mode.kind == "native":
        raise SystemExit(
            f"[holo2] {target.config_path}: [board] kind \"native\" is not "
            "supported: this build has no native board")
    settings = board_config(target)
    if settings is None:
        return None
    return LinearBoard(*settings, store_mode=mode.mode == "store")


class FileProvider:
    """A directory of ticket files as the board; format in the module docstring."""

    def __init__(self, root):
        self.root = Path(root)
        self.team = self.root.name
        self.last_listing = None

    def _path(self, identifier, suffix=".md"):
        return self.root / f"{identifier}{suffix}"

    def _identifiers(self):
        return sorted(p.stem for p in self.root.glob("*.md")
                      if p.is_file() and "." not in p.stem)

    def _state(self, identifier):
        path = self._path(identifier, ".state")
        return path.read_text().strip() if path.exists() else DEFAULT_STATE

    def _require(self, identifier):
        if "." in identifier or not self._path(identifier).is_file():
            raise RuntimeError(f"no ticket {identifier!r} in {self.root}")

    def claim_next(self, skip=(), order="identifier"):
        # A ticket file has no priority field, so `order = "priority"` is
        # accepted but orders the same way. `last_listing` is the column as
        # this ask saw it, for the empty pass's mirror reconcile (KO-425).
        self.last_listing = [i for i in self._identifiers()
                             if self._state(i) == DEFAULT_STATE]
        for identifier in self.last_listing:
            if identifier in skip:
                continue
            return self.fetch_task(identifier)
        return None

    def ready_issues(self):
        return [self.fetch_task(identifier) for identifier in self._identifiers()
                if self._state(identifier) == DEFAULT_STATE]

    def listing(self):
        # No relations on a file board, so nothing is ever blocked.
        return [dict(task, blocked_by=[]) for task in self.ready_issues()]

    def fetch_task(self, issue_id):
        if "." in issue_id or not self._path(issue_id).is_file():
            return None
        task = parse_body(issue_id, self._path(issue_id).read_text())
        title = self._path(issue_id, ".title")
        if title.exists():
            task["title"] = title.read_text().strip()
        estimate = self._path(issue_id, ".estimate")
        if estimate.exists():
            task["budget_min"] = int(estimate.read_text())
        task["labels"] = self._labels(issue_id)
        task["updatedAt"] = self._path(issue_id).stat().st_mtime_ns // 1_000_000
        task["column"] = _file_column(self._state(issue_id))[1]
        return task

    def _labels(self, identifier):
        path = self._path(identifier, ".labels")
        return path.read_text().splitlines() if path.exists() else []

    def label_issue(self, issue_id, name):
        self._require(issue_id)
        if not str(name).strip():
            raise RuntimeError(f"refused to label {issue_id} with an empty name")
        have = self._labels(issue_id)
        if name not in have:
            self._path(issue_id, ".labels").write_text(
                "".join(f"{label}\n" for label in [*have, name]))

    def issue_labels(self, issue_id):
        self._require(issue_id)
        return self._labels(issue_id)

    def unlabel_issue(self, issue_id, name):
        self._require(issue_id)
        left = [label for label in self._labels(issue_id) if label != name]
        self._path(issue_id, ".labels").write_text(
            "".join(f"{label}\n" for label in left))

    def set_state(self, issue_id, state_name):
        self._require(issue_id)
        if not str(state_name).strip():
            raise RuntimeError(f"refused to move {issue_id} to an empty state")
        self._path(issue_id, ".state").write_text(f"{state_name}\n")

    def comment(self, task_id, body):
        self._require(task_id)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._path(task_id, ".comments.md").open("a") as f:
            f.write(f"## {ts}\n\n{body.rstrip()}\n\n")

    def closed_identifiers(self, identifiers):
        # The file board keeps state *names*; these are the closed ones,
        # typed as Linear types so both boards answer alike.
        return {identifier: CLOSED_STATE_NAMES[self._state(identifier)]
                for identifier in identifiers
                if "." not in identifier and self._path(identifier).is_file()
                and self._state(identifier) in CLOSED_STATE_NAMES}

    def states(self, identifiers):
        # A file board has no labels to filter a column on: a `.state` of
        # Backlog is the backlog column, any other open name the ready one.
        answer = {}
        for identifier in identifiers:
            if "." in identifier:
                continue
            if not self._path(identifier).is_file():
                answer[identifier] = {"state": GONE, "name": None,
                                      "column": None}
                continue
            name = self._state(identifier)
            state, column = _file_column(name)
            answer[identifier] = {"state": state, "name": name,
                                  "column": column}
        return answer

    def _refuse_blockers(self, what, blockers):
        if blockers:
            raise RuntimeError(
                f"refused to {what} blocked by {', '.join(blockers)}: the "
                f"file board at {self.root} has no blocking relations")

    def file(self, title, body, estimate, state, priority=None, blockers=()):
        # Title and estimate live in the body; a file has no priority.
        self._refuse_blockers(f"file {title!r}", blockers)
        prefix = f"{self.team}-"
        numbers = [int(i[len(prefix):]) for i in self._identifiers()
                   if i.startswith(prefix) and i[len(prefix):].isdigit()]
        identifier = f"{prefix}{max(numbers, default=0) + 1}"
        self._write(identifier, title, body, estimate)
        if state != DEFAULT_STATE:
            self._path(identifier, ".state").write_text(f"{state}\n")
        return identifier

    def update(self, identifier, title, body, estimate, blockers=()):
        self._require(identifier)
        self._refuse_blockers(f"update {identifier}", blockers)
        self._write(identifier, title, body, estimate)
        return [], []

    def _write(self, identifier, title, body, estimate):
        # The body verbatim; a title or estimate it does not say beside it,
        # and a stale one from an earlier call removed.
        self._path(identifier).write_text(body)
        own = parse_body(identifier, body)
        for suffix, value, said in (
                (".title", title and title.strip(), own["title"]),
                (".estimate", estimate and int(estimate), own["budget_min"])):
            path = self._path(identifier, suffix)
            if value is None or value == said:
                path.unlink(missing_ok=True)
            else:
                path.write_text(f"{value}\n")

    def stored_body(self, identifier):
        self._require(identifier)
        return self._path(identifier).read_text()


def _file_column(name):
    """A file board state name as `(state, column)`: a closed name is its
    Linear type with column None (completed) or `canceled`, Backlog is the
    backlog column, and any other open name the ready one."""
    state = CLOSED_STATE_NAMES.get(name, "open")
    return state, {"completed": None, "canceled": "canceled"}.get(
        state, "backlog" if name == "Backlog" else "ready")


def parse_body(identifier, text):
    """A ticket body as the task dict, key for key as `parse_task()` builds it:
    the file board's parse, and a store-mode claim's task built from a
    stored body (Phase 3 stage 3).
    Mirrored rather than called because `linear_provider` cannot be imported
    without a configured project, which is the file board's whole case; the
    conformance suite holds the two parses to each other.
    """
    parsed = ticket_template.parse(text)
    m = VERIFY_RE.search(text)
    return {"id": identifier, "issue_id": identifier,
            "title": (parsed.title or identifier).strip(),
            "verify": m.group(1).strip() if m else None,
            "criteria": [*parsed.acceptance, *parsed.acceptance_done,
                         *parsed.acceptance_other],
            "contracts": parsed.contract_checks,
            "body": text,
            "budget_min": parsed.estimate_min or DEFAULT_BUDGET_MIN}

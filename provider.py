"""The board seam: one protocol, two boards.

`factory.py` never names a board. It is handed a `Provider` and drives it
through five members -- `team`, `claim_next()`, `fetch_task()`, `set_state()`
and `comment()` -- so which board a loop runs against is the caller's choice
(`factory.cli()` builds a `LinearProvider`), not a module import. Two boards
ship here: `LinearProvider`, which wraps the functions `linear_provider.py`
already has, and `FileProvider`, a directory of ticket files for tests and
offline runs. The conformance suite in `tests/test_provider.py` holds both to
the same observable behavior.

Kept deliberately plain for the Rust port -- a protocol with dict payloads,
no metaclass, no dispatch on module names -- so `Provider` reads as a trait
and `FileProvider` as its fixture backend.

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

A ticket file's name has exactly one dot (`KO-12.md`), which is what keeps
`KO-12.comments.md` from reading as a ticket called `KO-12.comments`. The
directory's name is the board's `team`. `claim_next()` offers the lowest
identifier -- plain string order, the order `linear_provider.claim_next()`
sorts in -- whose state is `Todo` and not in `skip`. The file board has no
blocking relations: a ticket that must wait is one a human leaves out of
`Todo`. `budget_min` is the body's `Estimate: N min` line, or 20 without one;
a file has no other estimate field.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import ticket_template

# The same fence `linear_provider.parse_task()` reads, so a body parsed by
# either board yields the same `verify`.
VERIFY_RE = re.compile(r"## Verify command\(s\)\s*```\n(.*?)```", re.S)
DEFAULT_BUDGET_MIN = 20
DEFAULT_STATE = "Todo"


class Provider(Protocol):
    """What the loop asks of a board. Task dicts: see the module docstring."""

    @property
    def team(self) -> str:
        """The board's identifier, recorded by `store.ensure_project()`."""
        ...

    def claim_next(self, skip=()) -> dict | None:
        """The first ready task whose `id` is not in `skip`; None when none."""
        ...

    def fetch_task(self, issue_id) -> dict | None:
        """The ticket as the board holds it now; None when it has no such issue."""
        ...

    def set_state(self, issue_id, state_name) -> None:
        """Move the ticket to the workflow state called `state_name`; raise
        when the board says the move did not happen."""
        ...

    def comment(self, task_id, body) -> None:
        """Record `body` as a comment on the ticket named by either id."""
        ...


class LinearProvider:
    """Linear, through the functions `linear_provider.py` already has.

    The module is imported at the first call that needs it rather than here.
    Importing `linear_provider` is itself a configuration read -- it raises
    without `HOLO2_PROJECT_ID` -- and `--report`, a read-only `--sweep` and an
    acting sweep that trips nothing all run without a board, as they always
    have: the loop's own local imports sat exactly where these calls now are.
    Construction does no I/O of any kind; the API key is read by the module
    on the first request, as today.
    """

    def __init__(self):
        self._module = None

    def _linear(self):
        if self._module is None:
            import linear_provider
            self._module = linear_provider
        return self._module

    @property
    def team(self):
        return self._linear().TEAM

    def claim_next(self, skip=()):
        return self._linear().claim_next(skip=skip)

    def fetch_task(self, issue_id):
        return self._linear().fetch_task(issue_id)

    def set_state(self, issue_id, state_name):
        self._linear().set_state(issue_id, state_name)

    def comment(self, task_id, body):
        self._linear().comment(task_id, body)


class FileProvider:
    """A directory of ticket files as the board; format in the module docstring."""

    def __init__(self, root):
        self.root = Path(root)
        self.team = self.root.name

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

    def claim_next(self, skip=()):
        for identifier in self._identifiers():
            if identifier in skip or self._state(identifier) != DEFAULT_STATE:
                continue
            return self.fetch_task(identifier)
        return None

    def fetch_task(self, issue_id):
        if "." in issue_id or not self._path(issue_id).is_file():
            return None
        return _parse(issue_id, self._path(issue_id).read_text())

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


def _parse(identifier, text):
    """A ticket file as the task dict, key for key as `parse_task()` builds it.

    Mirrors `linear_provider.parse_task()` rather than calling it because that
    module cannot be imported without a configured Linear project, and the
    file board exists for the runs that have none. The conformance suite
    seeds both boards with one body and holds the two parses to each other.
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

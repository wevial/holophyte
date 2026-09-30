from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import ticket_template
from holophyte.config.config_tables import board_config, board_mode

# The same fence `linear_provider.parse_task()` reads.
VERIFY_RE = re.compile(r"## Verify command\(s\)\s*```[^\n]*\n(.*?)```", re.S)
DEFAULT_BUDGET_MIN = 20
DEFAULT_STATE = "Todo"
# `closed_identifiers()` answers in Linear state types on both boards.
CLOSED_STATE_NAMES = {"Done": "completed", "Canceled": "canceled",
                      "Cancelled": "canceled", "Duplicate": "canceled"}
GONE = "gone"


class Board(Protocol):
    @property
    def team(self) -> str:
        ...

    def claim_next(self, skip=(), order="identifier") -> dict | None:
        """The listing the ask chose from lands on `self.last_listing`."""
        ...

    def ready_issues(self) -> list[dict]:
        ...

    def listing(self) -> list[dict]:
        ...

    def fetch_task(self, issue_id) -> dict | None:
        ...

    def set_state(self, issue_id, state_name) -> None:
        ...

    def comment(self, task_id, body) -> None:
        ...

    def closed_identifiers(self, identifiers) -> dict[str, str]:
        ...

    def states(self, identifiers) -> dict[str, dict]:
        """Gone only on a complete answer; raise when the board could not be asked."""
        ...

    def label_issue(self, issue_id, name) -> None:
        """Raise when the board did not take it: the loop gives the lease back then."""
        ...

    def issue_labels(self, issue_id) -> list[str]:
        ...

    def unlabel_issue(self, issue_id, name) -> None:
        ...

    def file(self, title, body, estimate, state, priority=None,
             blockers=(), parent=None) -> str:
        ...

    def update(self, identifier, title, body, estimate, blockers=(), *,
               revision=None, priority=None,
               labels=None) -> tuple[list[str], list[str]]:
        """A board with no native write raises for a native keyword that is not None."""
        ...

    def stored_body(self, identifier) -> str:
        ...


class LinearBoard:
    """The module is imported at first use, so read-only modes never touch it."""
    def __init__(self, project_id, team, label=None, *, store_mode=False):
        self.project_id = project_id
        self._team = team
        self._label = label
        self.store_mode = store_mode
        self._module = None
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

    def file(self, title, body, estimate, state, priority=None, blockers=(),
             parent=None):
        linear = self._linear()
        issue = linear.create_issue(self.project_id, self._team, title, body,
                                    estimate, state, priority=priority,
                                    parent=parent)
        try:
            for blocker in blockers:
                linear.add_blocker(issue["id"], blocker)
        except Exception as refused:
            raise FiledWithoutBlockers(issue["identifier"], refused) from refused
        return issue["identifier"]

    def update(self, identifier, title, body, estimate, blockers=(), *,
               revision=None, priority=None, labels=None):
        _refuse_native_update(identifier, revision, priority, labels)
        linear = self._linear()
        issue_id = linear.update_issue(identifier, title, body, estimate)
        # Read after the body is stored: a refused update adds nothing.
        held = linear.blockers_of(identifier)
        added = [b for b in blockers if b not in held]
        for blocker in added:
            linear.add_blocker(issue_id, blocker)
        return added, [b for b in held if b not in blockers]

    def stored_body(self, identifier):
        return self._linear().fetch_description(identifier)


def _refuse_native_update(identifier, revision, priority, labels):
    given = [name for name, value in (("revision", revision),
                                      ("priority", priority),
                                      ("labels", labels)) if value is not None]
    if given:
        raise RuntimeError(f"refused to update {identifier} with "
                           f"{', '.join(given)}: only a native board takes them")


class FiledWithoutBlockers(RuntimeError):
    def __init__(self, identifier, refused):
        super().__init__(f"filed {identifier} without all its blockers: "
                         f"{refused}")
        self.identifier = identifier


def refuse_parent(title, parent):
    if parent is not None:
        raise RuntimeError(f"refused to file {title!r} under {parent}: only "
                           "a Linear board has sub-issues")


def board_for(target):
    """NativeBoard is imported here: this module is imported where no store exists."""
    mode = board_mode(target)
    settings = board_config(target)
    if settings is None:
        return None
    if mode.kind == "native":
        from holophyte.board.native_board import NativeBoard
        return NativeBoard(target, settings.prefix, settings.team)
    return LinearBoard(settings.project_id, settings.team, settings.label,
                       store_mode=mode.mode == "store")


class FileProvider:
    """A directory of ticket files; a ticket file's name has exactly one dot."""
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
        # A ticket file has no priority, so `order = "priority"` orders the same way.
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
        return {identifier: CLOSED_STATE_NAMES[self._state(identifier)]
                for identifier in identifiers
                if "." not in identifier and self._path(identifier).is_file()
                and self._state(identifier) in CLOSED_STATE_NAMES}

    def states(self, identifiers):
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

    def file(self, title, body, estimate, state, priority=None, blockers=(),
             parent=None):
        self._refuse_blockers(f"file {title!r}", blockers)
        refuse_parent(title, parent)
        prefix = f"{self.team}-"
        numbers = [int(i[len(prefix):]) for i in self._identifiers()
                   if i.startswith(prefix) and i[len(prefix):].isdigit()]
        identifier = f"{prefix}{max(numbers, default=0) + 1}"
        self._write(identifier, title, body, estimate)
        if state != DEFAULT_STATE:
            self._path(identifier, ".state").write_text(f"{state}\n")
        return identifier

    def update(self, identifier, title, body, estimate, blockers=(), *,
               revision=None, priority=None, labels=None):
        _refuse_native_update(identifier, revision, priority, labels)
        self._require(identifier)
        self._refuse_blockers(f"update {identifier}", blockers)
        self._write(identifier, title, body, estimate)
        return [], []

    def _write(self, identifier, title, body, estimate):
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
    state = CLOSED_STATE_NAMES.get(name, "open")
    return state, {"completed": None, "canceled": "canceled"}.get(
        state, "backlog" if name == "Backlog" else "ready")


def parse_body(identifier, text):
    """Mirrored: `linear_provider` cannot be imported without a configured project."""
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

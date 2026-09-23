"""Harness adapters: an `[agents.ROLE]` table built into argv in the codebase.

A role written as a table -- `[agents.implementer] harness = "claude"`, with
optional `model` and `effort` -- names an adapter here instead of carrying a
command string. The adapter owns what a wrapper script on the writer host
used to: the argv of a turn, the session id that turn runs under (chosen
before launch, so the factory records it at dispatch rather than reading it
back out of the output) and the argv that resumes it. The binary is the
harness's own name, looked up on PATH at launch, unless the top-level
`[harnesses]` table names an absolute path for it.

Validation reads each adapter's `roles` and never names a harness or a role
itself, so letting a harness serve another role is an addition to its set
plus that role's argv. Nothing here reads a target: `holophyte.config` does,
and hands the table in, which keeps this module importable from there.
"""
import os
import uuid
from dataclasses import dataclass

# The `[agents]` roles that may be written as a table at all, and the keys
# such a table holds. Every other `[agents]` command stays a string.
TABLE_ROLES = ("implementer", "reviewer", "adjudicator")
TABLE_KEYS = ("harness", "model", "effort")


class Claude:
    """Claude Code in print mode, under a session id the factory assigns.

    `effort` is passed through as written: the CLI owns its list of levels.
    """
    name = "claude"
    roles = frozenset({"implementer"})

    def turn(self, binary, options):
        return [binary, "-p", "--session-id", str(uuid.uuid4()),
                *self.route(options)]

    def session(self, argv):
        return argv[argv.index("--session-id") + 1]

    def resume(self, binary, options, session):
        return [binary, "-p", "--resume", session, *self.route(options)]

    @staticmethod
    def route(options):
        # The implementer defaults live with the other role pins in
        # `holophyte.config`, which imports this module.
        from holophyte.config import IMPL_EFFORT, IMPL_MODEL
        return ["--model", options.get("model", IMPL_MODEL),
                "--effort", options.get("effort", IMPL_EFFORT)]


ADAPTERS = {adapter.name: adapter for adapter in (Claude(),)}


@dataclass(frozen=True)
class Seat:
    """One table-form role, resolved: its adapter, binary and options."""
    adapter: object
    binary: str
    options: dict

    @property
    def name(self):
        return self.adapter.name

    def turn(self, goal):
        """A fresh turn's argv, the goal last, under a new session id."""
        return self.adapter.turn(self.binary, self.options) + [goal]

    def session(self, argv):
        """The session id `argv`, built by `turn()`, runs under."""
        return self.adapter.session(argv)

    def resume(self, session):
        """The argv that resumes `session`; the caller appends the prompt."""
        return self.adapter.resume(self.binary, self.options, session)


def parse_role(where, key, table):
    """The adapter `[agents.KEY]` names, or a startup error naming the key.

    `where` prefixes every refusal (the config path). Refused: a table for a
    key outside `TABLE_ROLES`, a key outside `TABLE_KEYS`, a harness with no
    adapter, a harness whose `roles` do not hold `key` -- the message names
    the roles it does serve -- and a `model` or `effort` that is not a
    non-empty string.
    """
    if key not in TABLE_ROLES:
        raise SystemExit(
            f"{where}: [agents.{key}]: only {', '.join(TABLE_ROLES)} may be a "
            f"table; write [agents] {key} as a command string")
    for option in table:
        if option not in TABLE_KEYS:
            raise SystemExit(
                f"{where}: [agents.{key}] {option}: unknown key; "
                f"[agents.{key}] accepts: {', '.join(TABLE_KEYS)}")
    name = table.get("harness")
    adapter = ADAPTERS.get(name) if isinstance(name, str) else None
    if adapter is None:
        raise SystemExit(
            f"{where}: [agents.{key}] harness must be one of "
            f"{', '.join(sorted(ADAPTERS))}, got {name!r}")
    if key not in adapter.roles:
        raise SystemExit(
            f"{where}: [agents.{key}] harness: {name!r} supports "
            f"{', '.join(sorted(adapter.roles))}, not {key}")
    for option in ("model", "effort"):
        value = table.get(option)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise SystemExit(
                f"{where}: [agents.{key}] {option} must be a non-empty string, "
                f"got {value!r}")
    return adapter


def check_paths(where, paths):
    """Refuse a `[harnesses]` entry that is not an absolute path: rounds run
    in a task worktree, where a relative one resolves nowhere a check can
    look. Names outside `ADAPTERS` are `check_config_keys()`'s refusal."""
    for name, path in paths.items():
        if not isinstance(path, str) or not os.path.isabs(path):
            raise SystemExit(
                f"{where}: [harnesses] {name} must be an absolute path to the "
                f"{name} binary, got {path!r}")


def route_text(value):
    """What names a configured `[agents]` role in a record: the command
    string as written, or a table's harness."""
    return value.get("harness") if isinstance(value, dict) else value

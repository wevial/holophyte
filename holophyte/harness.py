"""Harness adapters: an `[agents.ROLE]` table built into argv in the codebase.

A role written as a table -- `[agents.implementer] harness = "claude"`, with
optional `model` and `effort` -- names an adapter here instead of carrying a
command string. The adapter owns what a wrapper script on the writer host
used to: the argv of a turn, the session id that turn runs under (chosen
before launch, so the factory records it at dispatch rather than reading it
back out of the output -- or, for a review harness that chooses its own,
read from the banner it prints or the session list it keeps) and the argv
that resumes it. The binary is the harness's own name, looked up on PATH at
launch, unless the top-level `[harnesses]` table names an absolute path for
it.

Validation reads each adapter's `roles`, `required` and `refused` options
and never names a harness or a role itself, so letting a harness serve
another role is an addition to its set plus that role's argv.
`holophyte.config` imports this module at load, so the target readers below
(`check_target()`, `seat()`, `agent_session()`) import its tables inside the
call.
"""
import json
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass

import review_runner

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
    required = refused = ()
    efforts = None

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


class Codex:
    """`codex exec` for the review roles, with Codex's own sandbox bypassed.

    The read-only sandbox cannot start under a systemd user unit with
    PrivateTmp, so the factory runs the turn in a throwaway detached
    checkout of the candidate (`agents.table_review()`) and that checkout is
    the write boundary. `resume` takes no `-C`, which is why the caller sets
    the process cwd rather than the argv naming it. Codex chooses the
    session id itself and prints it in its `session id:` banner, which
    `reported_session()` reads back out of the output.
    """
    name = "codex"
    roles = frozenset({"reviewer", "adjudicator"})
    required = refused = ()
    # `REVIEW_EFFORTS`, read at its source: `holophyte.config` imports this
    # module at load.
    efforts = review_runner.EFFORTS
    BANNER = re.compile(r"^[ \t]*session id:[ \t]*(\S+)", re.MULTILINE)

    def turn(self, binary, options):
        return [binary, "exec", *self.route(options)]

    def resume(self, binary, options, session):
        return [binary, "exec", "resume", *self.route(options), session]

    def reported_session(self, binary, output, cwd, env):
        match = self.BANNER.search(output)
        return match.group(1) if match else None

    @staticmethod
    def route(options):
        from holophyte.config import REVIEW_EFFORT, REVIEW_MODEL
        effort = options.get("effort", REVIEW_EFFORT)
        return ["-m", options.get("model", REVIEW_MODEL),
                "-c", f"model_reasoning_effort={effort}",
                "--dangerously-bypass-approvals-and-sandbox"]


class Devin:
    """Devin in print mode for the review roles, in the same throwaway
    candidate checkout as `Codex`.

    Print mode fails in a directory Devin has never trusted, and every
    throwaway checkout is one, hence `--respect-workspace-trust false`;
    `dangerous` lets the reviewer run git and tests without a prompt, the
    checkout staying the write boundary. Devin chooses the session id and
    prints none, so `reported_session()` asks `devin list` in the checkout,
    which holds only this turn's session: a resumed one moves to the
    directory it was resumed in. The factory has no Devin model to default
    to, and the CLI has no effort flag.
    """
    name = "devin"
    roles = frozenset({"reviewer", "adjudicator"})
    required = ("model",)
    refused = ("effort",)
    efforts = None
    LIST_TIMEOUT = 60

    def turn(self, binary, options):
        return [binary, *self.route(options), "-p"]

    def resume(self, binary, options, session):
        return [binary, *self.route(options), "-r", session, "-p"]

    def reported_session(self, binary, output, cwd, env):
        """The one session `devin list` shows for `cwd`, None for any
        other answer: a guess could resume someone else's conversation."""
        try:
            listed = subprocess.run(
                [binary, "list", "--format", "json"], cwd=cwd, env=env,
                capture_output=True, text=True, timeout=self.LIST_TIMEOUT,
                stdin=subprocess.DEVNULL)
            sessions = json.loads(listed.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if listed.returncode != 0 or not isinstance(sessions, list) \
                or len(sessions) != 1 or not isinstance(sessions[0], dict):
            return None
        session = sessions[0].get("id")
        return session if isinstance(session, str) else None

    @staticmethod
    def route(options):
        return ["--model", options["model"], "--permission-mode", "dangerous",
                "--respect-workspace-trust", "false"]


ADAPTERS = {adapter.name: adapter for adapter in (Claude(), Codex(), Devin())}


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

    def reported_session(self, output, cwd, env):
        """The session id a finished turn in `cwd` ran under, for an adapter
        whose harness chooses it; None when the harness names none."""
        return self.adapter.reported_session(self.binary, output, cwd, env)

    def named(self, argv):
        """`argv` as a record names it: the harness first, not a
        `[harnesses]` path -- the outage signatures match on the route's
        name -- and the prompt left off."""
        return shlex.join([self.name, *argv[1:-1]])


def parse_role(where, key, table):
    """The adapter `[agents.KEY]` names, or a startup error naming the key.

    `where` prefixes every refusal (the config path). Refused: a table for a
    key outside `TABLE_ROLES`, a key outside `TABLE_KEYS`, a harness with no
    adapter, a harness whose `roles` do not hold `key` -- the message names
    the roles it does serve -- an option the adapter lists as `refused`
    or leaves out that it lists as `required`, a `model` or `effort` that
    is not a non-empty string, and an `effort` outside the adapter's
    `efforts` when it declares them.
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
    check_options(where, key, adapter, table)
    return adapter


def check_options(where, key, adapter, table):
    """Refuse the `model` and `effort` of `[agents.KEY]` that `adapter`
    cannot take; `parse_role()` names the refusals."""
    name = adapter.name
    for option in adapter.refused:
        if option in table:
            raise SystemExit(
                f"{where}: [agents.{key}] {option}: harness {name!r} takes no "
                f"{option}; drop it")
    for option in adapter.required:
        if option not in table:
            raise SystemExit(
                f"{where}: [agents.{key}] {option}: required for harness "
                f"{name!r}")
    for option in ("model", "effort"):
        value = table.get(option)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise SystemExit(
                f"{where}: [agents.{key}] {option} must be a non-empty string, "
                f"got {value!r}")
    effort = table.get("effort")
    if adapter.efforts and effort is not None and effort not in adapter.efforts:
        raise SystemExit(
            f"{where}: [agents.{key}] effort must be one of "
            f"{', '.join(adapter.efforts)}, got {effort!r}")


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


def check_target(target):
    """Parse every table-form `[agents]` role and the `[harnesses]` paths.

    A table replaces the wrapper script the regex and resume template were
    written against, so `implementer_session` or `implementer_resume` beside
    a table implementer is refused as contradictory: the adapter assigns the
    session and builds the resume, and a second answer to the same question
    would be one the factory ignores.
    """
    from holophyte.config import AGENT_CONFIG_KEYS, config_table
    where = f"[holo2] {target.config_path}"
    check_paths(where, config_table(target, "harnesses"))
    for role in AGENT_CONFIG_KEYS:
        seat(target, role)
        seat(target, role, fallback=True)
    if seat(target, "implement") is None:
        return
    for key in ("implementer_session", "implementer_resume"):
        if key in config_table(target, "agents"):
            raise SystemExit(
                f"{where}: [agents] {key} beside [agents.implementer]: the "
                f"harness adapter records and resumes the session -- drop {key}")


def seat(target, role, *, fallback=False):
    """The `Seat` a table-form `[agents]` role resolves to, None for
    a command string or an absent key.

    Under `implementer_isolation = "container"` an implementer's binary
    is the bare harness name, whatever `[harnesses]` says: the image
    supplies it. Review roles run on the host either way, so they keep the
    `[harnesses]` path.
    """
    from holophyte.config import AGENT_CONFIG_KEYS, config_table
    key = AGENT_CONFIG_KEYS[role] + ("_fallback" if fallback else "")
    table = config_table(target, "agents").get(key)
    if not isinstance(table, dict):
        return None
    where = f"[holo2] {target.config_path}"
    adapter = parse_role(where, key, table)
    from holophyte.isolation import route_for
    binary = adapter.name
    if role != "implement" or route_for(target).backend != "container":
        paths = config_table(target, "harnesses")
        check_paths(where, paths)
        binary = paths.get(adapter.name, binary)
    return Seat(adapter, binary, table)


def agent_session(target, role, argv):
    """The session id a table-form `role`'s turn `argv` runs under, None
    for a command string -- its session, if any, is read from its output
    through `implementer_session` -- and for a container turn, which
    records none."""
    from holophyte.isolation import route_for
    resolved = seat(target, role)
    if resolved is None or route_for(target).backend == "container":
        return None
    return resolved.session(argv)

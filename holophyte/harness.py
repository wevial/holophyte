"""Harness adapters: an `[agents.ROLE]` table built into argv in the codebase.

A role written as a table -- `[agents.implementer] harness = "claude"`, with
optional `model` and `effort` -- names an adapter here instead of carrying a
command string. The adapter owns what a wrapper script on the writer host
used to: the argv of a turn, the session id that turn runs under (chosen
before launch, so the factory records it at dispatch rather than reading it
back out of the output -- or, for a harness that chooses its own, read from
the banner it prints) and the argv that resumes it. The binary is the
adapter's `binary` -- the harness's own name unless its CLI is called
something else -- looked up on PATH at launch, unless the top-level
`[harnesses]` table names an absolute path for it.

Validation reads each adapter's `roles` and never names a harness or a role
itself, so letting a harness serve another role is an addition to its set
plus that role's argv. `holophyte.config` imports this module at load, so
the target readers below (`check_target()`, `seat()`, `agent_session()`)
import its tables inside the call.
"""
import os
import re
import shlex
import uuid
from dataclasses import dataclass

import review_runner

# The `[agents]` roles that may be written as a table at all, and the keys
# such a table holds. Every other `[agents]` command stays a string.
TABLE_ROLES = ("implementer", "reviewer", "adjudicator")
TABLE_KEYS = ("harness", "model", "effort")


class Adapter:
    """What an adapter declares unless it says otherwise: the binary is its
    name, no table key is required or refused beyond `parse_role()`'s
    shape checks, the harness can resume a session, and a turn's output
    names no session."""
    requires = frozenset()
    refuses = frozenset()
    efforts = None
    resumes = True

    @property
    def binary(self):
        return self.name

    def reported_session(self, output, role):
        return None


class Claude(Adapter):
    """Claude Code in print mode, under a session id the factory assigns.

    `effort` is passed through as written: the CLI owns its list of levels.
    """
    name = "claude"
    roles = frozenset({"implementer"})

    def turn(self, binary, options, role):
        return [binary, "-p", "--session-id", str(uuid.uuid4()),
                *self.route(options)]

    def session(self, argv):
        return argv[argv.index("--session-id") + 1]

    def resume(self, binary, options, session, role):
        return [binary, "-p", "--resume", session, *self.route(options)]

    @staticmethod
    def route(options):
        # The implementer defaults live with the other role pins in
        # `holophyte.config`, which imports this module.
        from holophyte.config import IMPL_EFFORT, IMPL_MODEL
        return ["--model", options.get("model", IMPL_MODEL),
                "--effort", options.get("effort", IMPL_EFFORT)]


class Codex(Adapter):
    """`codex exec` with Codex's own sandbox bypassed.

    The read-only sandbox cannot start under a systemd user unit with
    PrivateTmp, so the factory runs a review turn in a throwaway detached
    checkout of the candidate (`agents.table_review()`) and that checkout is
    the write boundary; an implementer turn runs in the task worktree like
    any implementer. `resume` takes no `-C`, which is why the caller sets
    the process cwd rather than the argv naming it. Codex chooses the
    session id itself and prints it in its `session id:` banner, which
    `reported_session()` reads back out of the output -- after the turn, so
    `session()` has none to give at dispatch. The implementer's argv is the
    flag set the maintainer ran it under as a command string, and its resume
    carries the turn's model and effort.
    """
    name = "codex"
    roles = frozenset({"implementer", "reviewer", "adjudicator"})
    # `REVIEW_EFFORTS`, read at its source: `holophyte.config` imports this
    # module at load.
    efforts = review_runner.EFFORTS
    BANNER = re.compile(r"^[ \t]*session id:[ \t]*(\S+)", re.MULTILINE)
    IMPLEMENTER = ["--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check"]

    def turn(self, binary, options, role):
        if role == "implementer":
            return [binary, "exec", *self.IMPLEMENTER, *self.route(options)]
        return [binary, "exec", *self.route(options),
                "--dangerously-bypass-approvals-and-sandbox"]

    def session(self, argv):
        return None

    def resume(self, binary, options, session, role):
        if role == "implementer":
            return [binary, "exec", "resume", session, *self.IMPLEMENTER,
                    *self.route(options)]
        return [binary, "exec", "resume", *self.route(options),
                "--dangerously-bypass-approvals-and-sandbox", session]

    def reported_session(self, output, role):
        """The first banner's id; for the implementer, only a UUID, which
        is what `runs.providerSessionId` holds for a resume to name."""
        match = self.BANNER.search(output)
        if match is None:
            return None
        if role == "implementer":
            try:
                uuid.UUID(match.group(1))
            except ValueError:
                return None
        return match.group(1)

    @staticmethod
    def route(options):
        from holophyte.config import REVIEW_EFFORT, REVIEW_MODEL
        effort = options.get("effort", REVIEW_EFFORT)
        return ["-m", options.get("model", REVIEW_MODEL),
                "-c", f"model_reasoning_effort={effort}"]


class Cursor(Adapter):
    """`cursor-agent` in print mode for the review roles; it cannot resume.

    The flags are the ones `cursor-agent --help` lists for CLI version
    2026.09.10-fd3934a on the writer host:

    - `-p` / `--print`: non-interactive print mode, "for scripts or
      non-interactive use", with access to all tools including shell;
    - `--model <model>`: the model -- required here, since the CLI's own
      default is whatever its account settings say today;
    - `-f` / `--force`: "force allow commands unless explicitly denied", so
      print mode runs `git` without an approval prompt; `--trust` beside
      it "trusts the current workspace without prompting", and every turn
      runs in a fresh throwaway checkout the CLI has never seen;
    - `--resume [chatId]`: resume by id -- unused: the adapter declares no
      resume until a live turn shows how to learn a chat id, so capture
      and resume wait for a follow-up.

    The CLI has no effort flag (a model's effort is a bracket override in
    its name), so `effort` is refused rather than dropped. The throwaway
    candidate checkout (`agents.table_review()`) is the write boundary, as
    for Codex.
    """
    name = "cursor"
    binary = "cursor-agent"
    roles = frozenset({"reviewer", "adjudicator"})
    requires = frozenset({"model"})
    refuses = frozenset({"effort"})
    resumes = False

    def turn(self, binary, options, role):
        return [binary, "-p", "--model", options["model"], "--force", "--trust"]


ADAPTERS = {adapter.name: adapter for adapter in (Claude(), Codex(), Cursor())}


@dataclass(frozen=True)
class Seat:
    """One table-form role, resolved: its adapter, binary and options, and
    the `[agents]` role (`implementer`, `reviewer`, `adjudicator`) it fills,
    which the adapter's argv may differ by."""
    adapter: object
    binary: str
    options: dict
    role: str

    @property
    def name(self):
        return self.adapter.name

    def turn(self, goal):
        """A fresh turn's argv, the goal last, under a new session id."""
        return self.adapter.turn(self.binary, self.options, self.role) + [goal]

    def session(self, argv):
        """The session id `argv`, built by `turn()`, runs under; None for an
        adapter whose harness chooses it (`reported_session()`)."""
        return self.adapter.session(argv)

    def resume(self, session):
        """The argv that resumes `session`; the caller appends the prompt."""
        return self.adapter.resume(self.binary, self.options, session, self.role)

    def reported_session(self, output):
        """The session id a finished turn printed, for an adapter whose
        harness chooses it; None when the output names none."""
        return self.adapter.reported_session(output, self.role)

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
    the roles it does serve -- a `model` or `effort` that is not a
    non-empty string, a key the adapter `requires` that is absent or one
    it `refuses` that is present, and an `effort` outside the adapter's
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
    for option in ("model", "effort"):
        value = table.get(option)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise SystemExit(
                f"{where}: [agents.{key}] {option} must be a non-empty string, "
                f"got {value!r}")
    missing = sorted(adapter.requires - table.keys())
    if missing:
        raise SystemExit(
            f"{where}: [agents.{key}] {missing[0]} is required for harness "
            f"{name!r}")
    refused = sorted(adapter.refuses & table.keys())
    if refused:
        raise SystemExit(
            f"{where}: [agents.{key}] {refused[0]}: harness {name!r} takes no "
            f"{refused[0]} -- drop {refused[0]}")
    effort = table.get("effort")
    if adapter.efforts and effort is not None and effort not in adapter.efforts:
        raise SystemExit(
            f"{where}: [agents.{key}] effort must be one of "
            f"{', '.join(adapter.efforts)}, got {effort!r}")
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


def check_target(target):
    """Parse every table-form `[agents]` role and the `[harnesses]` paths.

    A table replaces the wrapper script the regex and resume template were
    written against, so `implementer_session` or `implementer_resume` beside
    a table implementer is refused as contradictory: the adapter records the
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
    binary = adapter.binary
    if role != "implement" or route_for(target).backend != "container":
        paths = config_table(target, "harnesses")
        check_paths(where, paths)
        binary = paths.get(adapter.name, binary)
    return Seat(adapter, binary, table, AGENT_CONFIG_KEYS[role])


def agent_session(target, role, argv):
    """The session id a table-form `role`'s turn `argv` runs under, None
    for a command string -- its session, if any, is read from its output
    through `implementer_session` -- for an adapter that learns it from the
    output (`agents.record_session()` reads that), and for a container
    turn, which records none."""
    from holophyte.isolation import route_for
    resolved = seat(target, role)
    if resolved is None or route_for(target).backend == "container":
        return None
    return resolved.session(argv)

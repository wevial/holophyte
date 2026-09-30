"""`holophyte.config.reader` imports this module at load; import it inside calls."""
import json
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass

import review_runner

TABLE_ROLES = ("implementer", "reviewer", "adjudicator", "critic")
TABLE_KEYS = ("harness", "model", "effort")


class Adapter:
    requires = frozenset()
    refuses = frozenset()
    efforts = None
    resumes = True

    @property
    def binary(self):
        return self.name

    def reported_session(self, binary, output, role, run):
        return None


class Claude(Adapter):
    name = "claude"
    roles = frozenset({"implementer", "critic"})

    def turn(self, binary, options, role):
        return [binary, "-p", "--session-id", str(uuid.uuid4()),
                *self.route(options)]

    def session(self, argv):
        return argv[argv.index("--session-id") + 1]

    def resume(self, binary, options, session, role):
        return [binary, "-p", "--resume", session, *self.route(options)]

    @staticmethod
    def route(options):
        from holophyte.config.reader import IMPL_EFFORT, IMPL_MODEL
        return ["--model", options.get("model", IMPL_MODEL),
                "--effort", options.get("effort", IMPL_EFFORT)]


class Codex(Adapter):
    """Codex's read-only sandbox cannot start under a PrivateTmp user unit, so
    a review's throwaway checkout is the write boundary; `resume` takes no `-C`."""
    name = "codex"
    roles = frozenset({"implementer", "reviewer", "adjudicator", "critic"})
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

    def reported_session(self, binary, output, role, run):
        """An implementer's id must be a UUID for a resume to name it."""
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
        from holophyte.config.reader import REVIEW_EFFORT, REVIEW_MODEL
        effort = options.get("effort", REVIEW_EFFORT)
        return ["-m", options.get("model", REVIEW_MODEL),
                "-c", f"model_reasoning_effort={effort}"]


class Devin(Adapter):
    name = "devin"
    roles = frozenset({"implementer", "reviewer", "adjudicator"})
    requires = frozenset({"model"})
    refuses = frozenset({"effort"})
    LIST_TIMEOUT = 60

    # Print mode fails in a directory Devin has never trusted.
    IMPLEMENTER = ["--respect-workspace-trust", "false", "--permission-mode",
                   "dangerous"]

    def turn(self, binary, options, role):
        if role == "implementer":
            return [binary, *self.IMPLEMENTER, "--model", options["model"],
                    "-p", "--"]
        return [binary, *self.route(options), "-p"]

    def session(self, argv):
        return None

    def resume(self, binary, options, session, role):
        if role == "implementer":
            return [binary, *self.IMPLEMENTER, "--model", options["model"],
                    "-r", session, "-p", "--"]
        return [binary, *self.route(options), "-r", session, "-p"]

    def reported_session(self, binary, output, role, run):
        if run is None:
            return None
        try:
            code, listed = run([binary, "list", "--format", "json"],
                               self.LIST_TIMEOUT)
            sessions = json.loads(listed)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if code != 0 or not isinstance(sessions, list):
            return None
        if role == "implementer":
            dated = [entry for entry in sessions if isinstance(entry, dict)
                     and isinstance(entry.get("last_activity_at"), int)]
            newest = max((entry["last_activity_at"] for entry in dated),
                         default=None)
            sessions = [entry for entry in dated
                        if entry["last_activity_at"] == newest]
        # A tie for newest is no answer: a guess could resume another conversation.
        if len(sessions) != 1 or not isinstance(sessions[0], dict):
            return None
        session = sessions[0].get("id")
        return session if isinstance(session, str) else None

    @staticmethod
    def route(options):
        return ["--model", options["model"], "--permission-mode", "dangerous",
                "--respect-workspace-trust", "false"]


class Cursor(Adapter):
    name = "cursor"
    binary = "cursor-agent"
    roles = frozenset({"reviewer", "adjudicator"})
    requires = frozenset({"model"})
    refuses = frozenset({"effort"})
    resumes = False

    def turn(self, binary, options, role):
        # --force runs git without a prompt; --trust accepts the fresh checkout.
        return [binary, "-p", "--model", options["model"], "--force", "--trust"]


ADAPTERS = {adapter.name: adapter for adapter in (Claude(), Codex(), Cursor(),
                                                  Devin())}


@dataclass(frozen=True)
class Seat:
    adapter: object
    binary: str
    options: dict
    role: str

    @property
    def name(self):
        return self.adapter.name

    def turn(self, goal):
        return self.adapter.turn(self.binary, self.options, self.role) + [goal]

    def session(self, argv):
        return self.adapter.session(argv)

    def resume(self, session):
        return self.adapter.resume(self.binary, self.options, session, self.role)

    def reported_session(self, output, run=None):
        return self.adapter.reported_session(self.binary, output, self.role,
                                             run)

    def named(self, argv):
        """The harness name, not a path: the outage signatures match on it."""
        return shlex.join([self.name, *argv[1:-1]])


def parse_role(where, key, table):
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
    for name, path in paths.items():
        # A relative path resolves nowhere from a task worktree.
        if not isinstance(path, str) or not os.path.isabs(path):
            raise SystemExit(
                f"{where}: [harnesses] {name} must be an absolute path to the "
                f"{name} binary, got {path!r}")


def route_text(value):
    return value.get("harness") if isinstance(value, dict) else value


def check_target(target):
    from holophyte.config.reader import AGENT_CONFIG_KEYS, config_table
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
    from holophyte.config.reader import AGENT_CONFIG_KEYS, config_table
    key = AGENT_CONFIG_KEYS[role] + ("_fallback" if fallback else "")
    table = config_table(target, "agents").get(key)
    where = f"[holo2] {target.config_path}"
    if key == "critic" and table is not None:
        table = critic_table(where, table)
    if not isinstance(table, dict):
        return None
    adapter = parse_role(where, key, table)
    from holophyte.isolation.launcher import route_for
    binary = adapter.binary
    # A container implementer's binary comes from the image, not `[harnesses]`.
    if role != "implement" or route_for(target).backend != "container":
        paths = config_table(target, "harnesses")
        check_paths(where, paths)
        binary = paths.get(adapter.name, binary)
    return Seat(adapter, binary, table, AGENT_CONFIG_KEYS[role])


def critic_table(where, table):
    if not isinstance(table, dict):
        raise SystemExit(
            f"{where}: [agents] critic: the critic has no command-string form; "
            f"write it as the [agents.critic] table")
    if table.get("harness", "codex") != "codex":
        return table
    # Codex.route()'s own defaults are the reviewer's, not the critic's.
    from holophyte.config.reader import CRITIC_EFFORT, CRITIC_MODEL
    return {"harness": "codex", "model": CRITIC_MODEL,
            "effort": CRITIC_EFFORT, **table}


def critic_seat(target):
    return seat(target, "critic")


def agent_session(target, role, argv):
    resolved = seat(target, role)
    if resolved is None:
        return None
    return resolved.session(argv)

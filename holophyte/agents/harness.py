"""`holophyte.config.reader` imports this module at load; import it inside calls."""
import json
import os
import re
import shlex
import subprocess
import uuid
from dataclasses import dataclass

import review_runner
from holophyte.tickets.ticket_template import ORCHESTRATION_MODES

TABLE_ROLES = ("implementer", "reviewer", "adjudicator", "critic", "trimmer",
               "adversary")
WRITING_ROLES = ("implementer", "trimmer")
SCOUT_LEAD = "scout-lead"
SCOUT_HELPER = "scout-helper"
SCOUT_RUNNER = "scout-runner"
READ_ONLY_ROLES = (SCOUT_LEAD, SCOUT_HELPER)
TABLE_KEYS = ("harness", "model", "effort")
IMPLEMENTER_KEYS = TABLE_KEYS + ("orchestration",)
SHADOW_KEY = "implementer_shadow"
PROFILES_KEY = "implementer_profiles"
PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]+")

SUBAGENTS_BRIEF = (
    "\n\nYou may orchestrate subagents for this ticket. Independent pieces "
    "of it (for example one per module) may go to subagents you start with "
    "your own tools. The subagents edit files in this worktree but do not "
    "commit, branch, push or open worktrees; you integrate their work and "
    "commit it on this one branch. Before handing in, have an independent "
    "subagent review the whole change against the acceptance criteria.")
WORKFLOW_BRIEF = SUBAGENTS_BRIEF + (
    "\n\nThe maintainer has opted this ticket in to a Claude Code "
    "multi-agent workflow: you may run one with the Workflow tool, under "
    "the same rules. If the Workflow tool is unavailable, orchestrate "
    "subagents instead.")
ORCHESTRATION_BRIEFS = {"off": "", "subagents": SUBAGENTS_BRIEF,
                        "workflow": WORKFLOW_BRIEF}


class Adapter:
    requires = frozenset()
    refuses = frozenset()
    efforts = None
    resumes = True
    orchestrations = frozenset()

    @property
    def binary(self):
        return self.name

    def reported_session(self, binary, output, role, run):
        return None

    def mcp_flags(self, server):
        raise ValueError(f"harness {self.name!r} takes no MCP server")


class Claude(Adapter):
    name = "claude"
    roles = frozenset({"implementer", "trimmer", "critic", SCOUT_LEAD,
                       SCOUT_HELPER})
    orchestrations = frozenset({"subagents", "workflow"})

    def turn(self, binary, options, role):
        return [binary, "-p", "--session-id", str(uuid.uuid4()),
                *self.subagents(role), *self.route(options),
                *self.output_format(role)]

    def session(self, argv):
        return argv[argv.index("--session-id") + 1]

    def resume(self, binary, options, session, role):
        return [binary, "-p", "--resume", session, *self.subagents(role),
                *self.route(options), *self.output_format(role)]

    @staticmethod
    def subagents(role):
        tools = ["Agent", "Task"] if role == SCOUT_LEAD else []
        if role in READ_ONLY_ROLES:
            tools += ["Bash", "Edit", "Write", "NotebookEdit"]
        return ["--disallowedTools", *tools] if tools else []

    @staticmethod
    def mcp_flags(server):
        entry = {"type": "stdio", "command": server["command"],
                 "args": server["args"], "env": server["env"]}
        return ["--mcp-config",
                json.dumps({"mcpServers": {server["name"]: entry}}),
                "--allowedTools", f"mcp__{server['name']}"]

    @staticmethod
    def output_format(role):
        return (["--output-format", "stream-json", "--verbose"]
                if role in WRITING_ROLES else [])

    @staticmethod
    def route(options):
        from holophyte.config.reader import IMPL_EFFORT, IMPL_MODEL
        return ["--model", options.get("model", IMPL_MODEL),
                "--effort", options.get("effort", IMPL_EFFORT)]


class Codex(Adapter):
    """The sandbox fails under PrivateTmp; a throwaway checkout bounds writes."""
    name = "codex"
    roles = frozenset({"implementer", "trimmer", "reviewer", "adjudicator",
                       "critic", "adversary", SCOUT_LEAD, SCOUT_HELPER})
    efforts = review_runner.EFFORTS
    orchestrations = frozenset({"subagents"})
    BANNER = re.compile(r"^[ \t]*session id:[ \t]*(\S+)", re.MULTILINE)
    IMPLEMENTER = ["--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check"]

    def turn(self, binary, options, role):
        if role in WRITING_ROLES:
            return [binary, "exec", *self.IMPLEMENTER, *self.route(options)]
        return [binary, "exec", *self.route(options), *self.subagents(role),
                *self.sandbox(role)]

    def session(self, argv):
        return None

    def resume(self, binary, options, session, role):
        if role in WRITING_ROLES:
            return [binary, "exec", "resume", session, *self.IMPLEMENTER,
                    *self.route(options)]
        tail = [*self.route(options), *self.subagents(role)]
        if role in READ_ONLY_ROLES:
            return [binary, "exec", *self.sandbox(role), "resume", *tail,
                    session]
        return [binary, "exec", "resume", *tail, *self.sandbox(role), session]

    @staticmethod
    def subagents(role):
        return ["--disable", "multi_agent"] if role == SCOUT_LEAD else []

    @staticmethod
    def mcp_flags(server):
        key = f"mcp_servers.{server['name']}"
        env = ", ".join(f"{name} = {json.dumps(value)}"
                        for name, value in server["env"].items())
        return ["-c", f"{key}.command={json.dumps(server['command'])}",
                "-c", f"{key}.args={json.dumps(server['args'])}",
                "-c", f"{key}.env={{{env}}}",
                "-c", f"{key}.env_vars={json.dumps(server['forward'])}"]

    @staticmethod
    def sandbox(role):
        if role in READ_ONLY_ROLES:
            return ["--sandbox", "read-only"]
        return ["--dangerously-bypass-approvals-and-sandbox"]

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
    roles = frozenset({"implementer", "trimmer", "reviewer", "adjudicator",
                       "adversary"})
    requires = frozenset({"model"})
    refuses = frozenset({"effort"})
    orchestrations = frozenset({"subagents"})
    LIST_TIMEOUT = 60

    # Print mode fails in a directory Devin has never trusted.
    IMPLEMENTER = ["--respect-workspace-trust", "false", "--permission-mode",
                   "dangerous"]

    def turn(self, binary, options, role):
        if role in WRITING_ROLES:
            return [binary, *self.IMPLEMENTER, "--model", options["model"],
                    "-p", "--"]
        return [binary, *self.route(options), "-p"]

    def session(self, argv):
        return None

    def resume(self, binary, options, session, role):
        if role in WRITING_ROLES:
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
    roles = frozenset({"reviewer", "adjudicator", "adversary"})
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

    def turn(self, goal, server=None):
        return self.served(self.adapter.turn(self.binary, self.options,
                                             self.role), server) + [goal]

    def session(self, argv):
        return self.adapter.session(argv)

    def resume(self, session, server=None):
        return self.served(self.adapter.resume(self.binary, self.options,
                                               session, self.role), server)

    def served(self, argv, server):
        if server is None:
            return argv
        return argv[:2] + self.adapter.mcp_flags(server) + argv[2:]

    def reported_session(self, output, run=None):
        return self.adapter.reported_session(self.binary, output, self.role,
                                             run)

    def named(self, argv):
        """The harness name, not a path: the outage signatures match on it."""
        return shlex.join([self.name, *argv[1:-1]])


def parse_role(where, key, table):
    profile = key.startswith(PROFILES_KEY + ".")
    role = "implementer" if key == SHADOW_KEY or profile else key
    if role not in TABLE_ROLES:
        raise SystemExit(
            f"{where}: [agents.{key}]: only {', '.join(TABLE_ROLES)} may be a "
            f"table; write [agents] {key} as a command string")
    accepted = IMPLEMENTER_KEYS if key == "implementer" or profile else TABLE_KEYS
    adapter = route_adapter(where, f"[agents.{key}]", table, role, accepted,
                            shown=key, listed=TABLE_ROLES)
    check_orchestration(where, table, adapter, key)
    return adapter


def route_adapter(where, label, table, role, accepted, *, shown=None,
                  listed=None):
    for option in table:
        if option not in accepted:
            raise SystemExit(
                f"{where}: {label} {option}: unknown key; "
                f"{label} accepts: {', '.join(accepted)}")
    name = table.get("harness")
    adapter = ADAPTERS.get(name) if isinstance(name, str) else None
    if adapter is None:
        raise SystemExit(
            f"{where}: {label} harness must be one of "
            f"{', '.join(sorted(ADAPTERS))}, got {name!r}")
    if role not in adapter.roles:
        supported = sorted(each for each in adapter.roles
                           if listed is None or each in listed)
        raise SystemExit(
            f"{where}: {label} harness: {name!r} supports "
            f"{', '.join(supported)}, not {shown or role}")
    for option in ("model", "effort"):
        value = table.get(option)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise SystemExit(
                f"{where}: {label} {option} must be a non-empty string, "
                f"got {value!r}")
    missing = sorted(adapter.requires - table.keys())
    if missing:
        raise SystemExit(
            f"{where}: {label} {missing[0]} is required for harness "
            f"{name!r}")
    refused = sorted(adapter.refuses & table.keys())
    if refused:
        raise SystemExit(
            f"{where}: {label} {refused[0]}: harness {name!r} takes no "
            f"{refused[0]} -- drop {refused[0]}")
    effort = table.get("effort")
    if adapter.efforts and effort is not None and effort not in adapter.efforts:
        raise SystemExit(
            f"{where}: {label} effort must be one of "
            f"{', '.join(adapter.efforts)}, got {effort!r}")
    return adapter


def check_orchestration(where, table, adapter, key):
    mode = table.get("orchestration", "off")
    if mode not in ORCHESTRATION_MODES:
        raise SystemExit(
            f"{where}: [agents.{key}] orchestration must be one of "
            f"{', '.join(ORCHESTRATION_MODES)}, got {mode!r}")
    if mode != "off" and mode not in adapter.orchestrations:
        raise SystemExit(
            f"{where}: [agents.{key}] orchestration: harness "
            f"{adapter.name!r} supports "
            f"{', '.join(['off', *sorted(adapter.orchestrations)])}, "
            f"not {mode!r}")


def claude_command(command):
    try:
        words = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return False
    return bool(words) and os.path.basename(words[0]) == "claude"


def check_fallback_orchestration(where, agents, key="implementer"):
    table = agents.get("implementer")
    fallback = agents.get("implementer_fallback")
    if (isinstance(table, dict) and table.get("orchestration") == "workflow"
            and fallback is not None and not claude_command(fallback)):
        raise SystemExit(
            f"{where}: [agents.{key}] orchestration = \"workflow\" "
            f"beside a non-Claude [agents] implementer_fallback: an outage "
            f"switch reuses the brief -- use \"subagents\" or a Claude "
            f"implementer_fallback")


def implementer_orchestrations(target, command):
    if command is not None:
        return frozenset({"subagents"})
    from holophyte.config.reader import config_table
    agents = config_table(target, "agents")
    active = seat(target, "implement")
    modes = (active.adapter.orchestrations if active is not None
             else frozenset({"subagents"}) if agents.get("implementer")
             else Claude.orchestrations)
    fallback = agents.get("implementer_fallback")
    if fallback is not None and not claude_command(fallback):
        return modes - {"workflow"}
    return modes


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
    shadow_seat(target)
    implementer_profiles(target)
    check_fallback_orchestration(where, config_table(target, "agents"))
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
    return table_seat(target, where, key, table, AGENT_CONFIG_KEYS[role],
                      launched=role in ("implement", "trim"))


def shadow_seat(target):
    from holophyte.config.reader import config_table
    table = config_table(target, "agents").get(SHADOW_KEY)
    if table is None:
        return None
    where = f"[holo2] {target.config_path}"
    if not isinstance(table, dict):
        raise SystemExit(
            f"{where}: [agents] {SHADOW_KEY}: write it as the "
            f"[agents.{SHADOW_KEY}] table")
    return table_seat(target, where, SHADOW_KEY, table, "implementer",
                      launched=True)


def implementer_profiles(target):
    from holophyte.config.reader import config_table
    agents = config_table(target, "agents")
    profiles = agents.get(PROFILES_KEY)
    if profiles is None:
        return {}
    where = f"[holo2] {target.config_path}"
    if not isinstance(profiles, dict):
        raise SystemExit(
            f"{where}: [agents] {PROFILES_KEY}: write each profile as an "
            f"[agents.{PROFILES_KEY}.NAME] table")
    seats = {}
    for name, table in profiles.items():
        key = f"{PROFILES_KEY}.{name}"
        if not PROFILE_NAME.fullmatch(name):
            raise SystemExit(
                f"{where}: [agents.{key}]: a profile name takes only letters, "
                f"digits, _ and -")
        if not isinstance(table, dict):
            raise SystemExit(
                f"{where}: [agents.{key}]: write it as a table with harness, "
                f"model, effort and orchestration, not a command string")
        seats[name] = table_seat(target, where, key, table, "implementer",
                                 launched=True)
        check_fallback_orchestration(
            where, dict(agents, implementer=table), key)
    return seats


def table_seat(target, where, key, table, role, *, launched):
    adapter = parse_role(where, key, table)
    from holophyte.isolation.launcher import route_for
    binary = adapter.binary
    # A container implementer's binary comes from the image, not `[harnesses]`.
    if not launched or route_for(target).backend != "container":
        binary = harness_binary(target, where, adapter)
    return Seat(adapter, binary, table, role)


def harness_binary(target, where, adapter):
    from holophyte.config.reader import config_table
    paths = config_table(target, "harnesses")
    check_paths(where, paths)
    return paths.get(adapter.name, adapter.binary)


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

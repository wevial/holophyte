import fcntl
import json
import re
import shlex
import tempfile
from collections.abc import MutableMapping
from pathlib import Path

from holophyte.agents.harness import route_text
from holophyte.config.reader import AGENT_CONFIG_KEYS, DEFAULT_IMPLEMENTER
from holophyte.redact import REDACTED, known_secrets, redact_prose


def command_secrets(project):
    secrets = set(known_secrets(project.config()))
    table = project.config().get('agents') or {}
    for seat in AGENT_CONFIG_KEYS.values():
        for key in (seat, seat + '_fallback'):
            configured = table.get(key, '')
            for command in (configured if isinstance(configured, list)
                            else [configured]):
                if not isinstance(command, str):
                    continue  # A harness table carries no free-form arguments.
                for arg in shlex.split(command)[1:]:
                    # An assignment's value may itself start with a dash.
                    if arg.startswith('-') and '=' not in arg:
                        continue
                    value = arg.split('=', 1)[-1]
                    if value:
                        secrets.add(value)
    return secrets


def safe_command(project, command):
    """An argument redacts only a whole name: `high` keeps `codex-astra-high`."""
    command = route_text(command)
    if not command:
        return command
    name = shlex.split(command)[0]
    if name in command_secrets(project):
        return REDACTED
    return redact_prose(name, known_secrets(project.config()))


def route_prose(project, text):
    """Argument values match whole words only: `exec` must not eat `execution`."""
    secrets = known_secrets(project.config())
    text = redact_prose(text, secrets)
    arguments = command_secrets(project) - secrets
    if arguments:
        pattern = r'(?<!\w)(?:' + '|'.join(
            re.escape(value) for value in sorted(arguments, key=len, reverse=True)
        ) + r')(?!\w)'
        text = re.sub(pattern, lambda _: REDACTED, text)
    return text


class ActiveRoutes:
    def __init__(self, project):
        self.commands = {}
        self.pending = {}
        self.project_id = None
        self.failed = False
        self.writer_failed = False
        self.trimmer_failed = False
        self.critic_failed = False
        self.stream = None
        self.project = project
        self.profiles = {}

    def publish(self):
        if self.stream is None:
            self.project.holo_dir.mkdir(parents=True, exist_ok=True)
            self.stream = tempfile.NamedTemporaryFile(
                mode='w+', prefix='active-routes-', suffix='.json',
                dir=self.project.holo_dir)
            fcntl.flock(self.stream, fcntl.LOCK_EX)
        self.stream.seek(0)
        self.stream.truncate()
        commands = dict(self.commands)
        if self.writer_failed:
            commands['write'] = (commands.get('implement')
                                 or route_text((self.project.config().get('agents')
                                                or {}).get('implementer'))
                                 or DEFAULT_IMPLEMENTER)
        snapshot = {AGENT_CONFIG_KEYS[role]: safe_command(self.project, command)
                    for role, command in commands.items()}
        if self.trimmer_failed:
            snapshot['trimmer'] = None
        json.dump(snapshot, self.stream)
        self.stream.flush()

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self.commands.clear()
        self.pending.clear()
        self.profiles.clear()


class OwnImplementer(MutableMapping):
    def __init__(self, shared, own):
        self.shared, self.own = shared, own

    def table(self, role):
        return self.own if role == 'implement' else self.shared

    def __getitem__(self, role):
        return self.table(role)[role]

    def __setitem__(self, role, value):
        self.table(role)[role] = value

    def __delitem__(self, role):
        del self.table(role)[role]

    def __iter__(self):
        yield from (role for role in self.shared if role != 'implement')
        yield from self.own

    def __len__(self):
        return sum(1 for _ in self)


class ProfileRoutes:
    def __init__(self, shared, name):
        own = shared.profiles.setdefault(name, ({}, {}))
        vars(self).update(shared=shared,
                          commands=OwnImplementer(shared.commands, own[0]),
                          pending=OwnImplementer(shared.pending, own[1]))

    def __getattr__(self, name):
        return getattr(self.shared, name)

    def __setattr__(self, name, value):
        setattr(self.shared, name, value)


def routes(project):
    state = vars(project).get('_active_agent_routes')
    if state is None:
        state = ActiveRoutes(project)
        project._active_agent_routes = state
    return state


def reset(project):
    previous = vars(project).get('_active_agent_routes')
    if previous is not None:
        previous.close()
    project._active_agent_routes = ActiveRoutes(project)


def active_fallbacks(project):
    """A snapshot is live only while its writer holds the lock, PID reuse or not."""
    active = {}
    for path in project.holo_dir.glob('active-routes-*.json'):
        try:
            with Path(path).open() as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    active.update(json.load(stream))
        except (OSError, ValueError):
            continue  # A writer mid-publish; the next poll reads it whole.
    return active

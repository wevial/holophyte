"""Process-owned active routes, with a locked snapshot for the serve process.

A held lock proves the snapshot's writer still exists, including after a crash
or PID reuse. Closing the owner resets all seats to primary at the next start.
The store keeps history; this small file is only the console's live indicator.
"""
import fcntl
import json
import re
import shlex
import tempfile
from pathlib import Path

from holophyte.config import AGENT_CONFIG_KEYS, DEFAULT_IMPLEMENTER
from holophyte.harness import route_text
from holophyte.redact import REDACTED, known_secrets, redact_prose


def command_secrets(project):
    """Treat command arguments as private, including values echoed by a CLI."""
    secrets = set(known_secrets(project.config()))
    table = project.config().get('agents') or {}
    for seat in AGENT_CONFIG_KEYS.values():
        for key in (seat, seat + '_fallback'):
            command = table.get(key, '')
            if not isinstance(command, str):
                continue  # A harness table carries no free-form arguments.
            for arg in shlex.split(command)[1:]:
                # An assignment's value may start with a dash; only bare
                # option names are excluded from argument redaction.
                if arg.startswith('-') and '=' not in arg:
                    continue
                value = arg.split('=', 1)[-1]
                if value:
                    secrets.add(value)
    return secrets


def safe_command(project, command):
    """Public route identifier: executable only, never command arguments.
    A table-form route is named by its harness.

    Known credentials redact substrings of the name; an argument value only
    the whole name, so `high` does not eat `codex-astra-high`."""
    command = route_text(command)
    if not command:
        return command
    name = shlex.split(command)[0]
    if name in command_secrets(project):
        return REDACTED
    return redact_prose(name, known_secrets(project.config()))


def route_prose(project, text):
    """Hide arbitrary argument echoes without damaging surrounding words.

    Command arguments are not necessarily credentials: `exec` must not eat
    `execution`. Match complete values in prose, including quoted values and
    flag assignments. Ambiguous standalone echoes stay private regardless of
    flag name or value entropy. Known credentials still redact substrings.
    Command fields use safe_command instead and never expose arguments.
    """
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
        self.stream = None
        self.project = project

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
        json.dump({AGENT_CONFIG_KEYS[role]: safe_command(self.project, command)
                   for role, command in commands.items()}, self.stream)
        self.stream.flush()

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self.commands.clear()
        self.pending.clear()


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
    """Read only snapshots whose process still owns its lock."""
    active = {}
    for path in project.holo_dir.glob('active-routes-*.json'):
        try:
            with Path(path).open() as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    active.update(json.load(stream))
        except (OSError, ValueError):
            # A writer may be publishing while the read starts; the next
            # status poll will see the complete snapshot.
            continue
    return active

"""Process-owned active routes, with a locked snapshot for the serve process.

A held lock proves the snapshot's writer still exists, including after a crash
or PID reuse. Closing the owner resets all seats to primary at the next start.
The store keeps history; this small file is only the console's live indicator.
"""
import fcntl
import json
import shlex
import tempfile
from pathlib import Path

from holophyte.config import AGENT_CONFIG_KEYS
from holophyte.redact import known_secrets, redact_prose


def command_secrets(target):
    """Treat command arguments as private, including values echoed by a CLI."""
    secrets = set(known_secrets(target.config()))
    table = target.config().get('agents') or {}
    for seat in AGENT_CONFIG_KEYS.values():
        for key in (seat, seat + '_fallback'):
            for arg in shlex.split(table.get(key, ''))[1:]:
                value = arg.split('=', 1)[-1]
                if value and not value.startswith('-'):
                    secrets.add(value)
    return secrets


def safe_command(target, command):
    """Public route identifier: executable only, never command arguments."""
    if not command:
        return command
    return redact_prose(shlex.split(command)[0], command_secrets(target))


class ActiveRoutes:
    def __init__(self, target):
        self.commands = {}
        self.pending = {}
        self.project = None
        self.failed = False
        self.stream = None
        self.target = target

    def publish(self):
        if self.stream is None:
            self.target.holo_dir.mkdir(parents=True, exist_ok=True)
            self.stream = tempfile.NamedTemporaryFile(
                mode='w+', prefix='active-routes-', suffix='.json',
                dir=self.target.holo_dir)
            fcntl.flock(self.stream, fcntl.LOCK_EX)
        self.stream.seek(0)
        self.stream.truncate()
        json.dump({AGENT_CONFIG_KEYS[role]: safe_command(self.target, command)
                   for role, command in self.commands.items()}, self.stream)
        self.stream.flush()

    def close(self):
        if self.stream is not None:
            self.stream.close()
        self.commands.clear()
        self.pending.clear()


def routes(target):
    state = vars(target).get('_active_agent_routes')
    if state is None:
        state = ActiveRoutes(target)
        target._active_agent_routes = state
    return state


def reset(target):
    previous = vars(target).get('_active_agent_routes')
    if previous is not None:
        previous.close()
    target._active_agent_routes = ActiveRoutes(target)


def active_fallbacks(target):
    """Read only snapshots whose process still owns its lock."""
    active = {}
    for path in target.holo_dir.glob('active-routes-*.json'):
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

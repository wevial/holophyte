import collections
from pathlib import Path

from holophyte.config.config_tables import CONSOLE_KEYS, split_address
from holophyte.config.reader import KNOWN_KEYS

ConsoleConfig = collections.namedtuple("ConsoleConfig", ("daemons",))


def console_config(project):
    table = project.config().get("console", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [console] must be a table, got "
            f"{type(table).__name__}")
    daemons = table.get("daemons", CONSOLE_KEYS["daemons"])
    if not isinstance(daemons, (list, tuple)):
        raise SystemExit(
            f"[holo2] {project.config_path}: [console] daemons must be a list "
            f"of HOST:PORT strings, got {daemons!r}")
    seen = set()
    for entry in daemons:
        try:
            if not isinstance(entry, str):
                raise ValueError(f"expected HOST:PORT, got {entry!r}")
            split_address(entry)
        except ValueError as error:
            raise SystemExit(
                f"[holo2] {project.config_path}: [console] daemons: {error}"
            ) from None
        if entry in seen:
            raise SystemExit(
                f"[holo2] {project.config_path}: [console] daemons: {entry!r} "
                f"is listed twice")
        seen.add(entry)
    return ConsoleConfig(daemons=tuple(daemons))


SERVE_KEYS = {
    "token_file": None,
    "machine_token_file": None,
    "actions": False,
    "config_edit": False,  # writing the file is command execution on the host
    "transcripts": [],
    "name": None,
}
# The reader imports no settings module, so `[serve]` registers its keys here.
KNOWN_KEYS["serve"] = frozenset(SERVE_KEYS)
ServeConfig = collections.namedtuple(
    "ServeConfig", ("token_file", "machine_token_file", "actions", "name",
                    "config_edit", "transcripts"))


def serve_config(project):
    table = project.config().get("serve", {})
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [serve] must be a table, got "
            f"{type(table).__name__}")
    flags = {}
    for key in ("actions", "config_edit"):
        flags[key] = table.get(key, SERVE_KEYS[key])
        if not isinstance(flags[key], bool):
            raise SystemExit(
                f"[holo2] {project.config_path}: [serve] {key} must be true or "
                f"false, got {flags[key]!r}")
    actions, config_edit = flags["actions"], flags["config_edit"]
    name = table.get("name", SERVE_KEYS["name"])
    if name is None:
        name = project.path.name
    elif not isinstance(name, str) or not name.strip() or "/" in name:
        raise SystemExit(
            f"[holo2] {project.config_path}: [serve] name must be a non-empty "
            f"systemd instance name without '/', got {name!r}")
    from holophyte.agents.transcript_config import transcript_roots
    transcripts = transcript_roots(project, table.get("transcripts", []))
    return ServeConfig(
        token_file=token_path(project, table, "token_file"),
        machine_token_file=token_path(project, table, "machine_token_file"),
        actions=actions, name=name, config_edit=config_edit,
        transcripts=transcripts)


def token_path(project, table, key):
    value = table.get(key, SERVE_KEYS[key])
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(
            f"[holo2] {project.config_path}: [serve] {key} must be a "
            f"non-empty path, got {value!r}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path(project.config_path).parent / path
    return path

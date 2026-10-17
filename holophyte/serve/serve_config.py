from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import tomllib
from contextlib import closing
from datetime import datetime, timezone

import store.read
from holophyte.admission import project_of
from holophyte.agents.probes import probe_implementer
from holophyte.config.checks import check_document
from holophyte.config.reader import KNOWN_KEYS
from holophyte.redact import RedactionError, redact, restore, spans

CONFIG_PATH = "/config"
CONFIG_ACTION = "config_edit"
CONFIG_APPLIES = "next loop start"
# The server is threaded: one PUT holds this from its read to its rename.
CONFIG_LOCK = threading.Lock()
BACKUP_STAMP = "%Y%m%dT%H%M%SZ"
PATCH_VALUE_TYPES = (str, int, float, bool, list)
TOMLKIT_MISSING = ("[holo2] the daemon needs the tomlkit module to edit the"
                   " config in place (PUT /config patch); install it with"
                   " python3 -m pip install --user -r requirements.txt")


def config_text(project):
    try:
        return project.config_path.read_text()
    except FileNotFoundError:
        return ""


def config_revision(text):
    return hashlib.sha256(text.encode()).hexdigest()


def config_saved(project):
    if not project.store_path.exists():
        return None
    with closing(store.read.open_readonly(project.store_path)) as conn:
        row = store.read.latest_intervention(
            conn, CONFIG_ACTION, project_of(conn, project))
        return None if row is None else {"at_ms": row[0], "actor": row[1]}


def read_config(project):
    """A text `redact()` cannot vouch for is served as no text at all."""
    try:
        current = config_text(project)
        text = redact(current)
    except RedactionError as bad:
        return 500, {"error": str(bad)}
    try:
        modified_ms = int(project.config_path.stat().st_mtime * 1000)
    except FileNotFoundError:
        modified_ms = None
    return 200, {"text": text, "values": config_values(text),
                 "revision": config_revision(current), "modified_ms": modified_ms,
                 "saved": config_saved(project),
                 "path": str(project.config_path), "applies": CONFIG_APPLIES}


def config_values(text):
    from holophyte.pr.github import CHECK_WAIT_S
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    merge = document.setdefault("merge", {})
    if isinstance(merge, dict):
        merge.setdefault("check_wait_sec", CHECK_WAIT_S)
    return json.loads(json.dumps(document, default=str))


def validate_config(project, text):
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as bad:
        return f"malformed TOML: {bad}"
    candidate = dataclasses.replace(project, _config=document)
    try:
        check_document(candidate)
    except SystemExit as refused:
        return str(refused)
    return None


def write_config(project, body, now=None, if_match=None):
    text = body.get("text")
    if not isinstance(body.get("check", False), bool):
        return 400, text_refusal(text, "check must be a boolean")
    if "patch" in body:
        if "check" in body:
            return 400, text_refusal(text, "check takes a text body")
        return patch_config(project, body["patch"], now, if_match)
    if not isinstance(text, str):
        return 400, text_refusal(text, "text must be the file's new"
                                " contents as a string")
    if body.get("check", False):
        restored, refused = restored_config(project, text, config_text(project))
        if refused is not None:
            return 400, text_refusal(text, refused, restored)
        return 200, {"ok": True, "checked": True}
    with CONFIG_LOCK:
        current = config_text(project)
        conflict = config_conflict(project, current, if_match)
        if conflict is not None:
            return conflict
        code, reply, written = _write_config(project, text, now, current)
    if written is not None:
        reply["probe"] = probe_changed_implementer(project, current, written)
    return code, reply


def text_refusal(text, error, restored=None):
    return {"ok": False, "error": error,
            "line": refusal_line(text, error, restored)}


def refusal_line(text, error, restored=None):
    if not isinstance(text, str):
        return None
    location = re.search(
        r"^malformed TOML: .*\(at line (\d+), column (\d+)\)$", error)
    if location:
        line, column = map(int, location.groups())
        return submitted_line(text, restored, line, column)
    named = re.search(r"\[([\w.-]+)\]\s+([\w-]+)\b", error)
    if not named:
        return None
    table, key = named.groups()
    section = ""
    for number, line in enumerate(config_key_lines(text), 1):
        header = re.fullmatch(r"\s*(\[\[?)\s*([^\[\]]+)\s*\]\]?\s*", line)
        if header:
            section = header[2].strip().strip("\"'")
            if header[1] == "[[":
                section += "[]"
        name = key if section == table else f"{table}.{key}" if not section else None
        if name and re.match(r"\s*" + dotted_key_pattern(name) + r"\s*=", line):
            return number
    return None


def submitted_line(text, restored, line, column):
    if restored is None or restored == text:
        return line
    offset = sum(len(part) + 1 for part in restored.split("\n")[:line - 1]) + column - 1
    shift = 0
    for (_, start, end), (_, restored_start, restored_end) in zip(
            spans(text), spans(restored)):
        if offset < restored_start:
            break
        if text[start:end] == restored[restored_start:restored_end]:
            continue
        if offset < restored_end:
            return text.count("\n", 0, start) + 1
        shift = text.count("\n", 0, end) - restored.count("\n", 0, restored_end)
    return line + shift


def config_key_lines(text):
    strings = (r'"""(?:\\[\s\S]|"(?!"")|[^"\\])*"""|\'\'\'[\s\S]*?\'\'\''
               r'|"(?:\\.|[^"\\\n])*"|\'[^\'\n]*\'|#[^\n]*')
    def mask(match):
        value = match[0]
        if value.startswith(("#", '\"\"\"', "'''")):
            return re.sub(r"[^\n]", " ", value)
        return value
    return re.sub(strings, mask, text).splitlines()


def dotted_key_pattern(name):
    return r"\s*\.\s*".join(
        rf"(?:{re.escape(part)}|\"{re.escape(part)}\"|'{re.escape(part)}')"
        for part in name.split("."))


def restored_config(project, text, current):
    try:
        restored = restore(text, current)
    except ValueError as bad:
        return None, str(bad)
    return restored, validate_config(project, restored)


class PatchError(ValueError):
    pass


def config_conflict(project, current, if_match):
    revision = config_revision(current)
    if if_match is not None and if_match.strip() != revision:
        return 409, {"ok": False,
                     "error": f"{project.config_path.name} changed since this revision"
                              " was read; read it again",
                     "current": revision}
    return None


def patch_config(project, patch, now, if_match=None):
    if not isinstance(patch, dict):
        return 400, {"ok": False, "error": "patch must be an object of"
                                            " dotted table.key to value"}
    with CONFIG_LOCK:
        current = config_text(project)
        conflict = config_conflict(project, current, if_match)
        if conflict is not None:
            return conflict
        try:
            text = apply_patch(current, patch)
        except PatchError as bad:
            return 400, {"ok": False, "error": str(bad)}
        code, reply, written = _write_config(
            project, text, now, current,
            how=f"patched {{path}} from the console (PUT /config patch:"
                f" {', '.join(patch)})")
    if written is not None:
        reply["probe"] = probe_changed_implementer(project, current, written)
    return code, reply


def apply_patch(current, patch):
    tomlkit = require_tomlkit()
    try:
        document = tomlkit.parse(current)
    except tomlkit.exceptions.ParseError as bad:
        raise PatchError(f"malformed TOML on disk: {bad}") from bad
    for key, value in patch.items():
        table, _, name = key.partition(".")
        if not table or not name:
            raise PatchError(f"{key}: a patch key is table.key")
        if table not in KNOWN_KEYS:
            raise PatchError(f"{key}: [{table}] is not a table this version"
                             f" reads; tables: {', '.join(sorted(KNOWN_KEYS))}")
        check_patch_value(key, value)
        section = document.get(table)
        if section is None:
            document[table] = tomlkit.table()
            section = document[table]
        elif not isinstance(section, (tomlkit.items.Table,
                                      tomlkit.items.InlineTable)):
            raise PatchError(f"{key}: [{table}] must be a table")
        if isinstance(value, float) and isinstance(section.get(name),
                                                   tomlkit.items.Integer):
            raise PatchError(f"{key}: cannot change an integer to a float")
        set_patched(tomlkit, section, name, value)
    return tomlkit.dumps(document)


def check_patch_value(key, value):
    if not isinstance(value, PATCH_VALUE_TYPES):
        raise PatchError(f"{key}: a patch value is a string, an integer, a"
                         f" float, a boolean or a list of strings, not"
                         f" {type(value).__name__}")
    if isinstance(value, list) and not all(isinstance(item, str)
                                           for item in value):
        raise PatchError(f"{key}: a list value holds strings only")


def set_patched(tomlkit, section, name, value):
    """Item by item: replacing a multi-line array puts it on one line."""
    existing = section.get(name)
    if isinstance(value, float) or (type(value) is int
                                   and isinstance(existing, tomlkit.items.Float)):
        value = tomlkit.float_(float(value))
    if not (isinstance(value, list)
            and isinstance(existing, tomlkit.items.Array)):
        section[name] = value
        return
    for index, item in enumerate(value):
        if index >= len(existing):
            existing.append(item)
        elif existing[index] != item:
            existing[index] = item
    while len(existing) > len(value):
        del existing[-1]


def require_tomlkit():
    try:
        import tomlkit
    except ImportError as missing:
        raise SystemExit(TOMLKIT_MISSING) from missing
    return tomlkit


def implementer_of(text):
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    agents = document.get("agents")
    return agents.get("implementer") if isinstance(agents, dict) else None


def probe_changed_implementer(project, before, after):
    """The bound `Project` read its config at bind, so `after` is probed."""
    if implementer_of(after) == implementer_of(before):
        return None
    candidate = dataclasses.replace(project, _config=tomllib.loads(after))
    result = probe_implementer(candidate)
    return None if result is None else result.to_json()


def _write_config(project, text, now, current,
                  how="replaced {path} from the console (PUT /config)"):
    restored, refused = restored_config(project, text, current)
    if refused is not None:
        return 400, text_refusal(text, refused, restored), None
    text = restored
    path = project.config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = (now or datetime.now(timezone.utc)).strftime(BACKUP_STAMP)
        backup = next_backup(path, stamp)
    note = (f"operator {how.format(path=path)};"
            f" applies at the {CONFIG_APPLIES}; previous text in "
            + (str(backup) if backup else "no backup: there was no file"))
    from holophyte.serve.serve_actions import record_action_intervention
    recorded = record_action_intervention(project, CONFIG_ACTION, note)
    if recorded is None:
        return 503, {"ok": False, "error": "the store holds no run to record"
                                            " the intervention against;"
                                            " nothing written"}, None
    if backup is not None:
        # The backup holds the file's secrets: its mode, never the umask's.
        mode = stat.S_IMODE(path.stat().st_mode)
        handle = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w") as out:
            os.fchmod(handle, mode)
            out.write(current)
    handle, staging = tempfile.mkstemp(prefix=f"{path.name}.new-",
                                       dir=path.parent)
    with os.fdopen(handle, "w") as out:
        out.write(text)
    if backup is not None:
        os.chmod(staging, stat.S_IMODE(path.stat().st_mode))
    os.replace(staging, path)
    return 200, {"ok": True, "path": str(path),
                 "revision": config_revision(config_text(project)),
                 "backup": None if backup is None else str(backup),
                 "applies": CONFIG_APPLIES, "recorded": recorded}, text


def next_backup(path, stamp):
    first = path.with_name(f"{path.name}.bak-{stamp}")
    if not first.exists():
        return first
    n = 2
    while True:
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        if not candidate.exists():
            return candidate
        n += 1

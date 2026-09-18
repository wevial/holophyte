"""holophyte.serve_config: the daemon's `/config` routes (KO-394).

Moved verbatim out of `holophyte/serve.py`: `GET /config`'s
`read_config()` with `config_text()` and `config_values()`; `PUT
/config`'s `write_config()` with `validate_config()` and the
`_write_config()`/`next_backup()` write path shared by text and patch
bodies; patch validation; and probing changes to `[agents] implementer`.
`record_action_intervention()`, shared with the `POST /actions/...`
routes, stays home: `holophyte.serve` imports this module's handlers
for its route table, so `_write_config()` reaches the name back through
a deferred `from holophyte.serve import`.
"""
from __future__ import annotations

import dataclasses
import json
import os
import stat
import tempfile
import threading
import tomllib
from datetime import datetime, timezone

from holophyte.agents import probe_implementer
from holophyte.config import KNOWN_KEYS, check_document
from holophyte.redact import RedactionError, redact, restore

# `GET /config` and `PUT /config` (KO-356), behind `[serve] config_edit =
# true` and the write token. `holophyte.redact` finds, hides and puts back
# the secret values -- any key whose name ends in `token` or `key`, so
# `token_file` (a path) is left alone. `CONFIG_LOCK` serialises the
# read-restore-validate-backup-replace of a `PUT`: the server is threaded,
# and two writers interleaved could back up the same previous text twice
# and lose one of the two edits without either being told.
CONFIG_PATH = "/config"
CONFIG_ACTION = "config_edit"
CONFIG_APPLIES = "next loop start"
CONFIG_LOCK = threading.Lock()
BACKUP_STAMP = "%Y%m%dT%H%M%SZ"
# `PUT /config` with `{"patch": {...}}` (KO-364) edits the file in place
# with `tomlkit`, the factory's one dependency (`requirements.txt`): a
# patch value is one of these, a list holding strings only.
PATCH_VALUE_TYPES = (str, int, bool, list)
TOMLKIT_MISSING = ("[holo2] the daemon needs the tomlkit module to edit the"
                   " config in place (PUT /config patch); install it with"
                   " python3 -m pip install --user -r requirements.txt")


def config_text(target):
    """The target's config file as written, `""` when there is none yet."""
    try:
        return target.config_path.read_text()
    except FileNotFoundError:
        return ""


def read_config(target):
    """`GET /config`: the file's text, secrets redacted, the same text as
    parsed `values` with the check-wait default, its path and applicability. A
    text `redact()` cannot vouch for is 500 with its sentence and no text:
    better no page than a secret on it."""
    try:
        text = redact(config_text(target))
    except RedactionError as bad:
        return 500, {"error": str(bad)}
    return 200, {"text": text, "values": config_values(text),
                 "path": str(target.config_path), "applies": CONFIG_APPLIES}


def config_values(text):
    """Parse response values, supplying the effective check wait for the UI.

    Quoted tables and triple-quoted strings become ordinary JSON values.
    Explicit values, including invalid shapes, remain visible for correction.
    Invalid TOML returns None; raw text and persisted patches stay unchanged.
    """
    from holophyte.pr import CHECK_WAIT_S
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    merge = document.setdefault("merge", {})
    if isinstance(merge, dict):
        merge.setdefault("check_wait_sec", CHECK_WAIT_S)
    return json.loads(json.dumps(document, default=str))


def validate_config(target, text):
    """Hold `text` to what startup would accept for `target`: parsed as
    TOML and run through `config.check_document()` on a copy of the target
    carrying the parsed document instead of the file's. The refusal, when
    there is one, is the loader's own sentence -- naming the file, the
    table and the key -- or `tomllib`'s; None when the document passes."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as bad:
        return f"malformed TOML: {bad}"
    candidate = dataclasses.replace(target, _config=document)
    try:
        check_document(candidate)
    except SystemExit as refused:
        return str(refused)
    return None


def write_config(target, body, now=None):
    """`PUT /config`: replace the target's config with `body["text"]`
    after validating it: `(http status, JSON-able body)`.

    `text` must be a string, 400 otherwise. Every `[redacted]` value in it
    is the current file's (`restore()`) before anything is judged, so the
    text validated and written is the whole document. A document
    `validate_config()` refuses is 400 with its sentence as `error`,
    nothing written. The `config_edit` interventions row lands first, on
    the store's newest run as the actions record theirs; a target with no
    store or no run has nothing to record against and the file is left
    alone, 503 saying so. Then the previous text is copied to
    `config.toml.bak-STAMP` beside the file (`-2`, `-3` when the second
    has a sibling already; none when there was no file) and the new text
    lands by rename from a staging file of its own, so a reader sees the
    old file or the new one and never a torn one. One `PUT` at a time
    holds `CONFIG_LOCK` from the read to the rename. The reply names the
    backup.

    A write that changes `[agents] implementer` is followed by the probe
    the loop runs at startup (KO-357): `probe_implementer()` on the
    document as written, outside the lock, its result under `probe` in
    the reply -- `null` when the key did not change or now names no route.
    The probe reports; it does not gate: the file is already replaced and
    backed up when it runs, so a route that does not answer is a `200`
    whose `probe.ok` is false, the same text the next loop start refuses
    with, and the operator fixes the key or restores the backup.
    """
    if "patch" in body:
        return patch_config(target, body["patch"], now)
    text = body.get("text")
    if not isinstance(text, str):
        return 400, {"ok": False, "error": "text must be the file's new"
                                            " contents as a string"}
    with CONFIG_LOCK:
        current = config_text(target)
        code, reply, written = _write_config(target, text, now, current)
    if written is not None:
        reply["probe"] = probe_changed_implementer(target, current, written)
    return code, reply


class PatchError(ValueError):
    """A `patch` the daemon cannot apply: its sentence names the key."""


def patch_config(target, patch, now):
    """`PUT /config` with `{"patch": {...}}` (KO-364): the current file
    edited in place with `tomlkit`, then held, recorded, backed up and
    written exactly as a `text` is. `patch` is a flat object of dotted
    `table.key` to a string, integer, boolean or list of strings; the
    table is one this version reads (`config.KNOWN_KEYS`) and is created
    when the file lacks it. Comments, order and the layout of everything
    but the patched values are kept byte for byte; a multi-line array is
    edited item by item so its lines and their comments stay. A key the
    patch cannot apply -- no table part, a table the loader does not read,
    a value of another shape, a `[table]` that is not a table (an inline
    `table = { ... }` is one, edited in place) -- is 400
    naming it and nothing is written; a result the loader refuses is the
    same 400 a `text` gets."""
    if not isinstance(patch, dict):
        return 400, {"ok": False, "error": "patch must be an object of"
                                            " dotted table.key to value"}
    with CONFIG_LOCK:
        current = config_text(target)
        try:
            text = apply_patch(current, patch)
        except PatchError as bad:
            return 400, {"ok": False, "error": str(bad)}
        code, reply, written = _write_config(
            target, text, now, current,
            how=f"patched {{path}} from the console (PUT /config patch:"
                f" {', '.join(patch)})")
    if written is not None:
        reply["probe"] = probe_changed_implementer(target, current, written)
    return code, reply


def apply_patch(current, patch):
    """`current` with every `patch` entry applied, as `tomlkit` writes
    it; `PatchError` naming the first key that cannot be."""
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
        set_patched(tomlkit, section, name, value)
    return tomlkit.dumps(document)


def check_patch_value(key, value):
    """`PatchError` unless `value` is a string, an integer, a boolean or
    a list of strings -- the shapes a patch carries and the loader reads."""
    if not isinstance(value, PATCH_VALUE_TYPES):
        raise PatchError(f"{key}: a patch value is a string, an integer, a"
                         f" boolean or a list of strings, not"
                         f" {type(value).__name__}")
    if isinstance(value, list) and not all(isinstance(item, str)
                                           for item in value):
        raise PatchError(f"{key}: a list value holds strings only")


def set_patched(tomlkit, section, name, value):
    """`section[name] = value`, editing an existing array item by item so
    a multi-line array keeps its lines and the comments beside them:
    replacing the whole array would rewrite it on one line. A removed
    entry leaves with its own line, inline comment included -- a note
    about a command no longer in the file would only mislead -- and
    every comment outside that line stays."""
    existing = section.get(name)
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
    """The `tomlkit` module, or `SystemExit` with the one line naming it
    and its install command: `serve()` asks before it binds, so a daemon
    without it fails at start rather than at the first patch."""
    try:
        import tomlkit
    except ImportError as missing:
        raise SystemExit(TOMLKIT_MISSING) from missing
    return tomlkit


def implementer_of(text):
    """`[agents] implementer` in `text`, None when unset or when the text
    does not parse: a file the loop would refuse names no route."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    agents = document.get("agents")
    return agents.get("implementer") if isinstance(agents, dict) else None


def probe_changed_implementer(target, before, after):
    """`probe_implementer().to_json()` for the `after` document when its
    `[agents] implementer` differs from `before`'s; None otherwise. The
    probed target carries `after` parsed, not the file: the `Target`
    the server was bound with read its config once, at bind."""
    if implementer_of(after) == implementer_of(before):
        return None
    candidate = dataclasses.replace(target, _config=tomllib.loads(after))
    result = probe_implementer(candidate)
    return None if result is None else result.to_json()


def _write_config(target, text, now, current,
                  how="replaced {path} from the console (PUT /config)"):
    """`(status, reply, written)` under the lock: `written` is the text
    on disk after a `200`, None when nothing was. `how` opens the
    interventions note, `{path}` the file."""
    try:
        text = restore(text, current)
    except ValueError as bad:
        return 400, {"ok": False, "error": str(bad)}, None
    refused = validate_config(target, text)
    if refused is not None:
        return 400, {"ok": False, "error": refused}, None
    path = target.config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = (now or datetime.now(timezone.utc)).strftime(BACKUP_STAMP)
        backup = next_backup(path, stamp)
    note = (f"operator {how.format(path=path)};"
            f" applies at the {CONFIG_APPLIES}; previous text in "
            + (str(backup) if backup else "no backup: there was no file"))
    from holophyte.serve_actions import record_action_intervention
    recorded = record_action_intervention(target, CONFIG_ACTION, note)
    if recorded is None:
        return 503, {"ok": False, "error": "the store holds no run to record"
                                            " the intervention against;"
                                            " nothing written"}, None
    if backup is not None:
        # The backup holds the same secrets as the file: it is born with
        # the file's mode, never the umask's default for a new file.
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
                 "backup": None if backup is None else str(backup),
                 "applies": CONFIG_APPLIES, "recorded": recorded}, text


def next_backup(path, stamp):
    """`path.bak-STAMP`, or the first of `-2`, `-3`, ... that does not
    exist yet: two writes in one second keep two backups."""
    first = path.with_name(f"{path.name}.bak-{stamp}")
    if not first.exists():
        return first
    n = 2
    while True:
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        if not candidate.exists():
            return candidate
        n += 1

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from time import time

from holophyte.serve.server import authorized

PREFS_PATH = "/prefs"
PREFS_FILE = "console-prefs.json"
MAX_PREFS_BODY = 16 * 1024
KEYS = ("day_log",)
PRESETS = ("highlights", "everything", "manual")
DAY_LOG_FIELDS = ("preset", "items", "projects")
MAX_ENTRIES = 64
MAX_PROJECT_NAME = 128
ITEM_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
WRITING = threading.Lock()


def check_map(field, value, key_ok, key_rule):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    if len(value) > MAX_ENTRIES:
        raise ValueError(f"{field} holds at most {MAX_ENTRIES} entries")
    for key, flag in value.items():
        if not key_ok(key):
            raise ValueError(f"{field}: {key!r} is not {key_rule}")
        if not isinstance(flag, bool):
            raise ValueError(f"{field}.{key} must be a boolean")


def check_day_log(day_log):
    if not isinstance(day_log, dict):
        raise ValueError("day_log must be an object or null")
    for field in day_log:
        if field not in DAY_LOG_FIELDS:
            raise ValueError(f"day_log.{field} is not a day_log field")
    for field in DAY_LOG_FIELDS:
        if field not in day_log:
            raise ValueError(f"day_log.{field} is missing")
    if day_log["preset"] not in PRESETS:
        raise ValueError(
            f"day_log.preset must be one of {', '.join(PRESETS)}")
    check_map("day_log.items", day_log["items"],
              lambda key: ITEM_ID.fullmatch(key) is not None,
              "an item id (letters, digits, - and _, up to 64)")
    check_map("day_log.projects", day_log["projects"],
              lambda key: 0 < len(key) <= MAX_PROJECT_NAME,
              f"a project name (up to {MAX_PROJECT_NAME} characters)")


def check_body(body):
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    for key, value in body.items():
        if key not in KEYS:
            raise ValueError(f"{key} is not a preference; only day_log is")
        if value is not None:
            check_day_log(value)
    return body


def parse_body(raw):
    try:
        body = json.loads(raw)
    except ValueError:
        raise ValueError("body must be a JSON object") from None
    return check_body(body)


def stored(home):
    path = home / PREFS_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as bad:
        raise ValueError(f"{path}: {bad.strerror or bad}") from None
    try:
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("the document must be a JSON object")
        updated = document.pop("updated_ms", None)
        if updated is not None and (isinstance(updated, bool)
                                    or not isinstance(updated, int)):
            raise ValueError("updated_ms must be an integer")
        check_body(document)
    except ValueError as bad:
        raise ValueError(f"{path}: {bad}") from None
    return {**document, "updated_ms": updated}


def read_prefs(home):
    try:
        document, error = stored(home), None
    except ValueError as bad:
        document, error = {}, str(bad)
    answer = {"day_log": document.get("day_log"),
              "updated_ms": document.get("updated_ms")}
    return answer if error is None else {**answer, "error": error}


def write_prefs(home, body, now):
    with WRITING:
        try:
            document = stored(home)
        except ValueError:
            document = {}
        for key, value in body.items():
            if value is None:
                document.pop(key, None)
            else:
                document[key] = value
        document["updated_ms"] = now
        replace(home / PREFS_FILE, json.dumps(document, indent=2) + "\n")
        return read_prefs(home)


def replace(path, text):
    """`mkstemp` creates the file 0600, so the document is never wider."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent,
                                             prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def get_prefs(handler):
    if not handler.admitted(handler.server.read_token):
        return handler.answer(401, {})
    handler.answer(200, read_prefs(handler.server.host.home))


def put_prefs(handler):
    server = handler.server
    if server.write_token is None or not authorized(
            handler.headers.get("Authorization"), server.write_token):
        return handler.answer(401, {})
    if not server.actions:
        return handler.answer(404, {"error": "not found", "path": PREFS_PATH})
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = -1
    if not 0 <= length <= MAX_PREFS_BODY:
        handler.close_connection = True
        return handler.answer(400, {
            "error": f"body must be at most {MAX_PREFS_BODY} bytes"})
    try:
        body = parse_body(handler.rfile.read(length))
    except ValueError as bad:
        return handler.answer(400, {"error": str(bad)})
    try:
        answer = write_prefs(server.host.home, body, int(time() * 1000))
    except OSError as bad:
        return handler.answer(500, {
            "error": f"{server.host.home / PREFS_FILE}: {bad}"})
    handler.answer(200, answer)

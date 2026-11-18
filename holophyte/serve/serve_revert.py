from __future__ import annotations

import dataclasses
import io
import re

from holophyte.board.provider import board_for
from holophyte.cli.board_verbs import require_board
from holophyte.redact import REDACTED, known_secrets, outbound
from holophyte.revert.checks import Refused
from holophyte.revert.command import checked, file_revert, revert_turn
from holophyte.revert.settings import revert_config
from holophyte.serve.serve_actions import action_store, written_on
from holophyte.serve.serve_runs import no_store

REVERT_ACTION = "revert"
URL_USERINFO = re.compile(r"(\b[a-zA-Z][\w+.-]*://)[^/\s@]+@")
URL_QUERY = re.compile(r"(\b[a-zA-Z][\w+.-]*://[^\s?#'\"]*)\?[^\s#'\"]+")


def scrubbed(project, text):
    text = outbound(text, known_secrets(project.config()))
    text = URL_USERINFO.sub(rf"\g<1>{REDACTED}@", text)
    return URL_QUERY.sub(rf"\g<1>?{REDACTED}", text)


def refusal(reason, paths=(), enabled=True):
    return 409, {"error": f"{reason}; nothing filed", "paths": list(paths),
                 "enabled": enabled, "dependents": [],
                 "refused": {"reason": reason, "paths": list(paths)}}


def revert_action(project, body):
    key, reason = body.get("key"), body.get("reason")
    dry_run = body.get("dry_run", False)
    if not isinstance(key, str) or not key.strip():
        return 400, {"error": "key must name a merged ticket (KO-n)"}
    if not isinstance(reason, str) or not reason.strip():
        return 400, {"error": "reason must be non-empty text"}
    if type(dry_run) is not bool:
        return 400, {"error": "dry_run must be true or false"}
    key = key.strip()
    project = dataclasses.replace(project, _config=None)
    if not revert_config(project).enabled:
        if dry_run:
            return 200, {"enabled": False, "refused": None, "dependents": []}
        return refusal(f"{key}: [revert] enabled is not true in"
                       f" {project.config_path}", enabled=False)
    if not project.store_path.exists():
        return 503, no_store(project)
    board = require_board(project, board_for(project))
    conn = action_store(project)
    try:
        with revert_turn(project):
            return checked_and_filed(project, conn, board, key, reason,
                                     dry_run)
    finally:
        conn.close()


def checked_and_filed(project, conn, board, key, reason, dry_run):
    try:
        merge, found = checked(project, conn, board, key, lambda line: None)
    except Refused as refused:
        return refusal(scrubbed(project, str(refused)), refused.paths)
    if dry_run:
        return 200, {"enabled": True, "refused": None, "dependents": [
            {"key": one.key, "sha": one.sha, "paths": list(one.paths)}
            for one in found]}
    said = io.StringIO()
    try:
        filed = file_revert(project, conn, board, merge, reason, found, said)
    except SystemExit as stopped:
        return 500, {"error": scrubbed(project, said.getvalue().strip()
                                       or f"filing exited {stopped.code}")}
    return 200, {"key": filed, "recorded": written_on(conn, REVERT_ACTION)}

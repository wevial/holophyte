import io
import re
import sys
import tempfile
from pathlib import Path

import store
from holophyte.board.projection import file_ticket
from holophyte.cli.board_verbs import require_board
from holophyte.config.config_tables import board_config
from holophyte.loop.runs import open_store
from holophyte.revert.checks import (
    MAIN,
    Refused,
    dependent_lines,
    dependents,
    fetch_origin,
    merged_run,
    open_revert,
    refused_paths,
)
from holophyte.revert.settings import revert_config
from holophyte.revert.ticket_text import revert_ticket

FILED = re.compile(r"^\[holo2\] filed (\S+): ", re.MULTILINE)


def revert(target, board, key, reason, dry_run):
    if not revert_config(target).enabled:
        raise SystemExit(f"[holo2] {key}: [revert] enabled is not true in"
                         f" {target.config_path}; nothing filed")
    if not target.store_path.exists():
        raise SystemExit(f"[holo2] no store at {target.store_path}")
    board = require_board(target, board)
    conn = open_store(target)
    try:
        return _revert(target, conn, board, key, reason, dry_run)
    except Refused as refused:
        raise SystemExit(f"[holo2] {refused}; nothing filed") from None
    finally:
        conn.close()


def _revert(target, conn, board, key, reason, dry_run):
    def say(line):
        print(f"[holo2] {key}: {line}")

    say("[revert] enabled")
    unfetched = fetch_origin(target.path)
    if unfetched is not None:
        say(f"warning: git fetch origin failed, so {MAIN} is as last"
            f" fetched: {unfetched}")
    merge = merged_run(conn, target.path, key)
    say(f"merged at {merge.sha}, on {MAIN}")
    refused = refused_paths(merge.files, revert_config(target).refuse_paths)
    if refused:
        raise Refused(f"{key}: its merge touches {', '.join(refused)}, which"
                      " [revert] refuse_paths refuses; ship a forward fix"
                      " instead")
    say(f"touches no refused path: {', '.join(merge.files)}")
    filed = open_revert(conn, board, merge)
    if filed is not None:
        raise Refused(f"{key}: {filed} is its open revert ticket")
    say("no open revert ticket")
    found = dependents(conn, target.path, merge)
    for line in dependent_lines(found):
        say(f"dependent: {line}")
    if dry_run:
        say("dry run; nothing filed")
        return 0
    return _file(target, conn, board, merge, revert_ticket(merge, reason, found),
                 reason)


def _file(target, conn, board, merge, text, reason):
    printed = io.StringIO()
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / f"revert-{merge.key}.md"
        path.write_text(text)
        broken = None
        try:
            code = file_ticket(target, path, "Todo", board, out=printed,
                               priority="urgent")
        except Exception as raised:
            code, broken = 2, raised
    sys.stdout.write(printed.getvalue())
    filed = FILED.search(printed.getvalue())
    if filed is None:
        if broken is not None:
            raise broken
        raise SystemExit(code or 1)
    store.record_intervention(conn, merge.run_id, "revert",
                              f"filed {filed[1]}: {reason}")
    if broken is not None:
        print(f"[holo2] {filed[1]}: filed, then not confirmed: {broken}")
    label = board_config(target).label
    if code == 0 and label is not None:
        try:
            board.label_issue(board.fetch_task(filed[1])["issue_id"], label)
        except Exception as unlabeled:
            print(f"[holo2] {filed[1]}: filed, its label {label!r} not"
                  f" confirmed added: {unlabeled}")
            code = 2
    if code != 0:
        raise SystemExit(code)
    print(f"[holo2] {merge.key}: revert ticket {filed[1]} filed")
    return 0

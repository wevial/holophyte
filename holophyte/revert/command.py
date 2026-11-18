import io
import re
import sys
import tempfile
from pathlib import Path

import store
from holophyte.board.projection import file_ticket
from holophyte.loop.runs import open_store
from holophyte.revert.checks import (
    MAIN,
    Refused,
    dependent_lines,
    dependents,
    merged_run,
    open_revert,
    refused_paths,
)
from holophyte.revert.settings import revert_config
from holophyte.revert.ticket_text import revert_ticket

FILED = re.compile(r"^\[holo2\] filed (\S+): ", re.MULTILINE)


def revert(target, board, key, reason, dry_run=False, out=None):
    out = sys.stdout if out is None else out
    if not revert_config(target).enabled:
        raise SystemExit(f"[holo2] {key}: [revert] enabled is not true in"
                         f" {target.config_path}; nothing filed")
    if not target.store_path.exists():
        raise SystemExit(f"[holo2] no store at {target.store_path}")
    conn = open_store(target)
    try:
        return _revert(target, conn, board, key, reason, dry_run, out)
    except Refused as refused:
        raise SystemExit(f"[holo2] {refused}; nothing filed") from None
    finally:
        conn.close()


def _revert(target, conn, board, key, reason, dry_run, out):
    def say(line):
        print(f"[holo2] {key}: {line}", file=out)

    say("[revert] enabled")
    merge = merged_run(conn, target.path, key)
    say(f"merged at {merge.sha}, on {MAIN}")
    refused = refused_paths(merge.files, revert_config(target).refuse_paths)
    if refused:
        raise Refused(f"{key}: its merge touches {', '.join(refused)}, which"
                      " [revert] refuse_paths refuses; ship a forward fix"
                      " instead")
    say(f"touches no refused path: {', '.join(merge.files)}")
    filed = open_revert(conn, merge)
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
                 reason, out)


def _file(target, conn, board, merge, text, reason, out):
    printed = io.StringIO()
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / f"revert-{merge.key}.md"
        path.write_text(text)
        code = file_ticket(target, path, "Todo", board, out=printed,
                           priority="urgent")
    out.write(printed.getvalue())
    filed = FILED.search(printed.getvalue())
    if code != 0 or filed is None:
        raise SystemExit(code or 1)
    store.record_intervention(conn, merge.run_id, "revert",
                              f"filed {filed[1]}: {reason}")
    print(f"[holo2] {merge.key}: revert ticket {filed[1]} filed", file=out)
    return 0

import sys

from holophyte.board.board_diff import board_diff
from holophyte.board.board_import import board_import
from holophyte.config.config_tables import board_mode
from holophyte.host.registry import native_key_conflict
from holophyte.host.sweep_report import sweep_report
from store import RevisionMoved


def _refuse_native_key(target, args, modes):
    if any(getattr(args, action.dest) != action.default
           for action in modes._group_actions):
        return
    conflict = native_key_conflict(target)
    if conflict is not None:
        raise SystemExit(conflict)


def _board_read(args, target, board):
    if args.board_diff:
        return board_diff(target, require_board(target, board))
    if board_mode(target).kind == "native":
        raise SystemExit(
            f"[holo2] {target.config_path}: [board] kind is \"native\" -- "
            "--board-import copies a Linear board into the store, and this "
            "project's board is the store already")
    return board_import(target, require_board(target, board),
                        dry_run=args.dry_run)


def _board_mode(args, target, board):
    if args.sweep:
        return lambda: sweep_report(target, act=args.act, provider=board)
    if args.board_diff or args.board_import:
        return lambda: _board_read(args, target, board)
    if args.move or args.cancel:
        return lambda: _board_verb(args, board)
    return None


def _board_verb(args, board, out=None):
    out = sys.stdout if out is None else out
    identifier = args.move[0] if args.move else args.cancel
    try:
        if args.move:
            revision = board.move(identifier, args.move[1], args.revision,
                                  args.note)
            line = f"moved {identifier} to {args.move[1]} (revision {revision})"
        else:
            revision, run, closed = board.cancel(identifier, args.revision,
                                                 args.note)
            line = f"canceled {identifier} (revision {revision})"
            if run is not None:
                line += f"; run {run} ends abandoned at its next safe point"
            elif closed is not None:
                line += f"; run {closed[0]} ended abandoned"
                if closed[1] is not None:
                    line += f", {closed[1]} left open"
    except RevisionMoved as moved:
        line = (f"{identifier} is at revision {moved.current}, not "
                f"{moved.expected}; nothing changed")
        print(f"[holo2] {line}", file=out)
        return 1
    except ValueError as refused:
        print(f"[holo2] {refused}", file=out)
        return 1
    print(f"[holo2] {line}", file=out)
    return 0


def require_board(target, board):
    if board is None:
        raise SystemExit(
            f"[holo2] {target.config_path}: [board] project_id is not set -- "
            "add a [board] table with project_id (the Linear project UUID) "
            "and team (the Linear team name)")
    return board

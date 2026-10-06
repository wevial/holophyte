from holophyte.cli.arguments import APPROVE_DEFAULT_NOTE
from holophyte.cli.board_verbs import require_board
from holophyte.cli.cli_story import story_verb
from holophyte.cli.operator import (
    BABYSIT_DEFAULT_NOTE,
    approve,
    babysit_ticket,
    close_ticket,
    gap_layer,
    repoint,
    requeue,
)


def _store_verb(args, target, board):
    if story_verb(args, target, board):
        return True
    if args.abort:
        from holophyte.loop.stop import abort_command
        abort_command(target, args.abort, args.note,
                      provider=require_board(target, board), close=args.close_pr)
        return True
    if args.pause or args.resume:
        from holophyte.loop.stop import command
        command(target, args.pause or args.resume, args.note, resume=bool(args.resume))
        return True
    if args.hold or args.release_hold:
        from holophyte.admission import change
        change(target, args.hold, args.note)
        return True
    if args.requeue is not None:
        requeue(target, args.requeue, args.note,
                provider=require_board(target, board))
        return True
    if args.approve is not None:
        require_board(target, board)
        approve(target, args.approve,
                args.note if args.note is not None else APPROVE_DEFAULT_NOTE,
                force=args.force)
        return True
    if args.babysit is not None:
        require_board(target, board)
        babysit_ticket(target, args.babysit,
                        args.note if args.note is not None
                        else BABYSIT_DEFAULT_NOTE, author=args.author)
        return True
    if args.gap_layer is not None:
        identifier, layer = args.gap_layer
        gap_layer(target, identifier, layer, args.note,
                  carried_by=args.carried_by, found_by=args.found_by)
        return True
    if args.close is not None:
        close_ticket(target, args.close, args.landed, args.note,
                     provider=require_board(target, board))
        return True
    if args.repoint is not None:
        identifier, sha = args.repoint
        repoint(target, identifier, sha, args.note)
        return True
    return False

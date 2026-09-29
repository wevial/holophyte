"""`--file-story SLUG`: the story verb's option and its dispatch."""
import sys

from holophyte.board import FILE_TICKET_PRIORITIES
from holophyte.story_filing import StoryRefused, file_story, update_story


def add_story_arguments(parser, modes):
    modes.add_argument(
        "--file-story", metavar="SLUG",
        help="on a native board, or a Linear board in store mode: validate "
             "the story directory stories/SLUG in the project's state "
             "directory and file it -- the parent in Backlog at needs_spec, "
             "each child in Backlog in dependency order with --priority (on "
             "Linear, a sub-issue of the parent), and the story rows, "
             "planned, in one store transaction -- then write 'Story: KEY-n' "
             "atop its story.md and 'Ticket: KEY-m' atop each child; exits 1 "
             "with the problem and nothing written when the story is invalid "
             "or already filed. "
             "With --update KEY-n --revision N: apply the directory to the "
             "story filed as KEY-n, its parent read at revision N -- edit "
             "each child file headed 'Ticket:', file each child without one "
             "in Backlog, and rewrite the story rows; a change to the plan "
             "returns the story to planned")


def check_story_arguments(parser, args):
    if args.file_story is None:
        return
    if args.update is not None and args.revision is None:
        parser.error("--file-story --update is made at --revision N, the "
                     "revision the story's parent was read at")
    if args.labels is not None:
        parser.error("--labels is what --file-ticket --update edits a native "
                     "ticket with; --file-story takes none")


def story_verb(args, target, board, out=None):
    if args.file_story is None:
        return False
    out = sys.stdout if out is None else out
    if args.update is not None and not getattr(board, "native", False):
        print("[holo2] --file-story --update changes a story on a native "
              "board only; a Linear story's children are edited on Linear",
              file=out)
        raise SystemExit(1)
    if not getattr(board, "store_mode", False):
        print("[holo2] --file-story files on a native board or a Linear "
              "board in store mode ([board] mode = \"store\")", file=out)
        raise SystemExit(1)
    priority = FILE_TICKET_PRIORITIES[args.priority] if args.priority else None
    try:
        if args.update is not None:
            lines = update_story(board, target, args.file_story, args.update,
                                 args.revision, priority=priority)
        else:
            filed = file_story(board, target, args.file_story,
                               priority=priority)
    except StoryRefused as refused:
        for line in refused.lines:
            print(f"[holo2] {line}", file=out)
        raise SystemExit(1) from None
    if args.update is not None:
        for line in lines:
            print(f"[holo2] {line}", file=out)
        return True
    for identifier, title, role in filed:
        detail = ", ".join(filter(None, (role, "Backlog", args.priority)))
        print(f"[holo2] filed {identifier}: {title} ({detail})", file=out)
    return True

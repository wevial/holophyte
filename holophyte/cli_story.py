"""`--file-story SLUG`: the story verb's option and its dispatch."""
import sys

from holophyte.board import FILE_TICKET_PRIORITIES
from holophyte.story_filing import StoryRefused, file_story


def add_story_arguments(parser, modes):
    modes.add_argument(
        "--file-story", metavar="SLUG",
        help="on a native board: validate the story directory stories/SLUG "
             "in the project's state directory and file it in one "
             "transaction -- the parent in Backlog at needs_spec, each child "
             "in Backlog in dependency order with --priority, and the story "
             "rows, planned -- then write 'Story: KEY-n' atop its story.md "
             "and 'Ticket: KEY-m' atop each child; exits 1 with the problem "
             "and nothing written when the story is invalid or already filed")


def story_verb(args, target, board, out=None):
    """Run the story verb the command line names and answer True, or answer
    False when it names none; a refusal prints its lines and exits 1."""
    if args.file_story is None:
        return False
    out = sys.stdout if out is None else out
    if not getattr(board, "native", False):
        print("[holo2] --file-story files on a native board only "
              "([board] kind = \"native\")", file=out)
        raise SystemExit(1)
    priority = FILE_TICKET_PRIORITIES[args.priority] if args.priority else None
    try:
        filed = file_story(board, target, args.file_story, priority=priority)
    except StoryRefused as refused:
        for line in refused.lines:
            print(f"[holo2] {line}", file=out)
        raise SystemExit(1) from None
    for identifier, title, role in filed:
        detail = ", ".join(filter(None, (role, "Backlog", args.priority)))
        print(f"[holo2] filed {identifier}: {title} ({detail})", file=out)
    return True

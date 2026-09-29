"""`--file-story SLUG` and `--approve-story KEY-n`: options and dispatch."""
import sys

from holophyte.board import FILE_TICKET_PRIORITIES
from holophyte.story_approval import ApprovalRefused, approve
from holophyte.story_filing import StoryRefused, file_story, update_story

RED_KINDS = ("exception",)


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
    modes.add_argument(
        "--approve-story", metavar="KEY-n",
        help="on a native board, or a Linear board in store mode: approve "
             "the planned story KEY-n at its parent's --revision N, "
             "recording --note -- run every witness at main's tip with the "
             "story's witness files copied in, each verdict a baseline row "
             "of the ledger; unless each is red by an assertion (or "
             "overridden) exit 1 with the story still planned; else freeze "
             "the plan and release every child to Ready (on Linear, a "
             "queued Todo push). Refused while another story of the project "
             "is approved or parked")
    parser.add_argument(
        "--baseline-green", metavar="W", action="append", default=[],
        help="with --approve-story: approve although witness W is green at "
             "main's tip; may be given more than once")
    parser.add_argument(
        "--baseline-red-kind", nargs=2, metavar=("KIND", "W"),
        action="append", default=[],
        help="with --approve-story: approve although witness W is red by "
             "KIND, which is exception; may be given more than once")


def check_story_arguments(parser, args):
    _check_approval_arguments(parser, args)
    if args.file_story is None:
        return
    if args.update is not None and args.revision is None:
        parser.error("--file-story --update is made at --revision N, the "
                     "revision the story's parent was read at")
    if args.labels is not None:
        parser.error("--labels is what --file-ticket --update edits a native "
                     "ticket with; --file-story takes none")


def _check_approval_arguments(parser, args):
    if args.approve_story is None:
        if args.baseline_green or args.baseline_red_kind:
            parser.error("--baseline-green and --baseline-red-kind belong to "
                         "--approve-story")
        return
    if args.revision is None:
        parser.error("--approve-story is made at --revision N, the revision "
                     "the story's parent was read at")
    for kind, _key in args.baseline_red_kind:
        if kind not in RED_KINDS:
            parser.error(f"--baseline-red-kind takes {', '.join(RED_KINDS)}, "
                         f"not {kind!r}")


def _approve_story(args, target, board, out):
    if not getattr(board, "store_mode", False):
        print("[holo2] --approve-story approves on a native board or a "
              "Linear board in store mode ([board] mode = \"store\")",
              file=out)
        raise SystemExit(1)
    try:
        lines = approve(board, target, args.approve_story, args.revision,
                        args.note, green=args.baseline_green,
                        exception=[key for _kind, key
                                   in args.baseline_red_kind])
    except ApprovalRefused as refused:
        for line in refused.lines:
            print(f"[holo2] {line}", file=out)
        raise SystemExit(1) from None
    for line in lines:
        print(f"[holo2] {line}", file=out)
    return True


def story_verb(args, target, board, out=None):
    out = sys.stdout if out is None else out
    if args.approve_story is not None:
        return _approve_story(args, target, board, out)
    if args.file_story is None:
        return False
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

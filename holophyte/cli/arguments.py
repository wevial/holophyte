import argparse

from holophyte.board.projection import FILE_TICKET_PRIORITIES
from holophyte.cli.cli_story import add_story_arguments
from holophyte.cli.operator import BABYSIT_DEFAULT_NOTE
from holophyte.config.config_tables import SUPERVISE_INTERVAL_SEC
from holophyte.serve.server import ADDRESS_SHAPE, parse_address
from store.enums import GapFinder, GapLayer

FILE_TICKET_STATES = ("Todo", "Backlog")
APPROVE_DEFAULT_NOTE = "approved for merge"


def serve_address(text):
    if text == "":
        return text
    try:
        parse_address(text)
    except ValueError as bad:
        raise argparse.ArgumentTypeError(str(bad)) from None
    return text


def _file_ticket_only(parser, args):
    if args.file_ticket is None:
        if args.update is not None and args.file_story is None:
            parser.error("--update says which issue --file-ticket replaces "
                         "the body of; it names nothing by itself")
        for flag, value in (("--state", args.state),
                            ("--priority", None if args.file_story
                             else args.priority)):
            if value is not None:
                parser.error(f"{flag} is what --file-ticket creates the "
                             "issue with; it names nothing by itself")
    if args.update is None:
        board_verb = (args.move is not None or args.cancel is not None
                      or args.approve_story is not None)
        for flag, value in (("--revision", None if board_verb else args.revision),
                            ("--labels", args.labels)):
            if value is not None:
                parser.error(f"{flag} is what --file-ticket --update edits "
                             "a native ticket with; it names nothing by itself")
    elif args.state is not None:
        parser.error("--state is set when --file-ticket creates an "
                     "issue; --update leaves it as it is")


def _native_update_only(parser, args, board):
    if (args.update is None or args.file_story is not None
            or getattr(board, "native", False)):
        return
    if args.priority is not None:
        parser.error("--priority is set when --file-ticket creates an "
                     "issue; --update leaves it as it is")
    for flag, value in (("--revision", args.revision),
                        ("--labels", args.labels)):
        if value is not None:
            parser.error(f"{flag} is a native board's; --update on this "
                         "board takes none")


def _board_verb_checks(parser, args):
    if args.move is not None and args.move[1] not in ("ready", "backlog"):
        parser.error(f"--move takes a ticket to ready or backlog, not "
                     f"{args.move[1]!r}")
    if (args.move or args.cancel) and args.revision is None:
        parser.error(f"{'--move' if args.move else '--cancel'} is made at "
                     "--revision N, the revision the ticket was read at")


def _native_board_only(parser, args, board):
    if (args.move or args.cancel) and not getattr(board, "native", False):
        parser.error(f"{'--move' if args.move else '--cancel'} is a native "
                     "board's ([board] kind = \"native\"); this project's "
                     "tickets are moved and canceled in Linear")


def label_names(text):
    return [name for name in (part.strip() for part in text.split(","))
            if name]


def _note_checks(parser, args):
    if args.requeue is not None and not (args.note or "").strip():
        parser.error("--requeue records why the ticket goes back in the "
                     "queue; say so with --note TEXT")
    if args.repoint is not None and not (args.note or "").strip():
        parser.error("--repoint records why the candidate moved to a new "
                     "sha; say so with --note TEXT")
    if args.hold or args.release_hold or args.pause or args.resume \
            or args.abort or args.cancel or args.gap_layer \
            or args.approve_story or args.decide:
        if not (args.note or "").strip():
            parser.error("--hold, --release-hold, --pause, --resume, --abort,"
                         " --cancel, --gap-layer, --approve-story and --decide"
                         " require --note TEXT")
        return
    optional = args.approve or args.babysit or args.close or args.move
    if args.note is not None and args.requeue is None \
            and args.repoint is None and optional is None:
        parser.error("--note is what --requeue, --approve, --babysit, "
                     "--repoint, --close and --move record; it has nothing to "
                     "annotate by itself")
    if optional is not None and args.note is not None \
            and not args.note.strip():
        parser.error("--note with --approve, --babysit, --close or --move is "
                     "the operator's own words; leave it off for the default "
                     "rather than blank")


def _close_checks(parser, args):
    if args.close is not None and not (args.landed or "").strip():
        parser.error("--close requires --landed URL")
    if args.landed is not None and args.close is None:
        parser.error("--landed belongs to --close")
    if args.close_pr and not args.abort:
        parser.error("--close-pr belongs to --abort")


def _gap_layer_checks(parser, args):
    layers = [member.value for member in GapLayer]
    if args.gap_layer is not None and args.gap_layer[1] not in layers:
        parser.error(f"--gap-layer takes one of {', '.join(layers)}, not "
                     f"{args.gap_layer[1]!r}")
    if args.carried_by is not None and args.gap_layer is None:
        parser.error("--carried-by belongs to --gap-layer")
    if args.found_by is not None and args.gap_layer is None:
        parser.error("--found-by belongs to --gap-layer")


def _modifier_checks(parser, args):
    if args.act and not args.sweep:
        parser.error("--act says what --sweep does with the runs it finds; "
                     "it has nothing to act on by itself")
    if args.json and not args.status:
        parser.error("--json says how --status prints; it prints nothing "
                     "by itself")
    if args.once and (not args.supervise or args.target is not None):
        parser.error("--once is the host sweep's single run: --supervise "
                     "--once with no project")
    if args.import_store is not None and not args.dry_run:
        parser.error("--import-store has only its dry run yet: add --dry-run "
                     "to see what it would move; applying the import is a "
                     "later ticket built on that report (after KO-595)")
    if args.dry_run and args.import_store is None and not args.board_import:
        parser.error("--dry-run says what --import-store or --board-import "
                     "does; it has nothing to run by itself")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="factory.py",
        description="Holophyte: a minimal Linear-driven software factory.",
        epilog="Project commands: factory.py project --help")
    parser.add_argument(
        "target", metavar="project", nargs="?",
        help="repository the loop works in; left out, --status reports the "
             "host: every project in HOLOPHYTE_HOME/host.toml")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--pause", metavar="KO-n",
                       help="stop at the next safe point; requires --note")
    modes.add_argument("--resume", metavar="KO-n",
                       help="resume a paused run at its recorded boundary")
    modes.add_argument("--abort", metavar="KO-n",
                       help="end a run now: kill its turn, commit its tree as"
                       " WIP, push an open pull request's branch, park the"
                       " ticket; requires --note")
    parser.add_argument("--close-pr", action="store_true",
                        help="with --abort: then comment the note on the run's"
                        " pull request and close it; the branch is kept")
    modes.add_argument(
        "--report", action="store_true",
        help="print the project store's estimate-vs-actual table and exit; "
             "reads only -- claims no ticket, cuts no worktree, calls nobody")
    modes.add_argument(
        "--requeue", metavar="KO-n",
        help="put the ticket back in the queue after its run failed: records "
             "a 'requeue' intervention on that run carrying --note and walks "
             "the ticket to ready and clears its question in one transaction; "
             "accepts in_flight or blocked_on_operator after a failed or "
             "rejected run, or a not_reproduced park (ended abandoned, no "
             "strike); refuses live runs, other states or outcomes and other "
             "parks (use --approve or --babysit); refusals write nothing")
    modes.add_argument(
        "--approve", metavar="KO-n",
        help="release the ticket parked awaiting merge approval: records an "
             "'approve' intervention on its parked run carrying --note "
             f"(default {APPROVE_DEFAULT_NOTE!r}), ends that run with its "
             "resume point at the merge gate and walks the ticket to ready, "
             "in one transaction; the loop's next claim reuses the preserved "
             "worktree and branch, re-runs the pre-merge verify and merges "
             "without an implementer or a reviewer; refuses a ticket in any "
             "other state naming it, and writes nothing then")
    modes.add_argument(
        "--babysit", metavar="KO-n",
        help="send the ticket parked on its pull request ([merge] mode = "
             "\"pr\") back to the babysitter: a custom --note records a "
             "maintainer instruction like Send back, while no note or the "
             f"default {BABYSIT_DEFAULT_NOTE!r} requests another look; "
             "ends that run with its resume "
             "point at the merge gate and walks the ticket to ready, in one "
             "transaction; the loop's next claim resumes the candidate on "
             "the PR and reads its threads and checks again, parking again "
             "under approve = \"human\" rather than merging; refuses a "
             "ticket in any other state naming it, and writes nothing then")
    modes.add_argument(
        "--repoint", nargs=2, metavar=("KO-n", "SHA"),
        help="move the ticket's parked candidate to SHA, a full 40-hex "
             "commit id, after the branch was rebuilt by hand (rebased onto "
             "a rewritten main, say): records a 'repoint' intervention on "
             "the parked run carrying --note and an event naming the old "
             "and new shas, then sets the run's candidateSha, in one "
             "transaction; the branch itself is not touched; refuses a "
             "ticket not parked awaiting merge approval or a malformed "
             "sha, naming it, and writes nothing then")
    modes.add_argument(
        "--gap-layer", nargs=2, metavar=("KEY-n", "LAYER"),
        help="record the correction layer the lesson of the gap the ticket "
             "answers landed in -- impossible, static, witness, guidance, "
             "review or none -- carrying --note, appended to the store's "
             "gapLayers; writes no intervention row and changes no run, "
             "ticket or project state")
    parser.add_argument(
        "--carried-by", metavar="KEY-n",
        help="with --gap-layer: the ticket carrying the lesson when it is "
             "not the gap's own")
    parser.add_argument(
        "--found-by", choices=[member.value for member in GapFinder],
        help="with --gap-layer: who found the gap, a witness or the "
             "operator (the default)")
    modes.add_argument(
        "--close", metavar="KO-n",
        help="close a ticket whose change landed outside the factory; requires "
             "--landed URL and a terminal non-merged last run, with no live run")
    parser.add_argument(
        "--landed", metavar="URL",
        help="with --close: where the change landed outside the factory")
    modes.add_argument(
        "--file-ticket", metavar="TICKET.md",
        help="validate the ticket file against the project, create it as an "
             "issue in the project's board with its title, body, "
             "estimate, state and Depends-on relations, read the stored body "
             "back and validate that; exits 1 with the problem and nothing "
             "created when the file is invalid, 2 with the identifier and "
             "the problem when the stored body is")
    modes.add_argument(
        "--sweep", action="store_true",
        help="print the live runs that have tripped a mechanical condition "
             "(dead heartbeat, blown time box, stuck review) and exit; acts "
             "on none of them unless --act says to")
    modes.add_argument(
        "--board-diff", action="store_true",
        help="print every way the store's copy of the board's ready queue "
             "differs from the board's -- a board-owned field, a listed "
             "issue with no store row, a ready row the listing no longer "
             "names -- and exit 1 if there is any; writes nothing")
    modes.add_argument(
        "--import-store", metavar="PATH",
        help="with --dry-run: open the store at PATH and the project's own "
             "store read-only and print, per table, the rows an import "
             "would move, their id range, the offset a remap would add and "
             "a sha256 of the rows; refuses stores at different schema "
             "versions, and writes nothing")
    modes.add_argument(
        "--board-import", action="store_true",
        help="copy every open issue of the project's Linear board, Backlog "
             "included, into its store by board id in one transaction, "
             "printing each issue as new, changed or unchanged and a summary "
             "counting the pushes and notes still pending for Linear; rows, "
             "runs, ledger and dependsOn the store holds stay; rerun it to "
             "restart; refuses [board] kind = \"native\"")
    modes.add_argument(
        "--status", action="store_true",
        help="print what the factory is doing now -- projects, live and "
             "parked runs, ready tickets, schema, lock holders -- and exit; "
             "reads only")
    parser.add_argument("--json", action="store_true",
                        help="with --status: print it as one JSON object")
    modes.add_argument(
        "--supervise", action="store_true",
        help="run the acting sweep on an interval ([supervisor] "
             "sweep_interval_sec, default %ds) until SIGINT/SIGTERM, as the "
             "project's one supervisor: a second one for the same project "
             "exits naming the first, and a project host.toml lists is "
             "refused. With no project, the host sweep over every project "
             "in host.toml every [supervisor] sweep_sec" % SUPERVISE_INTERVAL_SEC)
    parser.add_argument(
        "--once", action="store_true",
        help="with --supervise and no project: one host sweep run, then "
             "exit 1 if any project errored; what the sweep timer runs")
    modes.add_argument(
        "--serve", metavar=ADDRESS_SHAPE, type=serve_address, nargs="?",
        const="",
        help="serve the JSON routes and the console on %s until SIGINT/SIGTERM; reads "
             "the store by default, and writes only through two opt-ins: [serve] "
             "actions (POST /actions/...) and [serve] config_edit (PUT /config); a "
             "bearer token from [serve] token_file beyond loopback. With no "
             "project, every project in host.toml under /projects/NAME, on the "
             "address given, host.toml's [serve] bind or a socket from the "
             "service manager" % ADDRESS_SHAPE)
    modes.add_argument(
        "--worker", action="store_true",
        help="internal: run as one worker of the loop's pool -- claim one "
             "ticket, work it to merge or park, exit with the run's status; "
             "spawned by the scheduler under [loop] workers > 1, not meant "
             "to be typed")
    parser.add_argument(
        "--act", action="store_true",
        help="with --sweep: fail each tripped run and release its leases, "
             "leaving its branch and worktree for a human")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="with --import-store: report what the import would do and "
             "write nothing; required, as only the dry run exists yet; with "
             "--board-import: print the same lines and write nothing")
    parser.add_argument(
        "--note", metavar="TEXT",
        help="with --hold or --release-hold: why admission changes; "
             "with --requeue: why the ticket goes back in the queue; with "
             "--repoint: why the candidate moved to the new sha; with "
             "--approve: anything the approval should say beyond "
             f"{APPROVE_DEFAULT_NOTE!r}; with --babysit: a maintainer instruction "
             f"unless {BABYSIT_DEFAULT_NOTE!r}; with --close: context for the external "
             "landing; recorded on the intervention row's "
             "event; with --cancel, required, or --move: the ticket's note; "
             "with --approve-story, required: the approval's; with "
             "--decide, required: the answer's")
    parser.add_argument(
        "--state", choices=FILE_TICKET_STATES,
        help="with --file-ticket: the workflow state the issue is created in "
             "(default %s)" % FILE_TICKET_STATES[0])
    parser.add_argument(
        "--priority", choices=tuple(FILE_TICKET_PRIORITIES),
        help="with --file-ticket or --file-story: the priority the issue or "
             "the story's tickets are created with (default none)")
    parser.add_argument(
        "--update", metavar="KO-n",
        help="with --file-story: the filed story to apply the directory "
             "to. With --file-ticket: replace that issue's title, description "
             "and estimate from the validated file instead of creating one; "
             "state, priority and relations stay as they are, and the stored "
             "body is read back and validated as on filing; on a native "
             "board it requires --revision and takes --priority and --labels")
    parser.add_argument(
        "--revision", metavar="N", type=int,
        help="with --file-ticket --update, --file-story --update, --move or "
             "--cancel on a native board, or --approve-story: the revision "
             "the ticket (a story's parent) was read at; a ticket that moved "
             "past it is left unchanged and its current revision printed")
    parser.add_argument(
        "--labels", metavar="a,b", type=label_names,
        help="with --file-ticket --update on a native board: the ticket's "
             "labels, comma-separated")
    modes.add_argument(
        "--move", nargs=2, metavar=("KEY-n", "ready|backlog"),
        help="on a native board: move the ticket to Ready or Backlog at "
             "--revision N, recording --note if given; a live run continues. "
             "A ticket at another revision exits 1 and nothing changes")
    modes.add_argument(
        "--cancel", metavar="KEY-n",
        help="on a native board: cancel the ticket at --revision N, recording "
             "--note; a live run is aborted and ends abandoned at its next "
             "safe point, naming which run")
    modes.add_argument("--hold", action="store_true",
                       help="hold project admission; requires --note")
    modes.add_argument("--release-hold", action="store_true",
                       help="release project hold; requires --note")
    add_story_arguments(parser, modes)
    return parser, modes

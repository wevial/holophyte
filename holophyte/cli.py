"""The command line: `cli()` parses the arguments and runs the mode they name.

`--report`, `--requeue KO-n --note TEXT`, `--approve KO-n [--note TEXT]`,
`--babysit KO-n [--note TEXT]`, `--repoint KO-n SHA --note TEXT`,
`--close KO-n --landed URL [--note TEXT]`,
`--file-ticket PATH [--state] [--priority]`,
`--sweep [--act]`, `--board-diff`, `--status [--json]`, `--serve` and `--supervise
[--once]` (with no project, the host's),
`--import-store PATH --dry-run`,
`--supervise`, `--serve PORT|HOST:PORT`, the internal `--worker` and the
loop itself
dispatch from here to `holophyte.operator`, `holophyte.board`,
`holophyte.supervisor`, `holophyte.status` and `holophyte.serve`; the `Project`
is built once from the command line and handed down, and the board
(`provider.board_for()`) is built here and never reached for by name below.
Importing this module locates no target, reads no config and touches no
`HOLOPHYTE_HOME`.

`factory.py` calls `cli()` through its `__main__` guard.
"""
import argparse
import subprocess
import sys
from pathlib import Path

from holophyte.board import FILE_TICKET_PRIORITIES, file_ticket
from holophyte.board_diff import board_diff
from holophyte.config import (
    check_agent_commands,
    check_config,
    check_worktree_setup,
)
from holophyte.config_tables import (
    SUPERVISE_INTERVAL_SEC,
    loop_config,
)
from holophyte.host import native_key_conflict, watched_line
from holophyte.operator import (
    BABYSIT_DEFAULT_NOTE,
    approve,
    babysit_ticket,
    close_ticket,
    main,
    repoint,
    report,
    requeue,
)
from holophyte.pool import worker
from holophyte.project import Project
from holophyte.serve import ADDRESS_SHAPE, parse_address, serve
from holophyte.startup import eager_import
from holophyte.status import host_status_report, status_report
from holophyte.store_import import dry_run
from holophyte.supervisor import supervise, supervisor_liveness_line
from holophyte.supervisor_lock import SupervisorHeld, supervisor_running
from holophyte.sweep_report import sweep_report
from provider import board_for

# The entry point the loop's spawned supervisor is started through: the
# `factory.py` beside this package, by path, so the supervisor runs the same
# checkout as the loop that started it whatever the operator's cwd was.
FACTORY_PATH = Path(__file__).resolve().parent.parent / "factory.py"
# The seam the spawn goes through, so a test patches `holophyte.cli.SPAWN`
# and never the `subprocess.Popen` every gate in the process shares.
SPAWN = subprocess.Popen

# The states `--file-ticket` may create an issue in, the default first.
FILE_TICKET_STATES = ("Todo", "Backlog")
# What `--approve` records when the operator gives no `--note`: the row is
# the point of the mode, and "merge" is the whole of what a bare approval
# says, so it needs no reason the way `--requeue` does.
APPROVE_DEFAULT_NOTE = "approved for merge"


def serve_address(text):
    """`--serve`'s argparse type: the address as typed, once it parses;
    `""`, the bare flag, is the host daemon's."""
    if text == "":
        return text
    try:
        parse_address(text)
    except ValueError as bad:
        raise argparse.ArgumentTypeError(str(bad)) from None
    return text


def _file_ticket_only(parser, args):
    """Refuse `--state`, `--priority` and `--update` given without
    `--file-ticket`: each is a field of, or a verb on, the issue that
    command works with, and names nothing alone. And refuse `--update`
    beside `--state` or `--priority`: those are create-time fields, and an
    update leaves them as they are."""
    if args.file_ticket is None:
        if args.update is not None:
            parser.error("--update says which issue --file-ticket replaces "
                         "the body of; it names nothing by itself")
        for flag, value in (("--state", args.state),
                            ("--priority", args.priority)):
            if value is not None:
                parser.error(f"{flag} is what --file-ticket creates the "
                             "issue with; it names nothing by itself")
        return
    if args.update is not None:
        for flag, value in (("--state", args.state),
                            ("--priority", args.priority)):
            if value is not None:
                parser.error(f"{flag} is set when --file-ticket creates an "
                             "issue; --update leaves it as it is")


def _note_checks(parser, args):
    """`--note` belongs to `--requeue` and `--repoint`, which require it,
    and to `--approve`, `--babysit` and `--close`, which take it. Refuse a
    required note missing, a note without an operator verb, or a blank note:
    leave it off when there is nothing to add."""
    if args.requeue is not None and not (args.note or "").strip():
        parser.error("--requeue records why the ticket goes back in the "
                     "queue; say so with --note TEXT")
    if args.repoint is not None and not (args.note or "").strip():
        parser.error("--repoint records why the candidate moved to a new "
                     "sha; say so with --note TEXT")
    if args.hold or args.release_hold or args.pause or args.resume or args.abort:
        if not (args.note or "").strip():
            parser.error("--hold, --release-hold, --pause, --resume and --abort"
                         " require --note TEXT")
        return
    optional = args.approve or args.babysit or args.close
    if args.note is not None and args.requeue is None \
            and args.repoint is None and optional is None:
        parser.error("--note is what --requeue, --approve, --babysit and "
                     "--repoint and --close record; it has nothing to annotate "
                     "by itself")
    if optional is not None and args.note is not None \
            and not args.note.strip():
        parser.error("--note with --approve, --babysit or --close is the operator's "
                     "own words; leave it off for the default rather than "
                     "blank")


def _close_checks(parser, args):
    """Require the landing reference only for an external close-out, and
    let `--close-pr` modify an `--abort` alone."""
    if args.close is not None and not (args.landed or "").strip():
        parser.error("--close requires --landed URL")
    if args.landed is not None and args.close is None:
        parser.error("--landed belongs to --close")
    if args.close_pr and not args.abort:
        parser.error("--close-pr belongs to --abort")


def _modifier_checks(parser, args):
    """Refuse a mode's modifier without its mode -- `--act` without
    `--sweep`, `--json` without `--status`, `--dry-run` without
    `--import-store` -- and `--import-store` without `--dry-run`, the only
    form of it that exists yet."""
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
    if args.dry_run and args.import_store is None:
        parser.error("--dry-run says what --import-store does with the store "
                     "it names; it has nothing to run by itself")


def cli(argv=None):
    from holophyte.cli_project import project_cli
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["project"]:
        return project_cli(argv[1:])
    return _legacy_cli(argv)


def _legacy_cli(argv):
    """Parse the command line and run the mode it names.

    An explicit parser rather than `sys.argv[1]`, which is what the target
    path used to be read from at import: every first argument was a repository
    path, so `--help` named a repository called "--help" and a mistyped flag
    started a real loop somewhere unintended. Both are now argparse errors,
    and the module can be imported without a command line at all.
    """
    parser = argparse.ArgumentParser(
        prog="factory.py",
        description="Holophyte: a minimal Linear-driven software factory.",
        epilog="Project commands: factory.py project --help")
    # Required, with no default: a default would name one operator's checkout,
    # and a bare `factory.py` would then run against a path that exists on
    # one machine. A missing target is an argparse error, the same way a
    # mistyped flag is.
    parser.add_argument(
        "target", metavar="project", nargs="?",
        help="repository the loop works in; left out, --status reports the "
             "host: every project in HOLOPHYTE_HOME/host.toml")
    # The read-only modes, exclusive of each other: each one prints its table
    # and exits, so a command line naming both is a mistake argparse should
    # answer rather than a silent choice between them.
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--pause", metavar="KO-n",
                       help="stop at the next safe point; requires --note")
    modes.add_argument("--resume", metavar="KO-n",
                       help="resume a paused run at its recorded boundary")
    modes.add_argument("--abort", metavar="KO-n",
                       help="end a run now: kill its turn, commit its tree as"
                       " WIP, push an open pull request's branch, park the"
                       " ticket; requires --note")
    # `--close` is the external close-out's, so the modifier is `--close-pr`.
    parser.add_argument("--close-pr", action="store_true",
                        help="with --abort: then comment the note on the run's"
                        " pull request and close it; the branch is kept")
    modes.add_argument(
        "--report", action="store_true",
        help="print the project store's estimate-vs-actual table and exit; "
             "reads only -- claims no ticket, cuts no worktree, calls nobody")
    # The one writing mode among them, and the only write it makes: the
    # ladder's rung-3 pair (`record_intervention` then `walk_ticket`) as a
    # command line, so a ticket whose run failed goes back in the queue with
    # its intervention row instead of through a REPL.
    modes.add_argument(
        "--requeue", metavar="KO-n",
        help="put the ticket back in the queue after its run failed: records "
             "a 'requeue' intervention on that run carrying --note and walks "
             "the ticket to ready and clears its question in one transaction; "
             "accepts in_flight or blocked_on_operator after a failed or "
             "rejected run, or a not_reproduced park (ended abandoned, no "
             "strike); refuses live runs, other states or outcomes and other "
             "parks (use --approve or --babysit); refusals write nothing")
    # The operator's answer to `merge?`: the same rung-3 pair as `--requeue`
    # for a ticket parked by `[merge] approve = "human"`, so the loop's next
    # claim takes the preserved candidate straight to the merge gate.
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
    # The PR-mode twin of `--approve`: the same transaction with its own
    # intervention action, so the loop's next claim babysits the parked
    # candidate's pull request again -- new threads, checks -- rather than
    # reading the release as the human's "merge".
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
    # The one legitimate reason a parked candidate's sha changes: the
    # operator rebuilt the branch as the same commits on a rewritten main.
    # Before this the only way was raw SQL on `runs.candidateSha`; this is
    # that write as a recorded intervention, so the gate `--approve` resumes
    # into accepts the rebuilt tip and the ledger says why.
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
        "--close", metavar="KO-n",
        help="close a ticket whose change landed outside the factory; requires "
             "--landed URL and a terminal non-merged last run, with no live run")
    parser.add_argument(
        "--landed", metavar="URL",
        help="with --close: where the change landed outside the factory")
    # The other writing mode, and it writes to the board, not the store:
    # a ticket file validated against the target becomes a Linear issue,
    # and the body Linear stored is validated again so the transfer is a
    # checked step rather than the thing the loop discovers at claim time.
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
    # Read-only on both stores for now: the apply step is a later ticket
    # built on this report, so the mode runs only with `--dry-run` said.
    modes.add_argument(
        "--import-store", metavar="PATH",
        help="with --dry-run: open the store at PATH and the project's own "
             "store read-only and print, per table, the rows an import "
             "would move, their id range, the offset a remap would add and "
             "a sha256 of the rows; refuses stores at different schema "
             "versions, and writes nothing")
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
    # The port is required and a bare one binds loopback: the only default
    # interface is the one that publishes nothing, and a read daemon on any
    # other must be named. The value is checked while parsing, so a port
    # that is not a number is a usage error naming the shapes, not a bind
    # failure later.
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
    # Internal: the child the scheduler spawns under `[loop] workers > 1`.
    # One ticket, claim to close, exit with the run's status; the scheduler
    # has already run the startup checks, the sweep and the supervisor spawn
    # for the whole pool, so this mode skips them.
    modes.add_argument(
        "--worker", action="store_true",
        help="internal: run as one worker of the loop's pool -- claim one "
             "ticket, work it to merge or park, exit with the run's status; "
             "spawned by the scheduler under [loop] workers > 1, not meant "
             "to be typed")
    # Not a mode of its own: it says what `--sweep` does with what it finds,
    # so it is refused rather than ignored anywhere else. Silently doing
    # nothing would be the worse answer for the operator who typed
    # `--act` meaning to clean up and got a read-only pass.
    parser.add_argument(
        "--act", action="store_true",
        help="with --sweep: fail each tripped run and release its leases, "
             "leaving its branch and worktree for a human")
    # `--import-store`'s modifier, as `--act` is `--sweep`'s, and for now
    # its required one: the apply step without it does not exist yet.
    parser.add_argument(
        "--dry-run", action="store_true",
        help="with --import-store: report what the import would do and "
             "write nothing; required, as only the dry run exists yet")
    # Required with `--requeue` and `--repoint`, optional with `--approve`
    # and `--babysit`/`--close`, and meaningless without one of them: the
    # intervention row is the point of these modes, and a requeue or
    # re-point row with no reason is the unrecorded action the row exists
    # to replace, while an approval says "merge" by itself.
    parser.add_argument(
        "--note", metavar="TEXT",
        help="with --hold or --release-hold: why admission changes; "
             "with --requeue: why the ticket goes back in the queue; with "
             "--repoint: why the candidate moved to the new sha; with "
             "--approve: anything the approval should say beyond "
             f"{APPROVE_DEFAULT_NOTE!r}; with --babysit: a maintainer instruction "
             f"unless {BABYSIT_DEFAULT_NOTE!r}; with --close: context for the external "
             "landing; recorded on the intervention row's "
             "event")
    # Only the two states a filed ticket can start in: Todo is ready to
    # claim, Backlog waits on triage. Anything else is a state the loop
    # projects, never one a file declares, so argparse refuses it.
    parser.add_argument(
        "--state", choices=FILE_TICKET_STATES,
        help="with --file-ticket: the workflow state the issue is created in "
             "(default %s)" % FILE_TICKET_STATES[0])
    parser.add_argument(
        "--priority", choices=tuple(FILE_TICKET_PRIORITIES),
        help="with --file-ticket: the priority the issue is created with "
             "(default none)")
    parser.add_argument(
        "--update", metavar="KO-n",
        help="with --file-ticket: replace that issue's title, description "
             "and estimate from the validated file instead of creating one; "
             "state, priority and relations stay as they are, and the stored "
             "body is read back and validated as on filing")
    modes.add_argument("--hold", action="store_true",
                       help="hold project admission; requires --note")
    modes.add_argument("--release-hold", action="store_true",
                       help="release project hold; requires --note")
    args = parser.parse_args(argv)
    eager_import()
    _file_ticket_only(parser, args)
    _modifier_checks(parser, args)
    _note_checks(parser, args)
    _close_checks(parser, args)
    if args.target is None:
        return _host_mode(parser, args)
    if args.serve == "":
        parser.error(f"--serve needs {ADDRESS_SHAPE} with a project; without"
                     " one it serves the host")
    # A dry run and `--board-diff` write nothing, and adopting legacy state
    # moves files: they locate the target without adopting, so a store still
    # in a legacy layout is reported absent rather than moved.
    target = Project.locate(args.target, adopt=args.import_store is None
                            and not args.board_diff)
    # Read the target's config here, with the command line parsed and nothing
    # claimed yet: a malformed file is a startup error about the repository
    # this invocation names, and `--help` never had to touch a config at all.
    target.config()
    # And the `[supervisor]` table is checked in the same breath, for every
    # mode: the loop's startup self-sweep, `--sweep` and `--supervise` all
    # read it, and a threshold outside its constraint is the same kind of
    # mistake as a file that does not parse -- an error about the config,
    # before anything is claimed, rather than a sweep with numbers nobody
    # chose. Unknown keys in any table the factory reads are refused in the
    # same window: a typo the factory ignored would leave the operator
    # believing a knob is set that is not.
    check_config(target)
    # The modes that read the store and call nobody, in their own function
    # so the dispatch stays under the complexity bound with all of them in it.
    read_only = _read_only_mode(args, target)
    if read_only is not None:
        return read_only()
    # The board, built once here from the target's `[board]` table and handed
    # down: nothing below reaches for Linear by name. Construction touches
    # neither the network nor the module, so a read-only sweep still calls
    # nobody; the first call that posts to the board is what reads the key.
    # A target with no table has no board, which
    # a read-only sweep can live with (it calls nobody) and the modes that
    # post to the board cannot: they exit here, naming the key to set.
    # `board_for()` refuses `[board] kind = "native"` here, before the loop.
    board = board_for(target)
    # Same window and the same reasons as `--report`: it reads runs and prints
    # them, so no route has to resolve and nobody is called. `--act` fails
    # runs rather than dispatching them, so it needs no route either.
    if args.sweep:
        return sweep_report(target, act=args.act, provider=board)
    # Reads the board, so a target without one exits naming the key.
    if args.board_diff:
        return board_diff(target, require_board(target, board))
    # The operator verbs on the store, in their own function so the
    # dispatch stays under the complexity bound with all of them in it.
    if _store_verb(args, target, board):
        return None
    # Posts to the board, so a target without one exits here naming the key
    # -- before the file is read, so the error is about the target, not the
    # file. The board is the loop's own, filed to through its `file()`,
    # `update()` and `stored_body()` members, so `--file-ticket` names no
    # board either.
    if args.file_ticket is not None:
        return file_ticket(target, args.file_ticket,
                           args.state or FILE_TICKET_STATES[0],
                           require_board(target, board),
                           priority=args.priority, update=args.update)
    # The acting sweep on a timer. Like `--sweep --act` it dispatches nothing
    # and so resolves no route; unlike it, it takes the target's supervisor
    # lock first, and a target that already has one is an exit, not a loop.
    if args.supervise:
        return _supervise_project(target, board)
    # A worker of the pool: the scheduler that spawned it live-probed the
    # routes, checked the worktree setup and started the supervisor moments
    # ago for the whole pool, so none of that is repeated per child.
    if args.worker:
        return worker(target, require_board(target, board))
    # And, on the path that actually dispatches agents, every route the config
    # names resolves before the loop claims a ticket. `--report` skips this: it
    # calls nobody, so a reviewer that is not installed on the machine reading
    # the table is not that reading's problem.
    from holophyte.admission import disabled_startup
    if disabled_startup(target):
        return 0
    _refuse_native_key(target)
    check_agent_commands(target)
    # Same window, same reason: the `[worktree]` table is read here rather
    # than by the first run that cuts a worktree with it.
    check_worktree_setup(target)
    # And the watcher, before the first claim: a supervisor is part of the
    # factory, not a second command to remember, so a target nobody is
    # supervising gets one started here, detached, unless `[loop]
    # spawn_supervisor` says a service manager owns that job.
    if loop_config(target).spawn_supervisor:
        start_supervisor(target)
    return main(target, require_board(target, board))


def _refuse_native_key(target):
    """A native board's key is its own on the host (KO-752): the loop's
    start exits naming the conflict before a route is probed."""
    conflict = native_key_conflict(target)
    if conflict is not None:
        raise SystemExit(conflict)


def _supervise_project(target, board):
    """`PROJECT --supervise`, refused for a project the host registry lists:
    the host sweep watches it."""
    watched = watched_line(target)
    if watched is not None:
        raise SystemExit(watched)
    try:
        return supervise(target, require_board(target, board))
    except SupervisorHeld as held:
        # With the liveness line, so the refusal is actionable: a held
        # lock and a fresh heartbeat is a watcher doing its job; a held
        # lock and a stale one is a watcher to go and look at.
        raise SystemExit(
            f"{held}\n{supervisor_liveness_line(target)}") from None


def _host_mode(parser, args):
    """The flags with no project mean the host; `--status`, `--serve` and
    `--supervise [--once]` are the host forms. Anything else still needs
    the project it acts on."""
    from holophyte.host import Host, HostError
    if args.serve is not None:
        from holophyte.serve_host import serve_host
        return serve_host(Host.locate(), args.serve or None)
    if args.supervise:
        from holophyte.sweep_host import supervise_host
        return supervise_host(Host.locate(), once=args.once)
    if not args.status:
        parser.error("the following arguments are required: project (only "
                     "--status, --serve and --supervise have a host form)")
    try:
        return host_status_report(Host.locate(), as_json=args.json)
    except HostError as bad:
        raise SystemExit(str(bad)) from None


def _read_only_mode(args, target):
    """Return the read-only mode the command line names as a call to make,
    or None when it names none. `--report`, `--status`, `--import-store
    --dry-run` and `--serve` read the store and call nobody, so no board is
    built and no route has to resolve."""
    if args.report:
        return lambda: report(target)
    if args.status:
        return lambda: status_report(target, as_json=args.json)
    if args.import_store is not None:
        return lambda: dry_run(target, args.import_store)
    # Same window as `--report`: a read-only daemon calls nobody.
    if args.serve is not None:
        return lambda: serve(target, args.serve)
    return None


def _store_verb(args, target, board):
    """Run the operator verb the command line names, if it is one of the
    verbs that write the store and exit, including `--close`, which also
    projects the result to the board. No agent route has to resolve first."""
    # Parks the ticket and may end the run here, which projects to the board
    # like `--close`, so a target with no board exits here naming the key.
    if args.abort:
        from holophyte.stop import abort_command
        abort_command(target, args.abort, args.note,
                      provider=require_board(target, board), close=args.close_pr)
        return True
    if args.pause or args.resume:
        from holophyte.stop import command
        command(target, args.pause or args.resume, args.note, resume=bool(args.resume))
        return True
    if args.hold or args.release_hold:
        from holophyte.admission import change
        change(target, args.hold, args.note)
        return True
    # Hands the ticket back to a loop that will mirror it to the board when
    # it claims it again, so a target with no board exits here naming the
    # key, before anything is written.
    if args.requeue is not None:
        requeue(target, args.requeue, args.note,
                provider=require_board(target, board))
        return True
    # Same shape and the same reason: the released ticket is claimed by a
    # loop that mirrors it to the board, so a target with no board exits here.
    if args.approve is not None:
        require_board(target, board)
        approve(target, args.approve,
                args.note if args.note is not None else APPROVE_DEFAULT_NOTE)
        return True
    if args.babysit is not None:
        require_board(target, board)
        babysit_ticket(target, args.babysit,
                        args.note if args.note is not None
                        else BABYSIT_DEFAULT_NOTE)
        return True
    if args.close is not None:
        close_ticket(target, args.close, args.landed, args.note,
                     provider=require_board(target, board))
        return True
    # Repoint leaves the ticket parked, so it needs no board.
    if args.repoint is not None:
        identifier, sha = args.repoint
        repoint(target, identifier, sha, args.note)
        return True
    return False


def start_supervisor(target, out=None):
    """Start a detached `--supervise` for `target` unless one is watching.

    A project the host registry lists is the host sweep's (`watched_line()`)
    and nothing is spawned for it. A live pid in the target's supervisor
    lock is a watcher already on the job, named on stdout and left alone.
    Otherwise `factory.py --supervise TARGET` is spawned in its own
    session, stdin closed, stdout and stderr appended to `supervisor.log`
    in the target's state directory -- unbuffered (`-u`), so the log reads
    live under a redirect -- and its pid is named.
    Nothing waits on it and nothing reads its output: it is meant to outlive
    the loop, it takes the lock itself, and whether it is still watching is
    the store's `supervisorHeartbeats` row. Two loops starting at once both
    spawn, and the second supervisor exits at the lock as any second
    `--supervise` does.
    """
    out = sys.stdout if out is None else out
    watched = watched_line(target)
    if watched is not None:
        print(watched, file=out, flush=True)
        return None
    pid = supervisor_running(target)
    if pid is not None:
        print(f"[holo2] supervisor pid {pid} is watching {target.path}",
              file=out, flush=True)
        return None
    target.holo_dir.mkdir(parents=True, exist_ok=True)
    with open(target.holo_dir / "supervisor.log", "ab") as log:
        child = SPAWN(
            [sys.executable, "-u", str(FACTORY_PATH), "--supervise",
             str(target.path)],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log)
    print(f"[holo2] started a supervisor for {target.path} as pid {child.pid}",
          file=out, flush=True)
    return child.pid


def require_board(target, board):
    """`board`, or the startup exit for a target that has none.

    The loop and `--supervise` post to the board, so a target with no
    `[board]` table cannot start them: the exit
    names the key to set, in the same window as a route that resolves
    nowhere, before anything is claimed.
    """
    if board is None:
        raise SystemExit(
            f"[holo2] {target.config_path}: [board] project_id is not set -- "
            "add a [board] table with project_id (the Linear project UUID) "
            "and team (the Linear team name)")
    return board

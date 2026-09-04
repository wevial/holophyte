"""The command line: `cli()` parses the arguments and runs the mode they name.

`--report`, `--requeue KO-n --note TEXT`, `--file-ticket PATH [--state]
[--priority]`,
`--sweep [--act]`, `--supervise`, `--serve HOST:PORT` and the loop itself
dispatch from here to `holophyte.loop`, `holophyte.board`,
`holophyte.supervisor` and `holophyte.serve`; the `Target`
is built once from the command line and handed down, and the board
(`LinearProvider`) is built here and never reached for by name below.
Importing this module locates no target, reads no config and touches no
`HOLOPHYTE_HOME`.

Seventh and last slice of the phase-2 module split; moved verbatim from
`factory.py`, which keeps only `from holophyte.cli import cli` and the
`__main__` guard.
"""
import argparse

from holophyte.board import FILE_TICKET_PRIORITIES, file_ticket
from holophyte.config import (
    SUPERVISE_INTERVAL_SEC,
    board_config,
    check_agent_commands,
    check_config_keys,
    loop_config,
    report_config,
    sweep_config,
)
from holophyte.loop import check_worktree_setup, main, report, requeue
from holophyte.serve import ADDRESS_SHAPE, parse_address, serve
from holophyte.supervisor import (
    SupervisorHeld,
    supervise,
    supervisor_liveness_line,
    sweep_report,
)
from holophyte.target import Target
from provider import LinearProvider

# The states `--file-ticket` may create an issue in, the default first.
FILE_TICKET_STATES = ("Todo", "Backlog")


def serve_address(text):
    """`--serve`'s argparse type: the address as typed, once it parses."""
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


def cli(argv=None):
    """Parse the command line and run the mode it names.

    An explicit parser rather than `sys.argv[1]`, which is what the target
    path used to be read from at import: every first argument was a repository
    path, so `--help` named a repository called "--help" and a mistyped flag
    started a real loop somewhere unintended. Both are now argparse errors,
    and the module can be imported without a command line at all.
    """
    parser = argparse.ArgumentParser(
        prog="factory.py",
        description="Holophyte: a minimal Linear-driven software factory.")
    # Required, with no default: a default would name one operator's checkout,
    # and a bare `factory.py` would then run against a path that exists on
    # one machine. A missing target is an argparse error, the same way a
    # mistyped flag is.
    parser.add_argument(
        "target", help="repository the loop works in")
    # The read-only modes, exclusive of each other: each one prints its table
    # and exits, so a command line naming both is a mistake argparse should
    # answer rather than a silent choice between them.
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--report", action="store_true",
        help="print the target store's estimate-vs-actual table and exit; "
             "reads only -- claims no ticket, cuts no worktree, calls nobody")
    # The one writing mode among them, and the only write it makes: the
    # ladder's rung-3 pair (`record_intervention` then `walk_ticket`) as a
    # command line, so a ticket whose run failed goes back in the queue with
    # its intervention row instead of through a REPL.
    modes.add_argument(
        "--requeue", metavar="KO-n",
        help="put the ticket back in the queue after its run failed: records "
             "a 'requeue' intervention on that run carrying --note and walks "
             "the ticket to ready, in one transaction; refuses a ticket with "
             "a live run, one not in_flight, or one whose last run did not "
             "fail, and writes nothing then")
    # The other writing mode, and it writes to the board, not the store:
    # a ticket file validated against the target becomes a Linear issue,
    # and the body Linear stored is validated again so the transfer is a
    # checked step rather than the thing the loop discovers at claim time.
    modes.add_argument(
        "--file-ticket", metavar="TICKET.md",
        help="validate the ticket file against the target, create it as an "
             "issue in the target's [board] project with its title, body, "
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
        "--supervise", action="store_true",
        help="run the acting sweep on an interval ([supervisor] "
             "sweep_interval_sec, default %ds) until SIGINT/SIGTERM, as the "
             "target's one supervisor: a second one for the same target "
             "exits naming the first" % SUPERVISE_INTERVAL_SEC)
    # The bind address is required and explicit: a default would pick an
    # interface nobody named, and a read daemon on the wrong one is either
    # unreachable or public. The value is checked while parsing, so a port
    # that is not a number is a usage error naming the shape, not a bind
    # failure later.
    modes.add_argument(
        "--serve", metavar=ADDRESS_SHAPE, type=serve_address,
        help="answer GET /status and GET /runs as JSON on %s, read-only, "
             "until SIGINT/SIGTERM; a read-only connection per request, "
             "no authentication, and writes nothing" % ADDRESS_SHAPE)
    # Not a mode of its own: it says what `--sweep` does with what it finds,
    # so it is refused rather than ignored anywhere else. Silently doing
    # nothing would be the worse answer for the operator who typed
    # `--act` meaning to clean up and got a read-only pass.
    parser.add_argument(
        "--act", action="store_true",
        help="with --sweep: fail each tripped run and release its leases, "
             "leaving its branch and worktree for a human")
    # Required with `--requeue` and meaningless without it: the intervention
    # row is the point of the mode, and a row with no reason is the
    # unrecorded action the row exists to replace.
    parser.add_argument(
        "--note", metavar="TEXT",
        help="with --requeue: why the ticket goes back in the queue, "
             "recorded on the intervention row's event")
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
    args = parser.parse_args(argv)
    _file_ticket_only(parser, args)
    if args.act and not args.sweep:
        parser.error("--act says what --sweep does with the runs it finds; "
                     "it has nothing to act on by itself")
    if args.requeue is not None and not (args.note or "").strip():
        parser.error("--requeue records why the ticket goes back in the "
                     "queue; say so with --note TEXT")
    if args.note is not None and args.requeue is None:
        parser.error("--note is what --requeue records; it has nothing to "
                     "annotate by itself")
    target = Target.locate(args.target)
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
    check_config_keys(target)
    sweep_config(target)
    loop_config(target)
    report_config(target)
    if args.report:
        return report(target)
    # Same window as `--report`: a read-only daemon calls nobody, so no board
    # is built and no route has to resolve.
    if args.serve is not None:
        return serve(target, args.serve)
    # The board, built once here from the target's `[board]` table and handed
    # down: nothing below reaches for Linear by name. Construction touches
    # neither the network nor the module, so a read-only sweep still calls
    # nobody; the first call that posts to the board is what reads the key.
    # A target with no table has no board, which
    # a read-only sweep can live with (it calls nobody) and the modes that
    # post to the board cannot: they exit here, naming the key to set.
    settings = board_config(target)
    board = (LinearProvider(settings.project_id, settings.team)
             if settings is not None else None)
    # Same window and the same reasons as `--report`: it reads runs and prints
    # them, so no route has to resolve and nobody is called. `--act` fails
    # runs rather than dispatching them, so it needs no route either.
    if args.sweep:
        return sweep_report(target, act=args.act, provider=board)
    # Writes only to the store and calls nobody, so no route has to resolve;
    # but it hands the ticket back to a loop that will mirror it to the
    # board when it claims it again, so a target with no board exits here
    # naming the key, before anything is written.
    if args.requeue is not None:
        require_board(target, board)
        return requeue(target, args.requeue, args.note)
    # Posts to the board, so a target without one exits here naming the key
    # -- before the file is read, so the error is about the target, not the
    # file. The board is the `[board]` pair itself, not the loop's provider:
    # this is an operator command on the Linear module, as `--requeue` is on
    # the store, and the provider protocol does not widen for it.
    if args.file_ticket is not None:
        require_board(target, board)
        return file_ticket(target, args.file_ticket,
                           args.state or FILE_TICKET_STATES[0], settings,
                           priority=args.priority, update=args.update)
    # The acting sweep on a timer. Like `--sweep --act` it dispatches nothing
    # and so resolves no route; unlike it, it takes the target's supervisor
    # lock first, and a target that already has one is an exit, not a loop.
    if args.supervise:
        try:
            return supervise(target, require_board(target, board))
        except SupervisorHeld as held:
            # With the liveness line, so the refusal is actionable: a held
            # lock and a fresh heartbeat is a watcher doing its job; a held
            # lock and a stale one is a watcher to go and look at.
            raise SystemExit(
                f"{held}\n{supervisor_liveness_line(target)}") from None
    # And, on the path that actually dispatches agents, every route the config
    # names resolves before the loop claims a ticket. `--report` skips this: it
    # calls nobody, so a reviewer that is not installed on the machine reading
    # the table is not that reading's problem.
    check_agent_commands(target)
    # Same window, same reason: the `[worktree]` table is read here rather
    # than by the first run that cuts a worktree with it.
    check_worktree_setup(target)
    return main(target, require_board(target, board))


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

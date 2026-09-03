"""The command line: `cli()` parses the arguments and runs the mode they name.

`--report`, `--sweep [--act]`, `--supervise`, `--serve HOST:PORT` and the loop
itself dispatch from here to `holophyte.loop`, `holophyte.supervisor` and
`holophyte.serve`; the `Target` is built once from the command line and handed
down, and the board (`LinearProvider`) is built here and never reached for by
name below. Importing this module
locates no target, reads no config and touches no `HOLOPHYTE_HOME`.

Seventh and last slice of the phase-2 module split; moved verbatim from
`factory.py`, which keeps only `from holophyte.cli import cli` and the
`__main__` guard.
"""
import argparse

from holophyte.config import (
    SUPERVISE_INTERVAL_SEC,
    check_agent_commands,
    check_config_keys,
    loop_config,
    report_config,
    sweep_config,
)
from holophyte.loop import check_worktree_setup, main, report
from holophyte.serve import ADDRESS_SHAPE, parse_address, serve
from holophyte.supervisor import (
    SupervisorHeld,
    supervise,
    supervisor_liveness_line,
    sweep_report,
)
from holophyte.target import Target
from provider import LinearProvider


def serve_address(text):
    """`--serve`'s argparse type: the address as typed, once it parses."""
    try:
        parse_address(text)
    except ValueError as bad:
        raise argparse.ArgumentTypeError(str(bad)) from None
    return text


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
    args = parser.parse_args(argv)
    if args.act and not args.sweep:
        parser.error("--act says what --sweep does with the runs it finds; "
                     "it has nothing to act on by itself")
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
    # The board, built once here and handed down: nothing below reaches for
    # Linear by name. Construction touches neither the network nor the
    # module's configuration, so a read-only sweep still calls nobody; the
    # first call that posts to the board is what reads it.
    board = LinearProvider()
    # Same window and the same reasons as `--report`: it reads runs and prints
    # them, so no route has to resolve and nobody is called. `--act` fails
    # runs rather than dispatching them, so it needs no route either.
    if args.sweep:
        return sweep_report(target, act=args.act, provider=board)
    # The acting sweep on a timer. Like `--sweep --act` it dispatches nothing
    # and so resolves no route; unlike it, it takes the target's supervisor
    # lock first, and a target that already has one is an exit, not a loop.
    if args.supervise:
        try:
            return supervise(target, board)
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
    return main(target, board)

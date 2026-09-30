"""Importing this module locates no target, reads no config or HOLOPHYTE_HOME."""
import sys

from holophyte.board.projection import file_ticket
from holophyte.cli.arguments import (
    FILE_TICKET_STATES,
    _board_verb_checks,
    _close_checks,
    _file_ticket_only,
    _gap_layer_checks,
    _modifier_checks,
    _native_board_only,
    _native_update_only,
    _note_checks,
    build_parser,
)
from holophyte.cli.board_verbs import _board_mode, _refuse_native_key, require_board
from holophyte.cli.cli_story import check_story_arguments
from holophyte.cli.host_modes import (
    _host_mode,
    _read_only_mode,
    _supervise_project,
    start_supervisor,
)
from holophyte.cli.operator import main
from holophyte.cli.store_verbs import _store_verb
from holophyte.config.checks import (
    check_agent_commands,
    check_config,
    check_worktree_setup,
)
from holophyte.config.config_tables import loop_config
from holophyte.config.project import Project
from holophyte.host.startup import eager_import
from holophyte.loop.pool import worker
from holophyte.serve.server import ADDRESS_SHAPE
from provider import board_for


def cli(argv=None):
    from holophyte.cli.cli_project import project_cli
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["project"]:
        return project_cli(argv[1:])
    return _legacy_cli(argv)


def _legacy_cli(argv):
    parser, modes = build_parser()
    args = parser.parse_args(argv)
    eager_import()
    _file_ticket_only(parser, args)
    check_story_arguments(parser, args)
    _board_verb_checks(parser, args)
    _modifier_checks(parser, args)
    _note_checks(parser, args)
    _close_checks(parser, args)
    _gap_layer_checks(parser, args)
    if args.target is None:
        return _host_mode(parser, args)
    if args.serve == "":
        parser.error(f"--serve needs {ADDRESS_SHAPE} with a project; without"
                     " one it serves the host")
    # Modes that write nothing locate without adopting: adoption moves files.
    target = Project.locate(args.target, adopt=not (
        args.import_store is not None or args.board_diff or args.dry_run))
    target.config()
    check_config(target)
    read_only = _read_only_mode(args, target)
    if read_only is not None:
        return read_only()
    _refuse_native_key(target, args, modes)
    board = board_for(target)
    _native_update_only(parser, args, board)
    _native_board_only(parser, args, board)
    board_mode = _board_mode(args, target, board)
    if board_mode is not None:
        return board_mode()
    if _store_verb(args, target, board):
        return None
    if args.file_ticket is not None:
        return file_ticket(target, args.file_ticket,
                           args.state or FILE_TICKET_STATES[0],
                           require_board(target, board),
                           priority=args.priority, update=args.update,
                           revision=args.revision, labels=args.labels)
    if args.supervise:
        return _supervise_project(target, board)
    if args.worker:
        return worker(target, require_board(target, board))
    from holophyte.admission import disabled_startup
    if disabled_startup(target):
        return 0
    check_agent_commands(target)
    check_worktree_setup(target)
    if loop_config(target).spawn_supervisor:
        start_supervisor(target)
    return main(target, require_board(target, board))

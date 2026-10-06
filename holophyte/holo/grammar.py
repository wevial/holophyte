"""`holo` commands as data: each canonical row stands for one factory.py mode."""
import re
from typing import NamedTuple


class Flag(NamedTuple):
    name: str
    metavar: str | tuple | None = None
    emits: tuple = ()

    @property
    def dest(self):
        return self.name.lstrip("-").replace("-", "_")


class Command(NamedTuple):
    words: tuple
    mode: str
    does: str
    takes: tuple = ()
    landed: str | None = None
    note: str | None = None
    flags: tuple = ()
    records: tuple | None = None


ACT = Flag("--act")
JSON = Flag("--json")
DRY_RUN = Flag("--dry-run")
ONCE = Flag("--once")
CLOSE_PR = Flag("--close-pr")
BACKLOG = Flag("--backlog", emits=("--state", "Backlog"))
PRIORITY = Flag("--priority", "P")
UPDATE = Flag("--update", "KEY")
REVISION = Flag("--revision", "N")
LABELS = Flag("--labels", "a,b")
CARRIED_BY = Flag("--carried-by", "KEY")
FOUND_BY = Flag("--found-by", "F")
BASELINE_GREEN = Flag("--baseline-green", "W")
BASELINE_RED_KIND = Flag("--baseline-red-kind", ("KIND", "W"))

REQUIRED, OPTIONAL = "required", "optional"
NEGATIVE_NUMBER = re.compile(r"-[0-9]+|-[0-9]*\.[0-9]+")

COMMANDS = (
    Command(("status",), "--status", "what the factory is doing now", flags=(JSON,)),
    Command(("report",), "--report", "the estimate-vs-actual table"),
    Command(("sweep",), "--sweep", "tripped runs; --act fails them", flags=(ACT,)),
    Command(("board", "diff"), "--board-diff",
            "where the store's ready queue differs from the board's"),
    Command(("board", "import"), "--board-import",
            "copy every open Linear issue into the store", flags=(DRY_RUN,)),
    Command(("store", "import"), "--import-store",
            "what importing another store would move", takes=("PATH",),
            flags=(DRY_RUN,)),
    Command(("file",), "--file-ticket", "file a ticket, or --update its body",
            takes=("FILE",), flags=(BACKLOG, PRIORITY, UPDATE, REVISION, LABELS),
            records=()),
    Command(("move",), "--move", "a native ticket to ready or backlog",
            takes=("KEY", "ready|backlog"), note=OPTIONAL, flags=(REVISION,),
            records=()),
    Command(("cancel",), "--cancel", "cancel a native ticket", takes=("KEY",),
            note=REQUIRED, flags=(REVISION,),
            records=("abort", "abort_close", "close_out")),
    Command(("requeue",), "--requeue", "a failed ticket back in the queue",
            takes=("KEY",), note=REQUIRED, records=("requeue",)),
    Command(("approve",), "--approve", "release a run parked for merge approval",
            takes=("KEY",), note=OPTIONAL, records=("approve",)),
    Command(("babysit",), "--babysit", "look at a parked run's pull request again",
            takes=("KEY",), note=OPTIONAL, records=("babysit", "operator_note")),
    Command(("send-back",), None,
            "send a run parked on its pull request back with an instruction",
            takes=("RUN",), note=REQUIRED, records=("operator_note",)),
    Command(("repoint",), "--repoint", "move a parked candidate to a rebuilt tip",
            takes=("KEY", "SHA"), note=REQUIRED, records=("repoint",)),
    Command(("pause",), "--pause", "stop a run at its next safe point",
            takes=("KEY",), note=REQUIRED, records=("pause",)),
    Command(("resume",), "--resume", "continue a paused run", takes=("KEY",),
            note=REQUIRED, records=("resume",)),
    Command(("abort",), "--abort", "end a run now, preserving its work",
            takes=("KEY",), note=REQUIRED, flags=(CLOSE_PR,),
            records=("abort", "abort_close")),
    Command(("hold",), "--hold", "stop new admission", note=REQUIRED,
            records=("hold",)),
    Command(("release",), "--release-hold", "enable admission again",
            note=REQUIRED, records=("release_hold",)),
    Command(("close",), "--close", "record a change landed outside the factory",
            takes=("KEY",), landed="URL", note=OPTIONAL, records=("close_out",)),
    Command(("gap",), "--gap-layer", "where a gap's lesson landed",
            takes=("KEY", "LAYER"), note=REQUIRED, flags=(CARRIED_BY, FOUND_BY),
            records=()),
    Command(("story", "file"), "--file-story", "file a story from stories/SLUG",
            takes=("SLUG",), flags=(PRIORITY, UPDATE, REVISION)),
    Command(("story", "approve"), "--approve-story",
            "approve a planned story and release its children", takes=("KEY",),
            note=REQUIRED, flags=(REVISION, BASELINE_GREEN, BASELINE_RED_KIND),
            records=("approve_story",)),
    Command(("story", "witness"), "--witness-pass",
            "run an open story's witnesses at main's tip", takes=("KEY",)),
    Command(("story", "decide"), "--decide", "answer a parked story's decision",
            takes=("KEY", "ID", "[OPTION]"), note=REQUIRED, records=("decide",)),
    Command(("supervise",), "--supervise", "the acting sweep on a timer",
            flags=(ONCE,)),
    Command(("serve",), "--serve", "the JSON daemon and the console",
            takes=("[ADDR]",)),
)

NOT_EXPOSED = {"--worker": "internal: the loop's pool spawns it"}

TICKET_VERBS = ("file", "move", "cancel", "requeue", "approve", "babysit",
                "repoint", "pause", "resume", "abort", "close", "gap")

ALIASES = tuple((("ticket", verb), (verb,)) for verb in TICKET_VERBS)

GROUPS = {
    "board": "the board's modes",
    "store": "the store's modes",
    "story": "a story's modes",
    "ticket": "aliases: ticket VERB is holo VERB",
}

PROJECT_HELP = "factory.py project VERB, its arguments passed unchanged"


def canonical(words):
    return next(command for command in COMMANDS if command.words == words)


def _positionals(command):
    names = list(command.takes) + ([command.landed] if command.landed else [])
    return [(f"arg{index}", name) for index, name in enumerate(names)]


def _add_leaf(commands, word, command, help_text):
    leaf = commands.add_parser(word, help=help_text, description=help_text)
    for dest, name in _positionals(command):
        optional = name.startswith("[")
        leaf.add_argument(dest, metavar=name.strip("[]"),
                          nargs="?" if optional else None)
    if command.note:
        leaf.add_argument("note", metavar="NOTE", nargs="?",
                          help=f"why, in free text ({command.note})")
        leaf.add_argument("-n", "--note", dest="note_option", metavar="NOTE",
                          help="the note, as an option")
    for flag in command.flags:
        if flag.metavar is None:
            leaf.add_argument(flag.name, dest=flag.dest, action="store_true")
        else:
            nargs = len(flag.metavar) if isinstance(flag.metavar, tuple) else None
            leaf.add_argument(flag.name, dest=flag.dest, action="append",
                              metavar=flag.metavar, nargs=nargs)
    if command.records is not None:
        leaf.add_argument("--json", dest="json", action="store_true",
                          help="print one JSON result object")
    leaf.add_argument("-p", "--project", metavar="NAME|PATH",
                      help="a registered [serve] name or a repository path")
    leaf.add_argument("--verbose", action="store_true",
                      help="print the project resolved and the source that named it")
    leaf.set_defaults(command=command, leaf=leaf)


def add_commands(parser):
    top = parser.add_subparsers(dest="words", metavar="COMMAND")
    groups = {}
    rows = [(command.words, command, command.does) for command in COMMANDS]
    rows += [(alias, canonical(words), f"= holo {' '.join(words)}")
             for alias, words in ALIASES]
    for words, command, help_text in rows:
        if len(words) == 1:
            _add_leaf(top, words[0], command, help_text)
            continue
        if words[0] not in groups:
            group = top.add_parser(words[0], help=GROUPS[words[0]],
                                   description=GROUPS[words[0]])
            groups[words[0]] = group.add_subparsers(dest="verb", metavar="VERB",
                                                    required=True)
        _add_leaf(groups[words[0]], words[1], command, help_text)
    top.add_parser("project", help=PROJECT_HELP, add_help=False)
    return parser


def _positional(text):
    return (not text.startswith("-") or text == "-" or " " in text
            or bool(NEGATIVE_NUMBER.fullmatch(text)))


def _late_positionals(extra):
    cut = extra.index("--") if "--" in extra else len(extra)
    if not all(map(_positional, extra[:cut])):
        return None
    return extra[:cut] + extra[cut + 1:]


def parse(parser, argv):
    args, extra = parser.parse_known_args(argv)
    command = getattr(args, "command", None)
    if extra and command is not None:
        # argparse leaves an optional positional empty once a flag splits the line.
        slots = [dest for dest, name in _positionals(command) if name.startswith("[")]
        empty = [dest for dest in slots + ["note"] * bool(command.note)
                 if getattr(args, dest) is None]
        late = _late_positionals(extra)
        if late is not None and len(late) <= len(empty):
            for dest, text in zip(empty, late):
                setattr(args, dest, text)
            extra = []
    if extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    return args


def arguments(args, command):
    values = [getattr(args, dest) for dest, _ in _positionals(command)]
    note = getattr(args, "note", None)
    option = getattr(args, "note_option", None)
    if note is not None and option is not None:
        args.leaf.error("give the note once: last, or with -n/--note")
    if command.note and note is None and option is None:
        filled = [index for index, (_, name) in enumerate(_positionals(command))
                  if name.startswith("[") and values[index] is not None]
        if filled:
            note, values[filled[-1]] = values[filled[-1]], None
    return values, note if option is None else option


def factory_argv(args):
    command = args.command
    values, note = arguments(args, command)
    taken = [value for value in values[:len(command.takes)] if value is not None]
    if len(taken) == 1:
        argv = [f"{command.mode}={taken[0]}"]
    else:
        argv = [command.mode, *taken]
    if command.landed and values[-1] is not None:
        argv.append(f"--landed={values[-1]}")
    if note is not None:
        argv.append(f"--note={note}")
    for flag in command.flags:
        given = getattr(args, flag.dest)
        if flag.metavar is None:
            argv += (list(flag.emits) or [flag.name]) if given else []
            continue
        for value in given or ():
            argv += ([flag.name, *value] if isinstance(value, list)
                     else [f"{flag.name}={value}"])
    return argv

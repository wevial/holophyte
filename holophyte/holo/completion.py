"""`holo completion SHELL` and the hidden helper its scripts call for candidates."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import time

from holophyte.holo.grammar import SHOW
from store.enums import GapFinder, GapLayer

CACHE_DIR = "completion"
CACHE_SEC = 60
KEY = "KEY"
PROJECT = "project"
POSITIONAL_CHOICES = {"LAYER": tuple(layer.value for layer in GapLayer)}

BASH = """\
_holo() {
    local IFS=$'\\n'
    COMPREPLY=($(holo __complete "${COMP_WORDS[@]:1:COMP_CWORD}" 2>/dev/null))
}
complete -o default -F _holo holo
"""

ZSH = """\
#compdef holo
_holo() {
    local -a candidates
    candidates=(${(f)"$(holo __complete "${(@)words[2,CURRENT]}" 2>/dev/null)"})
    if (( ${#candidates} )); then
        compadd -a candidates
    else
        _default
    fi
}
if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    _holo "$@"
else
    compdef _holo holo
fi
"""

FISH = """\
function __holo_complete
    set -l words (commandline -opc)
    set -e words[1]
    set -l current (commandline -ct)
    holo __complete $words "$current" 2>/dev/null
end
complete -c holo -e
complete -c holo -a '(__holo_complete)'
"""

SCRIPTS = {"bash": BASH, "zsh": ZSH, "fish": FISH}


def option_choices(name):
    if name == "--priority":
        from holophyte.board.projection import FILE_TICKET_PRIORITIES
        return tuple(FILE_TICKET_PRIORITIES)
    if name == "--found-by":
        return tuple(finder.value for finder in GapFinder)
    return ()


def metavar_choices(metavar):
    parts = metavar.split("|")
    if len(parts) > 1 and all(part.islower() for part in parts):
        return tuple(parts)
    return POSITIONAL_CHOICES.get(metavar, ())


def _subcommands(parser):
    return next((action for action in parser._actions
                 if isinstance(action, argparse._SubParsersAction)), None)


def _arity(action):
    if action.nargs is None or action.nargs == "?":
        return 1
    return action.nargs if isinstance(action.nargs, int) else 0


def _metavars(action):
    metavar = action.metavar or action.dest.upper()
    return metavar if isinstance(metavar, tuple) else (metavar,) * _arity(action)


def _inline(parser, word):
    name, equals, value = word.partition("=")
    if equals and name in parser._option_string_actions:
        head = name + equals
    elif not word.startswith("--") and len(word) > 2:
        name, head, value = word[:2], word[:2], word[2:]
    else:
        return None, None, None
    action = parser._option_string_actions.get(name)
    if action is None or not _arity(action):
        return None, None, None
    return action, head, value


class Walk:
    """Where the words before the one being completed leave the parser."""

    def __init__(self, parser, words):
        self.parser, self.positionals, self.given = parser, [], {}
        self.pending, self.taken = None, 0
        for word in words:
            self.step(word)

    def shown(self):
        subcommands = _subcommands(self.parser)
        return None if subcommands is None else subcommands.choices.get(SHOW)

    def step(self, word):
        if self.pending is not None:
            if word == "=" and not self.taken:
                return
            self.given[self.pending.dest] = word
            self.taken += 1
            if self.taken == _arity(self.pending):
                self.pending = None
            return
        subcommands = _subcommands(self.parser)
        if (subcommands is not None and not self.positionals
                and word in subcommands.choices):
            self.parser = subcommands.choices[word]
            return
        self.parser = self.shown() or self.parser
        if word.startswith("-") and word != "-":
            action, _, value = _inline(self.parser, word)
            if action is not None:
                self.given[action.dest] = value
                return
            action = self.parser._option_string_actions.get(word)
            if action is not None and _arity(action):
                self.pending, self.taken = action, 0
            return
        self.positionals.append(word)

    def offered(self, current, keys):
        if self.pending is not None:
            return self.values(self.pending, self.taken, keys)
        if current.startswith("-"):
            leaf = self.shown() or self.parser
            action, head, _ = _inline(leaf, current)
            if action is not None:
                return [head + value for value in self.values(action, 0, keys)]
            return list(leaf._option_string_actions)
        subcommands = _subcommands(self.parser)
        if subcommands is None:
            return self.positional(self.parser, len(self.positionals), keys)
        if self.positionals:
            return []
        names = [choice.dest for choice in subcommands._choices_actions]
        shown = self.shown()
        return names + (self.positional(shown, 0, keys) if shown else [])

    def positional(self, leaf, index, keys):
        slots = [(action, metavar) for action in leaf._actions
                 if not action.option_strings for metavar in _metavars(action)]
        if index >= len(slots):
            return []
        action, metavar = slots[index]
        return self.choices(action, metavar, keys)

    def values(self, action, index, keys):
        return self.choices(action, _metavars(action)[index], keys)

    def choices(self, action, metavar, keys):
        if action.choices:
            return list(action.choices)
        if metavar == KEY:
            return keys(self.given.get(PROJECT))
        named = [choice for name in action.option_strings
                 for choice in option_choices(name)]
        return named or list(metavar_choices(metavar))


def cache_file(target):
    from holophyte.host.registry import home
    digest = hashlib.sha256(target.encode()).hexdigest()[:16]
    return home() / CACHE_DIR / f"{digest}.keys"


def board_keys(target):
    from holophyte.holo.cli import main
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["board", "--json", "-p", target])
    if code != 0:
        return None
    return [entry["ticket"] for column in json.loads(out.getvalue())["columns"]
            for entry in column["tickets"]]


def _cached(path):
    try:
        if time.time() - path.stat().st_mtime < CACHE_SEC:
            return path.read_text().split()
    except OSError:
        pass
    return None


def _store(path, keys):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = path.with_suffix(f".{os.getpid()}")
        fresh.write_text("".join(f"{key}\n" for key in keys))
        os.replace(fresh, path)
    except OSError:
        pass


def ticket_keys(option):
    from holophyte.holo.resolve import DEFAULT, client_config, resolve
    resolved = resolve(option, client_config().get(DEFAULT))
    if resolved is None:
        return []
    path = cache_file(resolved.value)
    keys = _cached(path)
    if keys is None:
        keys = board_keys(resolved.value)
        if keys is None:
            return []
        _store(path, keys)
    return keys


def quiet_keys(option):
    try:
        with contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            return ticket_keys(option)
    except (Exception, SystemExit):
        return []


def complete(words):
    from holophyte.holo.cli import build_parser
    *before, current = list(words) or [""]
    walk = Walk(build_parser(), before)
    if current == "=" and walk.pending is not None and not walk.taken:
        current = ""
    for candidate in walk.offered(current, quiet_keys):
        if candidate.startswith(current):
            print(candidate)
    return 0


def script(shell):
    print(SCRIPTS[shell], end="")
    return 0

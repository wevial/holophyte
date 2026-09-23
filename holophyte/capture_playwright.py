"""Run a ticket's Playwright capture spec for any Playwright project (KO-650).

A target names this script as its `[merge] ui_capture` command, so it has no
capture script of its own:

    python3 PATH/holophyte/capture_playwright.py [--boot COMMAND]
        [--env NAME=VALUE ...] [--dir DIR] [--config FILE] OUTPUT

The spec is `DIR/<HOLOPHYTE_TICKET>.capture.ts`, outside the project's test
tree. A generated config beside it imports the project's own config and points
its test projects at that spec; the boot command runs with that config and
`CAPTURE_OUT` set to OUTPUT, and the run fails unless an `NN-slug.png` landed.

Standard library only and no `holophyte` imports: it runs by path from a
target's worktree, where the package is not on `sys.path`.
"""
import os
import sys

# Run by path, sys.path[0] is this directory, whose operator.py would shadow
# the standard library module every later import leans on.
HERE = os.path.dirname(os.path.realpath(__file__))
if sys.path and os.path.realpath(sys.path[0] or ".") == HERE:
    del sys.path[0]

import argparse  # noqa: E402 - after the sys.path repair above
import json  # noqa: E402 - after the sys.path repair above
import re  # noqa: E402 - after the sys.path repair above
import shlex  # noqa: E402 - after the sys.path repair above
import signal  # noqa: E402 - after the sys.path repair above
import subprocess  # noqa: E402 - after the sys.path repair above
import tempfile  # noqa: E402 - after the sys.path repair above
from pathlib import Path  # noqa: E402 - after the sys.path repair above

TICKET = re.compile(r"^[A-Za-z]+-[0-9]+$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SHOT = "[0-9][0-9]-*.png"

# Plain JavaScript, which a TypeScript config also accepts. The imported
# config's relative paths would resolve against this file's directory, so a
# helper project's `testDir` is made absolute against the project's own.
TEMPLATE = """\
import config from {config};
import path from 'node:path';

const root = {root};
const capture = {{
  testDir: {dir}, testMatch: [{spec}], testIgnore: [], respectGitIgnore: false,
}};
const helpers = new Set((config.projects || []).flatMap(
  (project) => [...(project.dependencies || []),
                ...(project.teardown ? [project.teardown] : [])]));
const helper = (project) => ({{
  ...project,
  testDir: path.resolve(root, project.testDir ?? config.testDir ?? '.'),
}});

export default config.projects
  ? {{...config, projects: config.projects.map(
      (project) => helpers.has(project.name) ? helper(project)
                                              : {{...project, ...capture}})}}
  : {{...config, ...capture}};
"""


class Refusal(Exception):
    """A run that cannot or did not capture; the message goes to stderr."""


def _arguments(argv):
    parser = argparse.ArgumentParser(
        prog="capture_playwright",
        description="Run HOLOPHYTE_TICKET's Playwright capture spec.")
    parser.add_argument("--boot", default="npx playwright test",
                        help="the project's boot command (default: %(default)s)")
    parser.add_argument("--env", action="append", default=[],
                        metavar="NAME=VALUE",
                        help="extra environment; {key} becomes the ticket key "
                             "lowercased without its hyphen")
    parser.add_argument("--dir", default=os.environ.get(
        "HOLOPHYTE_CAPTURE_DIR") or ".holophyte-capture",
        help="capture directory (default: HOLOPHYTE_CAPTURE_DIR or "
             ".holophyte-capture)")
    parser.add_argument("--config", default="playwright.config.ts",
                        help="the project's config (default: %(default)s)")
    parser.add_argument("output", metavar="OUTPUT",
                        help="directory the screenshots are written to")
    return parser.parse_args(argv)


def _ticket():
    ticket = os.environ.get("HOLOPHYTE_TICKET", "")
    if not TICKET.match(ticket):
        raise Refusal(f"HOLOPHYTE_TICKET must be a ticket key such as KO-7 "
                      f"(letters, a hyphen, digits); got {ticket!r}")
    return ticket


def _extra_env(pairs, key):
    extra = {}
    for pair in pairs:
        name, equals, value = pair.partition("=")
        if not equals or not ENV_NAME.match(name):
            raise Refusal(f"--env expects NAME=VALUE with NAME matching "
                          f"{ENV_NAME.pattern}; got {pair!r}")
        extra[name] = value.replace("{key}", key)
    return extra


def _generated(config, directory, spec):
    """Write the capture config into `directory`; return its absolute path."""
    relative = Path(os.path.relpath(config, directory)).as_posix()
    if not relative.startswith("../"):
        relative = "./" + relative
    text = TEMPLATE.format(config=json.dumps(relative),
                           root=json.dumps(str(config.parent)),
                           dir=json.dumps(str(directory)),
                           spec=json.dumps(spec.name))
    handle, name = tempfile.mkstemp(prefix="holophyte-capture-",
                                    suffix=".config" + config.suffix,
                                    dir=directory)
    with os.fdopen(handle, "w") as file:
        file.write(text)
    return name


def _boot(command, argv, env):
    try:
        code = subprocess.run(shlex.split(command) + argv, env=env,
                              stdin=subprocess.DEVNULL).returncode
    except OSError as error:
        raise Refusal(f"boot command {command!r} could not start: {error}")
    if code:
        raise Refusal(f"boot command {command!r} failed with exit {code}")


def run(argv):
    args = _arguments(argv)
    output = Path(args.output).absolute()
    ticket = _ticket()
    key = ticket.lower().replace("-", "")
    extra = _extra_env(args.env, key)
    spec = Path(args.dir) / f"{ticket}.capture.ts"
    if not spec.is_file():
        raise Refusal(f"no capture spec for {ticket}: expected {spec}")
    config = Path(args.config).absolute()
    if not config.is_file():
        raise Refusal(f"no Playwright config: expected {args.config}")
    output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **extra, "CAPTURE_OUT": str(output)}
    generated = _generated(config, Path(args.dir).absolute(), spec)
    try:
        _boot(args.boot, ["--config", generated, str(spec)], env)
    finally:
        os.unlink(generated)
    if not any(shot.is_file() for shot in output.glob(SHOT)):
        raise Refusal(f"no screenshot in {output}: expected at least one "
                      f"NN-slug.png ({SHOT})")


def _terminated(signum, frame):
    raise SystemExit(128 + signum)


def main(argv=None):
    # A terminated run still unwinds, so the generated config is removed.
    signal.signal(signal.SIGTERM, _terminated)
    try:
        run(sys.argv[1:] if argv is None else argv)
    except Refusal as refusal:
        print(f"capture_playwright: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

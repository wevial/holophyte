"""Run a ticket's Playwright capture spec for any Playwright project (KO-650).

A target names this script as its `[merge] ui_capture` command, so it has no
capture script of its own:

    python3 PATH/holophyte/capture_playwright.py [--boot COMMAND]
        [--env NAME=VALUE ...] [--dir DIR] [--config FILE] [--default SPEC]
        OUTPUT

The spec is `DIR/<HOLOPHYTE_TICKET>.capture.ts`, outside the project's test
tree; when it is absent and the ticket lists no evidence states, `--default`
names the spec run instead; a default outside the working directory runs from
a temporary copy in DIR, so it resolves the project's modules. A generated
config beside the spec imports the project's own config and points its test
projects at that spec; the boot command runs with that config and
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
# Playwright reads a positional filter as a JavaScript regular expression.
REGEX_SPECIAL = re.compile(r"[.*+?^${}()|[\]\\]")

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
    parser.add_argument("--default", metavar="SPEC",
                        help="spec run when the ticket has no spec of its own "
                             "and HOLOPHYTE_EVIDENCE_STATES is empty; one outside "
                             "the working directory runs from a copy in --dir")
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


def _spec(directory, ticket, default, states):
    """The ticket's own spec, else `default` for a ticket listing no states."""
    spec = Path(directory) / f"{ticket}.capture.ts"
    if spec.is_file():
        return spec
    missing = f"no capture spec for {ticket}: expected {spec}"
    if not default or states.strip():
        raise Refusal(missing)
    if not Path(default).is_file():
        raise Refusal(f"{missing}, and no default capture spec: "
                      f"expected {default}")
    return Path(default)


def _copied(spec, default, directory):
    """A copy in `directory` of a default spec outside the working directory.

    Node resolves a spec's imports upward from its own directory, so a default
    kept beside the factory's config runs from here; None for any other spec.
    """
    if not default or spec != Path(default):
        return None
    if Path(os.path.realpath(spec)).is_relative_to(os.path.realpath(".")):
        return None
    if not Path(directory).is_dir():
        raise Refusal(f"no capture directory for the default capture spec "
                      f"{default}: expected {directory}")
    handle, name = tempfile.mkstemp(prefix="holophyte-default-",
                                    suffix=".capture.ts", dir=directory)
    # run() removes only a copy it was handed, so a failed copy removes itself.
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(spec.read_bytes())
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


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
    env = {**os.environ, **extra, "CAPTURE_OUT": str(output)}
    spec = _spec(args.dir, ticket, args.default,
                 env.get("HOLOPHYTE_EVIDENCE_STATES", ""))
    config = Path(args.config).absolute()
    if not config.is_file():
        raise Refusal(f"no Playwright config: expected {args.config}")
    copy = _copied(spec, args.default, args.dir)
    spec = copy or spec
    generated = None
    try:
        output.mkdir(parents=True, exist_ok=True)
        generated = _generated(config, spec.parent.absolute(), spec)
        _boot(args.boot, ["--config", generated,
                          REGEX_SPECIAL.sub(r"\\\g<0>", str(spec))], env)
    finally:
        for made in (generated, copy):
            if made:
                os.unlink(made)
    if not any(shot.is_file() for shot in output.glob(SHOT)):
        raise Refusal(f"no screenshot in {output}: expected at least one "
                      f"NN-slug.png ({SHOT})")


def _terminated(signum, frame):
    raise SystemExit(128 + signum)


def main(argv=None):
    # A terminated run still unwinds, so the generated files are removed.
    signal.signal(signal.SIGTERM, _terminated)
    try:
        run(sys.argv[1:] if argv is None else argv)
    except Refusal as refusal:
        print(f"capture_playwright: {refusal}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

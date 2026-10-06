import argparse
import sys
import tomllib
from importlib import metadata

from holophyte.holo.grammar import add_commands, factory_argv, parse
from holophyte.host.startup import build_sha, factory_checkout

DISTRIBUTION = "holophyte"


def package_version():
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        with (factory_checkout() / "pyproject.toml").open("rb") as stream:
            return tomllib.load(stream)["project"]["version"]


class UsageError(SystemExit):
    def __init__(self, line):
        super().__init__(2)
        self.line = line


class Parser(argparse.ArgumentParser):
    def error(self, message):
        line = f"{self.prog}: error: {message}"
        self.print_usage(sys.stderr)
        sys.stderr.write(line + "\n")
        raise UsageError(line)


def build_parser():
    parser = Parser(prog="holo")
    parser.add_argument("--version", action="store_true",
                        help="print the package version and the checkout's build")
    return add_commands(parser)


def project_argv(value):
    if value is None:
        return []
    if "/" not in value and value not in (".", ".."):
        from holophyte.host.registry import Host, HostError
        try:
            entry = Host.locate().project(value)
        except HostError as bad:
            raise SystemExit(str(bad)) from None
        if entry is not None:
            return [str(entry.path)]
    return [value]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["project"]:
        from holophyte.cli.entry import cli
        return cli(argv)
    parser = build_parser()
    try:
        args = parse(parser, argv)
    except UsageError as usage:
        from holophyte.holo.results import usage_result
        usage_result(argv, usage.line)
        raise
    if args.version:
        print(f"holo {package_version()} (build {build_sha()})")
        return 0
    if args.words is None:
        parser.print_help()
        return 0
    if args.command.records is not None:
        from holophyte.holo.results import run_write
        return run_write(args, project_argv)
    from holophyte.cli.entry import _legacy_cli
    return _legacy_cli(project_argv(args.project) + factory_argv(args))

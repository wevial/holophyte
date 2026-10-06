import argparse
import sys
import tomllib
from importlib import metadata

from holophyte.holo.grammar import (
    COMPLETION,
    HELPER,
    READS,
    add_commands,
    factory_argv,
    parse,
)
from holophyte.host.startup import build_sha, factory_checkout

DISTRIBUTION = "holophyte"


def package_version():
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        with (factory_checkout() / "pyproject.toml").open("rb") as stream:
            return tomllib.load(stream)["project"]["version"]


class UsageError(SystemExit):
    def __init__(self, line, partial=None):
        super().__init__(2)
        self.line = line
        self.partial = partial


class Parser(argparse.ArgumentParser):
    partial = None

    def parse_known_args(self, args=None, namespace=None):
        self.partial = argparse.Namespace() if namespace is None else namespace
        return super().parse_known_args(args, self.partial)

    def error(self, message):
        line = f"{self.prog}: error: {message}"
        self.print_usage(sys.stderr)
        sys.stderr.write(line + "\n")
        raise UsageError(line, self.partial)


def build_parser():
    parser = Parser(prog="holo")
    parser.add_argument("--version", action="store_true",
                        help="print the package version and the checkout's build")
    return add_commands(parser)


def project_argv(args, default):
    from holophyte.holo.resolve import HOST_FORMS, none_found, resolve
    from holophyte.host.registry import HostError
    try:
        resolved = resolve(args.project, default)
    except HostError as bad:
        raise SystemExit(str(bad)) from None
    if resolved is None:
        if args.command.mode not in HOST_FORMS:
            print(none_found(args.command.words), file=sys.stderr)
            raise SystemExit(2)
        if args.verbose:
            print("[holo2] no source names a project: the host form",
                  file=sys.stderr)
        return []
    if args.verbose:
        print(f"[holo2] project {resolved.shown} from {resolved.source}",
              file=sys.stderr)
    return [resolved.value]


def remote(args, argv, host, config):
    from holophyte.holo.resolve import Refused
    from holophyte.holo.transport import run
    try:
        return run(args, host, config)
    except (Refused, UsageError) as refused:
        from holophyte.holo.results import usage_result
        usage_result(argv, refused.line)
        raise


def client(argv):
    from holophyte.holo.resolve import Refused, client_config
    from holophyte.holo.transport import remote_host
    try:
        config = client_config()
        return config, remote_host(config)
    except Refused as refused:
        from holophyte.holo.results import usage_result
        usage_result(argv, refused.line)
        raise


def project(argv, host, config):
    if host is not None:
        from holophyte.holo.transport import run_project
        return run_project(argv, host, config)
    from holophyte.cli.entry import cli
    return cli(argv)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == [HELPER]:
        from holophyte.holo.completion import complete
        return complete(argv[1:])
    from holophyte.holo.resolve import DEFAULT, TIMEZONE
    config, host = client(argv)
    default = config.get(DEFAULT)
    if argv[:1] == ["project"]:
        return project(argv, host, config)
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
    if args.command is COMPLETION:
        from holophyte.holo.completion import script
        return script(args.shell)
    if host is not None:
        return remote(args, argv, host, config)
    if getattr(args, "foreground", False):
        from holophyte.holo.units import foreground
        return foreground(args, project_argv(args, default))
    if args.command.records is not None:
        from holophyte.holo.results import run_write
        return run_write(args, lambda: project_argv(args, default))
    target = project_argv(args, default)
    if args.command.mode == "--status" and not args.json:
        from holophyte.holo.status_page import show
        return show(target, config.get(TIMEZONE))
    if args.command in READS:
        from holophyte.holo.reads import read
        return read(args, target[0] if target else None,
                    config.get(TIMEZONE))
    from holophyte.cli.entry import _legacy_cli
    return _legacy_cli(target + factory_argv(args))

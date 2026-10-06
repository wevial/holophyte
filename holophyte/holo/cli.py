import argparse
import sys
import tomllib
from importlib import metadata

from holophyte.holo.grammar import READS, add_commands, factory_argv, parse
from holophyte.host.startup import build_sha, factory_checkout

DISTRIBUTION = "holophyte"


def package_version():
    try:
        return metadata.version(DISTRIBUTION)
    except metadata.PackageNotFoundError:
        with (factory_checkout() / "pyproject.toml").open("rb") as stream:
            return tomllib.load(stream)["project"]["version"]


def build_parser():
    parser = argparse.ArgumentParser(prog="holo")
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


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from holophyte.holo.resolve import DEFAULT, client_config
    default = client_config().get(DEFAULT)
    if argv[:1] == ["project"]:
        from holophyte.cli.entry import cli
        return cli(argv)
    parser = build_parser()
    args = parse(parser, argv)
    if args.version:
        print(f"holo {package_version()} (build {build_sha()})")
        return 0
    if args.words is None:
        parser.print_help()
        return 0
    target = project_argv(args, default)
    if args.command in READS:
        from holophyte.holo.reads import read
        return read(args, target[0] if target else None)
    from holophyte.cli.entry import _legacy_cli
    return _legacy_cli(target + factory_argv(args))

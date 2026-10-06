import argparse
import tomllib
from importlib import metadata

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
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"holo {package_version()} (build {build_sha()})")
    else:
        parser.print_help()
    return 0

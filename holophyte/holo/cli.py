import argparse
import tomllib
from importlib import metadata

from holophyte.host.startup import build_sha, factory_checkout

DISTRIBUTION = "holophyte"


def package_version():
    # An editable install's metadata keeps the version it was installed at.
    pyproject = factory_checkout() / "pyproject.toml"
    if not pyproject.is_file():
        return metadata.version(DISTRIBUTION)
    with pyproject.open("rb") as stream:
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

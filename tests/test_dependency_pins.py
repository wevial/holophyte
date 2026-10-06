"""Every place the factory installs its dependencies from names the same pins."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def requirement_pins():
    found = {}
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, _, version = line.partition("==")
            found[name.strip()] = version.strip()
    return found


def install_section(readme):
    match = re.search(r"^## Install\n(.*?)(?=^## |\Z)", readme, re.M | re.S)
    return match.group(1) if match else ""


class DependencyPinTests(unittest.TestCase):
    def test_dockerfile_version_arguments_equal_the_requirements_pins(self):
        dockerfile = (ROOT / "docker" / "reviewer.Dockerfile").read_text()
        arguments = dict(re.findall(r"^ARG (\w+)_VERSION=(\S+)$", dockerfile, re.M))
        pins = requirement_pins()
        self.assertLessEqual({"tomlkit", "mcp"}, set(pins))
        for name, version in pins.items():
            with self.subTest(package=name):
                self.assertEqual(arguments.get(name.upper()), version)

    def test_readme_install_section_names_every_pinned_package(self):
        section = install_section((ROOT / "README.md").read_text())
        self.assertTrue(section, "README has no Install section")
        for name in requirement_pins():
            with self.subTest(package=name):
                self.assertIn(f"`{name}`", section)


if __name__ == "__main__":
    unittest.main()

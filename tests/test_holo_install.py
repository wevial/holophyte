"""`holo` installs editable from a checkout and reports that checkout's build."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT = ["git", "-c", "user.name=holo test", "-c", "user.email=holo@example.invalid",
       "-c", "commit.gpgsign=false"]


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def checkout_files():
    listed = git(ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return [name for name in listed.split("\0") if name and (ROOT / name).is_file()]


def copy_checkout(copy):
    for name in checkout_files():
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    git(copy, "init", "-q")
    git(copy, "add", "-A")
    git(copy, "commit", "-qm", "copy of the checkout")


def fresh_venv(venv):
    subprocess.run([sys.executable, "-m", "venv", venv], check=True)
    return venv / "bin" / "python"


def expected_line(checkout):
    with (checkout / "pyproject.toml").open("rb") as stream:
        version = tomllib.load(stream)["project"]["version"]
    return f"holo {version} (build {git(checkout, 'rev-parse', '--short', 'HEAD')})"


def pins(lines):
    found = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            name, separator, version = line.partition("==")
            found[re.sub(r"[-_.]+", "-", name.strip().lower())] = (
                separator, version.strip())
    return found


class EditableInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.mkdtemp()
        cls.addClassCleanup(shutil.rmtree, cls.directory)
        cls.copy = Path(cls.directory) / "checkout"
        copy_checkout(cls.copy)
        venv = Path(cls.directory) / "venv"
        python = fresh_venv(venv)
        install = subprocess.run(
            [python, "-m", "pip", "install", "-q", "-e", cls.copy],
            capture_output=True, text=True)
        if install.returncode != 0:
            raise AssertionError(f"pip install -e failed:\n{install.stderr}")
        cls.holo = venv / "bin" / "holo"

    def run_holo(self, *args):
        return subprocess.run([self.holo, *args], cwd=self.directory,
                              capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": ""})

    def test_installed_holo_prints_the_copy_version_and_short_head(self):
        result = self.run_holo("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), expected_line(self.copy))

    def test_installed_holo_follows_a_new_commit_without_reinstall(self):
        before = self.run_holo("--version").stdout.strip()
        git(self.copy, "commit", "-q", "--allow-empty", "-m", "a later commit")
        after = self.run_holo("--version")
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertNotEqual(after.stdout.strip(), before)
        self.assertEqual(after.stdout.strip(), expected_line(self.copy))

    def test_install_leaves_the_copy_clean(self):
        self.assertEqual(git(self.copy, "status", "--porcelain"), "")

    def test_help_exits_zero_and_an_unknown_command_exits_two_with_usage(self):
        self.assertEqual(self.run_holo("--help").returncode, 0)
        unknown = self.run_holo("no-such-command")
        self.assertEqual(unknown.returncode, 2)
        self.assertTrue(unknown.stderr.startswith("usage: holo"), unknown.stderr)


class UninstalledCheckoutTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        self.copy = Path(directory) / "checkout"
        copy_checkout(self.copy)
        self.python = fresh_venv(Path(directory) / "venv")
        self.env = {key: value for key, value in os.environ.items()
                    if key != "PYTHONPATH"}

    def test_module_form_prints_the_checkout_version_line(self):
        installed = subprocess.run(
            [self.python, "-c", "from importlib import metadata; "
             "metadata.version('holophyte')"],
            cwd=self.copy, capture_output=True, text=True, env=self.env)
        self.assertIn("PackageNotFoundError", installed.stderr)
        result = subprocess.run([self.python, "-m", "holophyte.holo", "--version"],
                                cwd=self.copy, capture_output=True, text=True,
                                env=self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), expected_line(self.copy))


class DependencyPinTests(unittest.TestCase):
    def test_pyproject_and_requirements_pin_the_same_versions(self):
        with (ROOT / "pyproject.toml").open("rb") as stream:
            declared = tomllib.load(stream)["project"]["dependencies"]
        required = (ROOT / "requirements.txt").read_text().splitlines()
        self.assertTrue(declared)
        self.assertEqual(pins(declared), pins(required))
        self.assertEqual({separator for separator, _ in pins(declared).values()},
                         {"=="})


if __name__ == "__main__":
    unittest.main()

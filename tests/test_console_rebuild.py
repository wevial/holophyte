"""The host daemon's startup console build: a `console/dist` stamped with
another `console/` tree is rebuilt and swapped in, a matching one is left
alone, and a failed build leaves the previous one served.

Run: python3 -m unittest discover -s tests -p 'test_console_rebuild.py' -v
"""
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from holophyte.serve.console_build import refresh_console
from holophyte.serve.server import static_file
from tests.host_fixture import git

ROOT = Path(__file__).resolve().parent.parent

STAND_IN = """\
import subprocess, sys
from pathlib import Path
outdir, runs = Path(sys.argv[1]), Path(sys.argv[2])
with runs.open("a") as log:
    log.write("build\\n")
(outdir / "index.html").write_text("new")
tree = subprocess.run(["git", "rev-parse", "HEAD:./"], capture_output=True,
                      text=True, check=True).stdout
(outdir / "source-tree").write_text(tree)
"""

FAILING = """\
import sys
for n in range(40):
    print(f"bundler line {n}")
sys.exit(1)
"""


class StartupCheckTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = self.root / "repo"
        self.sources = self.repo / "console"
        (self.sources / "src").mkdir(parents=True)
        (self.sources / "package.json").write_text("{}\n")
        (self.sources / "src" / "app.ts").write_text("v1\n")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "v1")
        self.old_tree = git(self.repo, "rev-parse", "HEAD:console")
        self.dist = self.sources / "dist"
        self.dist.mkdir()
        (self.dist / "index.html").write_text("old")
        (self.dist / "source-tree").write_text(self.old_tree + "\n")
        self.runs = self.root / "runs.log"
        self.out = io.StringIO()

    def change_sources(self):
        (self.sources / "src" / "app.ts").write_text("v2\n")
        git(self.repo, "commit", "-q", "-am", "v2")

    def check(self, script):
        path = self.root / "build.py"
        path.write_text(script)
        refresh_console(self.out, sources=self.sources,
                        commands=lambda outdir: (
                            (sys.executable, str(path), str(outdir),
                             str(self.runs)),))

    def build_count(self):
        return len(self.runs.read_text().splitlines()) if self.runs.exists() else 0

    def assert_no_staging_left(self):
        self.assertEqual(sorted(p.name for p in self.sources.iterdir()),
                         ["dist", "package.json", "src"])

    def test_a_changed_console_tree_is_rebuilt_once_and_swapped_in(self):
        self.change_sources()
        self.check(STAND_IN)
        self.assertEqual(self.build_count(), 1)
        self.assertEqual((self.dist / "index.html").read_text(), "new")
        new_tree = git(self.repo, "rev-parse", "HEAD:console")
        self.assertNotEqual(new_tree, self.old_tree)
        self.assertEqual((self.dist / "source-tree").read_text().strip(),
                         new_tree)
        self.assert_no_staging_left()

    def test_a_stamp_matching_the_tree_runs_no_build(self):
        self.check(STAND_IN)
        self.assertEqual(self.build_count(), 0)
        self.assertEqual((self.dist / "index.html").read_text(), "old")

    def test_a_failed_build_keeps_serving_the_previous_one_and_logs_its_tail(self):
        self.change_sources()
        self.check(FAILING)
        self.assertEqual((self.dist / "index.html").read_text(), "old")
        self.assertEqual((self.dist / "source-tree").read_text().strip(),
                         self.old_tree)
        self.assertEqual(static_file(self.dist, "/")[0], b"old")
        log = self.out.getvalue()
        self.assertIn("console build failed", log)
        self.assertIn("exited 1", log)
        self.assertIn("bundler line 39", log)
        self.assertNotIn("bundler line 0\n", log)
        self.assert_no_staging_left()


@unittest.skipUnless(shutil.which("bun"),
                     "bun is not installed; the console build needs it")
@unittest.skipUnless((ROOT / "console" / "node_modules").is_dir(),
                     "console/node_modules is missing; run"
                     " `bun --cwd=console install --frozen-lockfile` first")
class RealBuildStampTests(unittest.TestCase):
    def test_the_console_build_stamps_its_output_with_the_console_tree(self):
        outdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outdir, True)
        done = subprocess.run(["bun", "--cwd=console", "run", "build",
                               str(outdir)], cwd=ROOT, capture_output=True,
                              text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((outdir / "index.html").is_file())
        tree = subprocess.run(["git", "rev-parse", "HEAD:console"], cwd=ROOT,
                              capture_output=True, text=True,
                              check=True).stdout.strip()
        self.assertEqual((outdir / "source-tree").read_text().strip(), tree)


if __name__ == "__main__":
    unittest.main()

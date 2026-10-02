"""The host daemon's startup console build: a `console/dist` stamped with
another `console/` tree is rebuilt and swapped in, a matching one is left
alone, and a failed build leaves the previous one served. `bun` is a
stand-in on `PATH` except in the last test.

Run: python3 -m unittest discover -s tests -p 'test_console_rebuild.py' -v
"""
import http.client
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

from holophyte.host.registry import Host
from holophyte.serve.console_build import refresh_console
from holophyte.serve.serve_host import serve_host
from tests.host_fixture import HostFixture, git

ROOT = Path(__file__).resolve().parent.parent
SERVING = re.compile(r"\[holo2\] serving 127\.0\.0\.1:(\d+) ")
COMMIT = ("git -c user.name=t -c user.email=t@t -c commit.gpgsign=false"
          " -c core.hooksPath=/dev/null commit -q --allow-empty -am moved")

FAKE_BUN = """\
import subprocess
import sys
from pathlib import Path
here = Path(__file__).parent
with (here / "calls.log").open("a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1] == "run":
    outdir = Path(sys.argv[-1])
    exec((here / "build.py").read_text())
"""

NEW_BUILD = """\
(outdir / "index.html").write_text("new")
tree = subprocess.run(["git", "rev-parse", "HEAD:./"], capture_output=True,
                      text=True, check=True).stdout
(outdir / "source-tree").write_text(tree)
"""

FAILING_BUILD = """\
for n in range(40):
    print(f"bundler line {n}")
sys.exit(1)
"""

SOURCES_MOVE = f"""\
Path("src/app.ts").write_text("v3\\n")
subprocess.run({COMMIT!r}.split(), check=True)
""" + NEW_BUILD


class ConsoleCase(HostFixture):
    def setUp(self):
        super().setUp()
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
        self.bin = self.root / "bin"
        self.bin.mkdir()
        bun = self.bin / "bun"
        bun.write_text(f"#!{sys.executable}\n{FAKE_BUN}")
        bun.chmod(0o755)
        self.enterContext(patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}))
        self.out = io.StringIO()

    def build_with(self, script):
        (self.bin / "build.py").write_text(script)

    def change_sources(self):
        (self.sources / "src" / "app.ts").write_text("v2\n")
        git(self.repo, "commit", "-q", "-am", "v2")

    def bun_calls(self):
        log = self.bin / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def assert_old_build_kept(self):
        self.assertEqual((self.dist / "index.html").read_text(), "old")
        self.assertEqual((self.dist / "source-tree").read_text().strip(),
                         self.old_tree)
        self.assert_no_staging_left()

    def assert_no_staging_left(self):
        self.assertEqual(sorted(p.name for p in self.sources.iterdir()),
                         ["dist", "package.json", "src"])


class StartupCheckTests(ConsoleCase):
    def test_a_changed_console_tree_is_rebuilt_once_and_swapped_in(self):
        self.change_sources()
        self.build_with(NEW_BUILD)
        refresh_console(self.out, self.dist)
        calls = self.bun_calls()
        self.assertEqual(calls[0], "install --frozen-lockfile")
        self.assertEqual([c.split()[:2] for c in calls[1:]], [["run", "build"]])
        self.assertEqual((self.dist / "index.html").read_text(), "new")
        new_tree = git(self.repo, "rev-parse", "HEAD:console")
        self.assertNotEqual(new_tree, self.old_tree)
        self.assertEqual((self.dist / "source-tree").read_text().strip(),
                         new_tree)
        self.assert_no_staging_left()

    def test_a_stamp_matching_the_tree_runs_no_build(self):
        self.build_with(NEW_BUILD)
        refresh_console(self.out, self.dist)
        self.assertEqual(self.bun_calls(), [])
        self.assert_old_build_kept()

    def test_a_build_stamped_with_sources_that_moved_under_it_is_not_published(self):
        self.change_sources()
        self.build_with(SOURCES_MOVE)
        refresh_console(self.out, self.dist)
        self.assert_old_build_kept()
        self.assertIn("console build failed", self.out.getvalue())


class DaemonStartupTests(ConsoleCase):
    def setUp(self):
        super().setUp()
        previous = signal.signal(signal.SIGTERM, lambda *_: None)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        self.factory = self.root / "factory"
        self.factory.mkdir()
        git(self.factory, "init", "-q")
        git(self.factory, "commit", "-q", "--allow-empty", "-m", "A")
        self.enterContext(patch("holophyte.serve.serve_host.CONSOLE_DIR",
                                self.dist))
        self.enterContext(patch(
            "holophyte.serve.serve_host.factory_revision",
            lambda: git(self.factory, "rev-parse", "HEAD")))
        self.reexec = self.enterContext(
            patch("holophyte.serve.server.reexec_self"))

    def serve(self, on_serving=None, interval=0.1):
        """Run the host daemon here, stopping it with SIGTERM once
        `on_serving(port)` returns, or after 20s if it has not exited."""
        done = threading.Event()

        def drive():
            deadline = monotonic() + 20
            if on_serving is None:
                done.wait(20)
            else:
                found = None
                while found is None and monotonic() < deadline:
                    found = SERVING.search(self.out.getvalue())
                    sleep(0.05)
                if found is not None:
                    on_serving(int(found.group(1)))
            if not done.is_set():
                os.kill(os.getpid(), signal.SIGTERM)
        driver = threading.Thread(target=drive)
        driver.start()
        try:
            return serve_host(Host.locate(), "127.0.0.1:0", self.out,
                              interval=interval)
        finally:
            done.set()
            driver.join()

    def test_a_failed_build_leaves_the_daemon_serving_the_previous_console(self):
        self.change_sources()
        self.build_with(FAILING_BUILD)
        pages = []

        def fetch(port):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                conn.request("GET", "/")
                response = conn.getresponse()
                pages.append((response.status, response.read()))
            finally:
                conn.close()
        self.assertEqual(self.serve(fetch, interval=60), 0)
        self.assertEqual(pages, [(200, b"old")])
        self.assert_old_build_kept()
        log = self.out.getvalue()
        self.assertIn("console build failed, serving the previous build", log)
        self.assertIn("exited 1", log)
        self.assertIn("bundler line 39", log)
        self.assertNotIn("bundler line 0\n", log)

    def test_a_factory_commit_during_the_build_moves_the_daemon_on(self):
        started = git(self.factory, "rev-parse", "HEAD")
        self.change_sources()
        self.build_with(f"subprocess.run({COMMIT!r}.split(), check=True,"
                        f" cwd={str(self.factory)!r})\n" + NEW_BUILD)
        self.serve()
        moved = git(self.factory, "rev-parse", "HEAD")
        self.assertNotEqual(moved, started)
        self.assertEqual((self.dist / "index.html").read_text(), "new")
        self.reexec.assert_called_once()
        self.assertIn(f"moved from {started} to {moved}",
                      self.reexec.call_args.args[0])


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

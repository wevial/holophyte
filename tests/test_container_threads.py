"""The pids limit of a real isolated implementer launch, which counts threads."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent

THREADS = """
import threading, time
threads = [threading.Thread(target=time.sleep, args=(1,)) for _ in range(400)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print(len(threads))
"""

FORK_LOOP = """
i=0
while [ "$i" -lt 6000 ]; do
  sleep 300 &
  i=$((i + 1))
done
echo "started $i without a fork failure"
"""


@unittest.skipUnless(
    os.environ.get("HOLOPHYTE_TEST_DOCKER") == "1",
    "set HOLOPHYTE_TEST_DOCKER=1 for container integration",
)
class ContainerThreadsTests(unittest.TestCase):
    def launch(self, argv):
        from holophyte.config.project import Project
        from holophyte.isolation import launcher
        from holophyte.isolation.isolation_git import git

        if not shutil.which("docker"):
            self.skipTest("Docker absent")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        clone = root / "checkout"
        git(root, "clone", "-q", str(ROOT), str(clone))
        with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(root / "home")}):
            project = Project.locate(clone, adopt=False)
            project.config_path.parent.mkdir(parents=True)
            project.config_path.write_text(
                "[agents]\n"
                'implementer_isolation = { backend = "container",'
                ' memory = "4g", writable = true }\n'
            )
            return launcher.launch(
                launcher.route_for(project), clone,
                launcher.environment(project), argv,
                timeout=300, project=project,
            )

    def test_four_hundred_threads_start_and_join(self):
        code, output = self.launch(["python3", "-c", THREADS])
        self.assertEqual(code, 0, output)
        self.assertEqual(output.strip().splitlines()[-1], "400", output)

    def test_unbounded_background_processes_stop_at_a_fork_failure(self):
        code, output = self.launch(["sh", "-c", FORK_LOOP])
        self.assertNotEqual(code, 0, output)
        self.assertNotIn("without a fork failure", output)
        self.assertRegex(output, r"(?i)fork", output)


if __name__ == "__main__":
    unittest.main()
